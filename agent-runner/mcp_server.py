#!/usr/bin/env python3
"""
Saiga MCP Proxy Server for LM Studio.

Стартует как MCP сервер (stdio), к которому подключается LM Studio.
Автоматически получает список инструментов из C# API SS14 сервера
и регистрирует их с правильной JSON Schema для LM Studio.

При запуске:
1. Подключается к LM Studio, получает список моделей, берёт первую
2. Подключается к C# API SS14 сервера, получает схему инструментов
3. Регистрирует MCP сервер, который проксирует вызовы в C# API

Использование:
  python mcp_server.py --ss14-url http://127.0.0.1:1212 --ss14-token devsecret
  python mcp_server.py --ss14-url http://127.0.0.1:1212 --ss14-token devsecret --lm-studio-url http://127.0.0.1:1234

Запуск для LM Studio (mcp.json):
  {
    "mcp/saiga-agent": {
      "command": "python",
      "args": ["путь/до/mcp_server.py", "--ss14-url", "http://127.0.0.1:1212", "--ss14-token", "devsecret"]
    }
  }
"""

import argparse
import json
import logging
import sys
import urllib.request
import urllib.error

logging.basicConfig(
    level=logging.INFO,
    format="[mcp-server] %(levelname)s %(message)s",
    stream=sys.stderr
)
log = logging.getLogger("mcp-server")


def http_post(url: str, data: dict, headers: dict | None = None, timeout: int = 30) -> dict:
    """Simple HTTP POST with JSON body."""
    body = json.dumps(data).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        log.error(f"HTTP {e.code} from {url}: {text}")
        raise
    except urllib.error.URLError as e:
        log.error(f"Connection error to {url}: {e.reason}")
        raise


# ---------- SS14 API Client ----------

class Ss14Api:
    """Client for the SS14 Saiga HTTP API."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def _post(self, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = body or {}
        return http_post(url, data, self.headers, timeout=30)

    def ping(self) -> dict:
        return self._post("/api/agent/ping")

    def state(self, agent_id: int) -> dict:
        return self._post("/api/agent/state", {"agent": agent_id})

    def tools(self) -> list[dict]:
        """Get tool list with JSON Schema from SS14 server."""
        resp = self._post("/api/agent/tools")
        return resp.get("tools", [])

    def observe(self, agent_id: int, filter_str: str | None = None) -> dict:
        body = {"agent": agent_id}
        if filter_str:
            body["filter"] = filter_str
        return self._post("/api/agent/observe", body)

    def listen(self, agent_id: int) -> dict:
        return self._post("/api/agent/listen", {"agent": agent_id})

    def say(self, agent_id: int, text: str) -> dict:
        return self._post("/api/agent/say", {"agent": agent_id, "text": text})

    def act(self, agent_id: int, action: str, target: int | None = None,
            text: str | None = None, recipe: str | None = None) -> dict:
        body = {"agent": agent_id, "action": action}
        if target is not None:
            body["target"] = target
        if text is not None:
            body["text"] = text
        if recipe is not None:
            body["recipe"] = recipe
        return self._post("/api/agent/act", body)

    def recall(self, agent_id: int, query: str | None = None) -> dict:
        body = {"agent": agent_id}
        if query:
            body["query"] = query
        return self._post("/api/agent/recall", body)

    def recipes(self, query: str | None = None) -> dict:
        body = {}
        if query:
            body["query"] = query
        return self._post("/api/agent/recipes", body)

    def where_is(self, agent_id: int, name: str) -> dict:
        return self._post("/api/agent/where_is", {"agent": agent_id, "name": name})


# ---------- MCP Protocol (stdio) ----------

class McpServer:
    """
    MCP server communicating over stdio (as required by LM Studio mcp.json).
    Implements the Model Context Protocol for tool listing and calling.
    """

    def __init__(self, ss14: Ss14Api, default_agent: int | None = None):
        self.ss14 = ss14
        self.default_agent = default_agent
        self._tools: list[dict] = []
        self._request_id = 0

    def load_tools(self):
        """Fetch tools from SS14 API and convert to MCP format."""
        raw_tools = self.ss14.tools()
        self._tools = []
        for t in raw_tools:
            schema = t.get("inputSchema", {"type": "object", "properties": {}})
            # LM Studio MCP format expects specific schema structure
            mcp_tool = {
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": schema
            }
            self._tools.append(mcp_tool)
        log.info(f"Loaded {len(self._tools)} tools from SS14 API")

    def _read_request(self) -> dict | None:
        """Read a JSON-RPC request from stdin."""
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            log.warning(f"Invalid JSON from stdin: {line}")
            return None

    def _send_response(self, response: dict):
        """Send a JSON-RPC response to stdout."""
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()

    def _send_event(self, event: dict):
        """Send a JSON-RPC notification/event to stdout."""
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()

    def _handle_initialize(self, req: dict) -> dict:
        """Handle MCP initialize request."""
        params = req.get("params", {})
        client_version = params.get("protocolVersion", "2025-06-18")

        return {
            "jsonrpc": "2.0",
            "id": req.get("id"),
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {
                    "tools": {
                        "listChanged": True
                    }
                },
                "serverInfo": {
                    "name": "saiga-agent-mcp",
                    "version": "1.0.0"
                }
            }
        }

    def _handle_list_tools(self, req: dict) -> dict:
        """Handle tools/list request."""
        return {
            "jsonrpc": "2.0",
            "id": req.get("id"),
            "result": {
                "tools": self._tools
            }
        }

    def _handle_call_tool(self, req: dict) -> dict:
        """Handle tools/call request."""
        params = req.get("params", {})
        name = params.get("name", "")
        arguments = params.get("arguments", {})

        # Auto-inject default agent if not provided
        agent = arguments.get("agent", self.default_agent)

        log.info(f"Tool call: {name}({arguments})")

        try:
            result_text, is_error = self._execute_tool(name, arguments, agent)
            return {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": result_text
                        }
                    ],
                    "isError": is_error
                }
            }
        except Exception as e:
            log.error(f"Tool {name} error: {e}")
            return {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Error: {e}"
                        }
                    ],
                    "isError": True
                }
            }

    def _execute_tool(self, name: str, args: dict, agent: int | None) -> tuple[str, bool]:
        """Execute a tool via SS14 API and return (text, is_error)."""
        if agent is None:
            return "Error: 'agent' parameter is required. Укажите агента.", True

        if name == "observe":
            resp = self.ss14.observe(agent, args.get("filter"))
            entities = resp.get("entities", [])
            if not entities:
                return "Рядом ничего не видно.", False
            lines = ["Вижу рядом (id, имя, расстояние, направление):"]
            for e in entities:
                tool_info = f" [инстр:{e['tool']}]" if e.get("tool") else ""
                lines.append(f"- id={e['id']} {e['name']}{tool_info} ({e['dist']}м, {e['dir']})")
            return "\n".join(lines), False

        elif name == "listen":
            resp = self.ss14.listen(agent)
            heard = resp.get("heard", [])
            if not heard:
                return "Новых реплик нет.", False
            lines = ["Тебе сказали (id, кто: что, как давно):"]
            for l in heard:
                lines.append(f"- id={l['speakerId']} {l['speakerName']}: «{l['text']}» ({l['secondsAgo']}с назад)")
            return "\n".join(lines), False

        elif name == "say":
            text = args.get("text", "")
            if not text:
                return "Error: 'text' parameter required.", True
            resp = self.ss14.say(agent, text)
            return f"Сказал: {text}", False

        elif name == "act":
            action = args.get("action", "")
            target = args.get("target")
            text = args.get("text")
            recipe = args.get("recipe")
            resp = self.ss14.act(agent, action, target, text, recipe)
            if "error" in resp:
                return f"Error: {resp['error']}", True
            return f"ok: action={action}, target={target}", False

        elif name in ("follow", "pickup", "pull", "throw", "move_to", "place", "use_on"):
            target = args.get("target")
            if target is None:
                return f"Error: 'target' required for {name}", True
            resp = self.ss14.act(agent, name, target=target)
            if "error" in resp:
                return f"Error: {resp['error']}", True
            return f"ok: {name} target={target}", False

        elif name in ("stop", "drop", "swap", "build", "activate"):
            resp = self.ss14.act(agent, name)
            if "error" in resp:
                return f"Error: {resp['error']}", True
            return f"ok: {name}", False

        elif name == "store":
            resp = self.ss14.act(agent, "store")
            if "error" in resp:
                return f"Error: {resp['error']}", True
            return f"ok: store", False

        elif name == "craft":
            recipe = args.get("recipe")
            if not recipe:
                return "Error: 'recipe' required for craft", True
            resp = self.ss14.act(agent, "craft", recipe=recipe)
            if "error" in resp:
                return f"Error: {resp['error']}", True
            return f"ok: craft {recipe}", False

        elif name == "construct":
            recipe = args.get("recipe")
            if not recipe:
                return "Error: 'recipe' required for construct", True
            resp = self.ss14.act(agent, "construct", recipe=recipe)
            if "error" in resp:
                return f"Error: {resp['error']}", True
            return f"ok: construct {recipe}", False

        elif name == "recall":
            resp = self.ss14.recall(agent, args.get("query"))
            nodes = resp.get("nodes", [])
            if not nodes:
                return "Память пуста.", False
            lines = [f"Помню {len(nodes)} объект(ов):"]
            for n in nodes:
                tool_info = f" (инстр:{n['tool']})" if n.get("tool") else ""
                near_info = f", рядом: {n['near']}" if n.get("near") else ""
                lines.append(f"- id={n['id']} {n['name']}{tool_info} [{n['category']}], видел {n['secondsAgo']}с назад{near_info}")
            return "\n".join(lines), False

        elif name == "recipes":
            resp = self.ss14.recipes(args.get("query"))
            recipes = resp.get("recipes", [])
            if not recipes:
                return "Рецептов не найдено.", False
            lines = [f"Рецепты ({len(recipes)}):"]
            for r in recipes:
                lines.append(f"- id={r['id']} «{r['name']}» [{r['type']}]")
            return "\n".join(lines), False

        elif name == "where_is":
            name_arg = args.get("name", "")
            if not name_arg:
                return "Error: 'name' required for where_is", True
            resp = self.ss14.where_is(agent, name_arg)
            if resp.get("found"):
                n = resp
                return f"{n['name']} [{n['category']}]: {n['place']}, видел {n['secondsAgo']}с назад. Рядом было: {n['near']}", False
            return resp.get("message", "Не найдено"), False

        else:
            return f"Unknown tool: {name}", True

    def run(self):
        """Main MCP server loop - reads JSON-RPC from stdin, processes, writes to stdout."""
        # Send initialized event
        self._send_event({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {}
        })

        log.info("MCP server running, waiting for requests on stdin...")

        while True:
            req = self._read_request()
            if req is None:
                break

            method = req.get("method", "")

            if method == "initialize":
                resp = self._handle_initialize(req)
            elif method == "notifications/initialized":
                # Ignore
                continue
            elif method == "notifications/cancelled":
                continue
            elif method == "ping":
                resp = {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}
            elif method == "tools/list":
                resp = self._handle_list_tools(req)
            elif method == "tools/call":
                resp = self._handle_call_tool(req)
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "error": {"code": -32601, "message": f"Method not found: {method}"}
                }

            self._send_response(resp)


def get_lm_studio_model(lm_studio_url: str) -> str | None:
    """Get the first available model from LM Studio."""
    url = f"{lm_studio_url.rstrip('/')}/api/v1/models"
    try:
        resp = http_post(url, {"stream": False}, timeout=5)
        data = resp.get("data", [])
        if data:
            model_id = data[0].get("id")
            log.info(f"LM Studio model found: {model_id}")
            return model_id
    except Exception as e:
        log.warning(f"Could not get models from LM Studio: {e}")

    # Try older endpoint
    try:
        req = urllib.request.Request(f"{lm_studio_url.rstrip('/')}/v1/models")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
            models = data.get("data", [])
            if models:
                model_id = models[0].get("id")
                log.info(f"LM Studio model found (v1): {model_id}")
                return model_id
    except Exception as e:
        log.warning(f"Could not get models from LM Studio (v1): {e}")

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Saiga MCP Proxy Server for LM Studio",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  # Запуск с указанием SS14 сервера
  python mcp_server.py --ss14-url http://127.0.0.1:1212 --ss14-token devsecret

  # С агентом по умолчанию (ID 6311)
  python mcp_server.py --ss14-url http://127.0.0.1:1212 --ss14-token devsecret --agent 6311

  # Запуск для LM Studio mcp.json:
  # В mcp.json укажи:
  # {
  #   "mcp/saiga-agent": {
  #     "command": "python",
  #     "args": ["путь/к/mcp_server.py", "--ss14-url", "http://127.0.0.1:1212", "--ss14-token", "devsecret"]
  #   }
  # }
        """
    )
    parser.add_argument("--ss14-url", default="http://127.0.0.1:1212",
                        help="SS14 server API URL (default: http://127.0.0.1:1212)")
    parser.add_argument("--ss14-token", default="devsecret",
                        help="SS14 API Bearer token (default: devsecret)")
    parser.add_argument("--agent", type=int, default=None,
                        help="Default agent net ID (optional)")
    parser.add_argument("--lm-studio-url", default=None,
                        help="LM Studio URL to auto-detect model (optional)")

    args = parser.parse_args()

    # Connect to SS14 API
    log.info(f"Connecting to SS14 API at {args.ss14_url}")
    ss14 = Ss14Api(args.ss14_url, args.ss14_token)

    # Ping to verify connection
    try:
        pong = ss14.ping()
        log.info(f"SS14 API connected: {pong}")
    except Exception as e:
        log.error(f"Cannot connect to SS14 API: {e}")
        log.error("Убедитесь, что SS14 сервер запущен и Saiga API включён.")
        sys.exit(1)

    # Try to detect LM Studio model
    if args.lm_studio_url:
        model = get_lm_studio_model(args.lm_studio_url)
        if model:
            log.info(f"Using LM Studio model: {model}")
        else:
            log.warning("Could not detect LM Studio model, continuing anyway")

    # Start MCP server
    server = McpServer(ss14, default_agent=args.agent)
    server.load_tools()

    log.info(f"MCP server ready! Agent ID: {args.agent or 'not set (required in each call)'}")
    log.info(f"Loaded {len(server._tools)} tools")

    try:
        server.run()
    except KeyboardInterrupt:
        log.info("MCP server stopped by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
