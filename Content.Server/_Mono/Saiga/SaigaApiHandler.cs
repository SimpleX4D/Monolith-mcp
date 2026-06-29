using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Numerics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using Content.Server.Spawners.Components;
using Content.Server.Speech;
using Content.Server.Speech.Components;
using Content.Shared._Mono.Saiga;
using Content.Shared.Construction.Prototypes;
using Content.Shared.Examine;
using Content.Shared.Ghost.Roles.Components;
using Content.Shared.Inventory;
using Content.Shared.Item;
using Content.Shared.Mobs.Components;
using Content.Shared.SubFloor;
using Content.Shared.Tools.Components;
using Robust.Server.ServerStatus;
using Robust.Shared.Asynchronous;
using Robust.Shared.Audio.Components;
using Robust.Shared.Configuration;
using Robust.Shared.Containers;
using Robust.Shared.Map;
using Robust.Shared.Player;
using Robust.Shared.Prototypes;
using Robust.Shared.Timing;

namespace Content.Server._Mono.Saiga;

/// <summary>
///     Minimal HTTP API for the Saiga agent. Exposes agent tools via simple JSON endpoints.
///     The Python MCP proxy (agent-runner/mcp_server.py) talks to this API and translates
///     to the MCP protocol for LM Studio / Claude / etc.
/// </summary>
public sealed class SaigaApiHandler : EntitySystem
{
    [Dependency] private readonly IStatusHost _statusHost = default!;
    [Dependency] private readonly IConfigurationManager _cfg = default!;
    [Dependency] private readonly ITaskManager _taskManager = default!;
    [Dependency] private readonly SaigaManager _saiga = default!;
    [Dependency] private readonly EntityLookupSystem _lookup = default!;
    [Dependency] private readonly SharedTransformSystem _xform = default!;
    [Dependency] private readonly SharedContainerSystem _container = default!;
    [Dependency] private readonly ExamineSystemShared _examine = default!;
    [Dependency] private readonly InventorySystem _inventory = default!;
    [Dependency] private readonly IGameTiming _timing = default!;
    [Dependency] private readonly IPrototypeManager _proto = default!;

    private ISawmill _sawmill = default!;
    private bool _enabled;
    private string _token = string.Empty;

    private const float ObserveRange = 10f;
    private const int ObserveMax = 20;
    private const int RecallMax = 25;
    private const int RecipesMax = 30;
    private const int HeardMax = 12;

    // ---- Tool specs with JSON Schema ----

    private sealed record ToolSpec(string Name, string Description, JsonElement InputSchema);

    private static ToolSpec[] BuildSpecs()
    {
        return new[]
        {
            MakeSpec("observe", "Что агент видит вокруг (id, имя, расстояние, направление). filter — опц. имена через запятую, вернуть ТОЛЬКО их (напр. «яблоко,морковь»); пусто = всё.",
                new { agent = new { type = "number", description = "Net id агента" }, filter = new { type = "string", description = "Имена через запятую для фильтрации (опционально)" } },
                new[] { "agent" }),

            MakeSpec("listen", "Что агенту сказали вслух рядом: кто и что.",
                new { agent = new { type = "number", description = "Net id агента" } },
                new[] { "agent" }),

            MakeSpec("say", "Заставить агента произнести фразу вслух.",
                new { agent = new { type = "number", description = "Net id агента" }, text = new { type = "string", description = "Текст реплики" } },
                new[] { "agent", "text" }),

            MakeSpec("act", "Выполнить действие (follow, pickup, pull, throw, move_to, place, use_on, stop, drop, swap, build, store, activate, craft, construct).",
                new { agent = new { type = "number", description = "Net id агента" }, action = new { type = "string", description = "Название действия" }, target = new { type = "number", description = "Net id цели (опц.)" }, recipe = new { type = "string", description = "Id рецепта для craft/construct (опц.)" } },
                new[] { "agent", "action" }),

            MakeSpec("recall", "Вспомнить, что агент видел раньше (память-граф).",
                new { agent = new { type = "number", description = "Net id агента" }, query = new { type = "string", description = "Подстрока имени для фильтра (опц.)" } },
                new[] { "agent" }),

            MakeSpec("recipes", "Меню крафта: список рецептов сборки.",
                new { query = new { type = "string", description = "Фильтр по имени/категории (опц.)" } },
                Array.Empty<string>()),

            MakeSpec("where_is", "Где агент в последний раз видел объект.",
                new { agent = new { type = "number", description = "Net id агента" }, name = new { type = "string", description = "Имя объекта" } },
                new[] { "agent", "name" }),
        };
    }

    private static ToolSpec MakeSpec(string name, string desc, object props, string[] required)
    {
        var schema = new { type = "object", properties = props, required };
        var json = JsonSerializer.SerializeToElement(schema, SerializerOptions);
        return new ToolSpec(name, desc, json);
    }

    private static readonly JsonSerializerOptions SerializerOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = false
    };

    private ToolSpec[] _specs = Array.Empty<ToolSpec>();

    // ---- Init ----

    public override void Initialize()
    {
        base.Initialize();
        _sawmill = Logger.GetSawmill("saiga.api");
        _specs = BuildSpecs();

        _cfg.OnValueChanged(SaigaMcpCVars.Enabled, v => _enabled = v, true);
        _cfg.OnValueChanged(SaigaMcpCVars.Token, v => _token = v, true);
        _statusHost.AddHandler(HandleAsync);

        SubscribeLocalEvent<ActiveListenerComponent, ListenEvent>(OnHeard);
    }

    // ---- Listen capture ----

    private void OnHeard(EntityUid uid, ActiveListenerComponent comp, ListenEvent args)
    {
        if (args.Source == uid || !HasComp<SaigaAgentStateComponent>(uid))
            return;
        var text = args.Message.Trim();
        if (text.Length == 0)
            return;

        var hearing = EnsureComp<SaigaHearingComponent>(uid);
        hearing.Lines.Add(new HeardLine
        {
            Net = GetNetEntity(args.Source),
            Speaker = MetaData(args.Source).EntityName,
            Text = text,
            Time = _timing.CurTime,
        });
        while (hearing.Lines.Count > HeardMax)
            hearing.Lines.RemoveAt(0);
    }

    // ---- HTTP handler ----

    private async Task<bool> HandleAsync(IStatusHandlerContext context)
    {
        var path = context.Url.AbsolutePath.TrimEnd('/');
        if (!path.StartsWith("/api/agent"))
            return false;

        if (!_enabled || string.IsNullOrEmpty(_token))
        {
            context.RespondErrorAsync(HttpStatusCode.NotFound);
            return true;
        }

        if (context.RequestMethod != HttpMethod.Post)
        {
            context.RespondErrorAsync(HttpStatusCode.MethodNotAllowed);
            return true;
        }

        if (!CheckAuth(context))
        {
            context.RespondErrorAsync(HttpStatusCode.Unauthorized);
            return true;
        }

        JsonElement body;
        try
        {
            body = await context.RequestBodyJsonAsync<JsonElement>();
        }
        catch (Exception)
        {
            await context.RespondErrorAsync(HttpStatusCode.BadRequest);
            return true;
        }

        try
        {
            var result = await RunOnMainThread(() => Dispatch(path, body));
            await context.RespondJsonAsync((object)result);
        }
        catch (Exception e)
        {
            _sawmill.Warning($"API {path} error: {e.Message}");
            await context.RespondErrorAsync(HttpStatusCode.InternalServerError);
        }

        return true;
    }

    private object Dispatch(string path, JsonElement body)
    {
        return path switch
        {
            "/api/agent/ping" => new { ok = true },
            "/api/agent/tools" => HandleTools(),
            "/api/agent/observe" => HandleObserve(body),
            "/api/agent/listen" => HandleListen(body),
            "/api/agent/say" => HandleSay(body),
            "/api/agent/act" => HandleAct(body),
            "/api/agent/recall" => HandleRecall(body),
            "/api/agent/recipes" => HandleRecipes(body),
            "/api/agent/where_is" => HandleWhereIs(body),
            _ => throw new Exception($"unknown endpoint: {path}")
        };
    }

    // ---- Auth ----

    private bool CheckAuth(IStatusHandlerContext context)
    {
        if (!context.RequestHeaders.TryGetValue("Authorization", out var header))
            return false;

        var value = header.ToString();
        var space = value.IndexOf(' ');
        if (space == -1)
            return false;

        var scheme = value[..space];
        var token = value[space..].Trim();
        if (!string.Equals(scheme, "Bearer", StringComparison.OrdinalIgnoreCase))
            return false;

        return CryptographicOperations.FixedTimeEquals(
            Encoding.UTF8.GetBytes(token),
            Encoding.UTF8.GetBytes(_token));
    }

    // ---- Handlers ----

    private object HandleTools()
    {
        var tools = _specs.Select(s => new
        {
            name = s.Name,
            description = s.Description,
            inputSchema = s.InputSchema
        }).ToArray();
        return new { ok = true, tools };
    }

    private object HandleObserve(JsonElement body)
    {
        if (!TryResolveAgent(body, out var agent, out _, out var err))
            return new { error = err };

        string? filter = null;
        if (body.TryGetProperty("filter", out var fEl) && fEl.ValueKind == JsonValueKind.String)
            filter = fEl.GetString();

        var selfMap = Transform(agent).MapPosition;
        var found = new List<object>();

        foreach (var ent in _lookup.GetEntitiesInRange(agent, ObserveRange))
        {
            if (ent == agent) continue;
            if (_container.IsEntityInContainer(ent)) continue;
            if (HasComp<SubFloorHideComponent>(ent)) continue;
            if (HasComp<AudioComponent>(ent)) continue;
            if (HasComp<GhostRoleMobSpawnerComponent>(ent)
                || HasComp<ConditionalSpawnerComponent>(ent)
                || HasComp<RandomSpawnerComponent>(ent)
                || HasComp<TimedSpawnerComponent>(ent))
                continue;
            if (!TryComp<MetaDataComponent>(ent, out var meta) || string.IsNullOrWhiteSpace(meta.EntityName))
                continue;

            var pos = Transform(ent).MapPosition;
            if (pos.MapId != selfMap.MapId) continue;
            if (!_examine.InRangeUnOccluded(agent, ent, ObserveRange)) continue;

            var delta = pos.Position - selfMap.Position;
            found.Add(new
            {
                id = GetNetEntity(ent).Id,
                name = meta.EntityName,
                category = Category(ent),
                tool = ToolQualities(ent),
                dist = Math.Round(delta.Length(), 1),
                dir = DirText(delta)
            });
        }

        if (!string.IsNullOrEmpty(filter))
        {
            var terms = filter.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Select(t => t.ToLowerInvariant()).ToArray();
            found = found.Where(f => terms.Any(t => ((dynamic)f).name.ToString().Contains(t, StringComparison.OrdinalIgnoreCase))).ToList();
        }

        found = found.OrderBy(f => ((dynamic)f).dist).Take(ObserveMax).ToList();
        return new { ok = true, count = found.Count, entities = found };
    }

    private object HandleListen(JsonElement body)
    {
        if (!TryResolveAgent(body, out var agent, out _, out var err))
            return new { error = err };

        if (!TryComp<SaigaHearingComponent>(agent, out var hearing) || hearing.Lines.Count == 0)
            return new { ok = true, heard = Array.Empty<object>() };

        var unread = hearing.Lines.Where(l => !l.Read).ToList();
        foreach (var l in hearing.Lines)
            l.Read = true;

        var now = _timing.CurTime;
        var lines = unread.Select(l => new
        {
            speakerId = l.Net.Id,
            speakerName = l.Speaker,
            text = l.Text,
            secondsAgo = (int)(now - l.Time).TotalSeconds
        }).ToArray();

        return new { ok = true, count = lines.Length, heard = lines };
    }

    private object HandleSay(JsonElement body)
    {
        if (!TryResolveAgent(body, out var agent, out var session, out var err))
            return new { error = err };

        string? text = null;
        if (body.TryGetProperty("text", out var tEl) && tEl.ValueKind == JsonValueKind.String)
            text = tEl.GetString();

        if (string.IsNullOrEmpty(text))
            return new { error = "missing 'text' field" };

        RaiseNetworkEvent(new SaigaAgentDecisionResponseEvent(text, "none", null, null), session);
        _saiga.LogTranscript("api", Name(agent), null, text, "say", null);

        return new { ok = true, said = text };
    }

    private object HandleAct(JsonElement body)
    {
        if (!TryResolveAgent(body, out var agent, out var session, out var err))
            return new { error = err };

        if (!body.TryGetProperty("action", out var aEl) || aEl.ValueKind != JsonValueKind.String)
            return new { error = "missing 'action' field" };

        var action = aEl.GetString()!;

        NetEntity? target = null;
        if (body.TryGetProperty("target", out var targetEl))
        {
            if (targetEl.ValueKind == JsonValueKind.Number && targetEl.TryGetInt32(out var tid))
                target = new NetEntity(tid);
            else if (targetEl.ValueKind == JsonValueKind.String && int.TryParse(targetEl.GetString(), out tid))
                target = new NetEntity(tid);
        }

        string? recipeArg = null;
        if (body.TryGetProperty("recipe", out var rEl) && rEl.ValueKind == JsonValueKind.String)
            recipeArg = rEl.GetString();

        // For store, resolve bag entity
        if (action == "store")
        {
            if (!_inventory.TryGetSlotEntity(agent, "back", out var bag))
                return new { error = "у агента нет сумки в слоте back" };
            target = GetNetEntity(bag.Value);
        }

        RaiseNetworkEvent(new SaigaAgentDecisionResponseEvent(null, action, target, recipeArg), session);
        _saiga.LogTranscript("api", Name(agent), null, null, action, target?.ToString() ?? recipeArg);

        return new { ok = true, action, target = target?.Id, recipe = recipeArg };
    }

    private object HandleRecall(JsonElement body)
    {
        if (!TryResolveAgent(body, out var agent, out _, out var err))
            return new { error = err };

        string? query = null;
        if (body.TryGetProperty("query", out var qEl) && qEl.ValueKind == JsonValueKind.String)
            query = qEl.GetString();

        if (!TryComp<SaigaMemoryComponent>(agent, out var mem) || mem.Nodes.Count == 0)
            return new { ok = true, count = 0, nodes = Array.Empty<object>() };

        var now = _timing.CurTime;
        var nodes = mem.Nodes.Values
            .Where(n => string.IsNullOrEmpty(query) || n.Name.Contains(query!, StringComparison.OrdinalIgnoreCase))
            .OrderBy(n => (now - n.LastSeen))
            .Take(RecallMax)
            .Select(n => new
            {
                id = n.Net.Id,
                name = n.Name,
                category = n.Category,
                tool = n.Tool,
                secondsAgo = (int)(now - n.LastSeen).TotalSeconds,
                near = NearNames(mem, n)
            })
            .ToArray();

        return new { ok = true, count = nodes.Length, nodes };
    }

    private object HandleRecipes(JsonElement body)
    {
        string? query = null;
        if (body.TryGetProperty("query", out var qEl) && qEl.ValueKind == JsonValueKind.String)
            query = qEl.GetString();

        var list = new List<object>();
        foreach (var p in _proto.EnumeratePrototypes<ConstructionPrototype>())
        {
            if (p.Hide) continue;
            if (!string.IsNullOrEmpty(query)
                && !p.Name.Contains(query!, StringComparison.OrdinalIgnoreCase)
                && !p.ID.Contains(query!, StringComparison.OrdinalIgnoreCase)
                && !p.Category.Contains(query!, StringComparison.OrdinalIgnoreCase))
                continue;

            list.Add(new { id = p.ID, name = p.Name, type = p.Type == ConstructionType.Item ? "item" : "structure", category = p.Category });
            if (list.Count >= RecipesMax) break;
        }

        return new { ok = true, count = list.Count, recipes = list };
    }

    private object HandleWhereIs(JsonElement body)
    {
        if (!TryResolveAgent(body, out var agent, out _, out var err))
            return new { error = err };

        if (!body.TryGetProperty("name", out var nEl) || nEl.ValueKind != JsonValueKind.String)
            return new { error = "missing 'name' field" };

        var name = nEl.GetString()!;

        if (!TryComp<SaigaMemoryComponent>(agent, out var mem) || mem.Nodes.Count == 0)
            return new { ok = true, found = false, message = "Память пуста" };

        var now = _timing.CurTime;
        var match = mem.Nodes.Values
            .Where(n => n.Name.Contains(name, StringComparison.OrdinalIgnoreCase))
            .OrderBy(n => (now - n.LastSeen))
            .FirstOrDefault();

        if (match == null)
            return new { ok = true, found = false, message = $"Не помню «{name}»" };

        var selfPos = Transform(agent).MapPosition;
        string place;
        if (selfPos.MapId == match.MapId)
        {
            var delta = match.Pos - selfPos.Position;
            place = $"~{delta.Length():F1}м на {DirText(delta)}";
        }
        else
        {
            place = "на другой карте";
        }

        return new
        {
            ok = true, found = true, id = match.Net.Id, name = match.Name,
            category = match.Category, place,
            secondsAgo = (int)(now - match.LastSeen).TotalSeconds,
            near = NearNames(mem, match)
        };
    }

    // ---- Helpers ----

    private string Category(EntityUid e)
        => HasComp<ActorComponent>(e) ? "person"
            : HasComp<MobStateComponent>(e) ? "creature"
            : HasComp<ToolComponent>(e) ? "tool"
            : HasComp<ItemComponent>(e) ? "item"
            : "object";

    private string ToolQualities(EntityUid e)
    {
        if (!TryComp<ToolComponent>(e, out var tool) || tool.HideQualities)
            return string.Empty;
        return string.Join(",", tool.Qualities);
    }

    private static string NearNames(SaigaMemoryComponent mem, MemNode node)
    {
        var names = new List<string>();
        foreach (var net in node.Near)
        {
            if (mem.Nodes.TryGetValue(net, out var n))
                names.Add(n.Name);
            if (names.Count >= 4) break;
        }
        return string.Join(", ", names);
    }

    private bool TryResolveAgent(JsonElement body, out EntityUid agent, out ICommonSession session, out string error)
    {
        agent = default;
        session = default!;
        error = string.Empty;

        if (!body.TryGetProperty("agent", out var aEl))
        {
            error = "missing 'agent' field";
            return false;
        }

        EntityUid? uid = null;
        if (aEl.ValueKind == JsonValueKind.Number && aEl.TryGetInt32(out var idn))
            uid = GetEntity(new NetEntity(idn));
        else if (aEl.ValueKind == JsonValueKind.String)
        {
            var s = aEl.GetString()!;
            uid = int.TryParse(s, out var ids) ? GetEntity(new NetEntity(ids)) : FindAgentByName(s);
        }

        if (uid is not { } u || !Exists(u))
        {
            error = "agent not found";
            return false;
        }

        if (!TryComp<ActorComponent>(u, out var actor))
        {
            error = "entity has no ActorComponent";
            return false;
        }

        agent = u;
        session = actor.PlayerSession;
        return true;
    }

    private EntityUid? GetEntity(NetEntity net)
    {
        return TryGetEntity(net, out var uid) ? uid : null;
    }

    private EntityUid? FindAgentByName(string name)
    {
        var query = EntityQueryEnumerator<SaigaAgentStateComponent, ActorComponent>();
        while (query.MoveNext(out var uid, out _, out _))
        {
            if (string.Equals(Name(uid), name, StringComparison.OrdinalIgnoreCase))
                return uid;
        }
        return null;
    }

    private static string DirText(Vector2 d)
    {
        if (d.LengthSquared() < 0.01f) return "here";
        var ang = (MathF.Atan2(d.Y, d.X) * 180f / MathF.PI + 360f) % 360f;
        string[] names = { "E", "NE", "N", "NW", "W", "SW", "S", "SE" };
        return names[(int)MathF.Round(ang / 45f) % 8];
    }

    private async Task<T> RunOnMainThread<T>(Func<T> func)
    {
        var tcs = new TaskCompletionSource<T>();
        _taskManager.RunOnMainThread(() =>
        {
            try { tcs.TrySetResult(func()); }
            catch (Exception e) { tcs.TrySetException(e); }
        });
        return await tcs.Task;
    }
}
