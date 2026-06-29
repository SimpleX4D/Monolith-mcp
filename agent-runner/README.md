# Saiga MCP Agent Runner

Система для управления игровым агентом в SS14 через LM Studio / любой LLM.

## Архитектура

```
LM Studio (модель)
    ↓ MCP (stdio протокол)
Python MCP Proxy (mcp_server.py)
    ↓ HTTP POST (простой JSON)
C# SS14 Server (SaigaApiHandler.cs)
    → /api/agent/ping, /api/agent/tools, /api/agent/observe,
      /api/agent/listen, /api/agent/say, /api/agent/act,
      /api/agent/recall, /api/agent/recipes, /api/agent/where_is
    ↓
    Игровой клиент выполняет действия
```

## Быстрый старт

### 1. Запусти SS14 сервер

```bash
cd C:\Users\SimpleX\Desktop\serverass\server\Monolith
dotnet run --project Content.Server
```

Saiga API автоматически включён через конфиг-пресет `Build/development.toml`.

### 2. Зайди в игру и включи агента

1. Подключись к серверу через клиент SS14
2. Выбери персонажа, войди в раунд
3. Открой консоль (F3) и выполни:
   ```
   saiga_agent on
   ```
4. Узнай ID своего персонажа (покажи в игре через Tab или другие UI)

### 3. Запусти Python MCP прокси

Прокси нужно запустить, указав ID агента:

```bash
cd C:\Users\SimpleX\Desktop\serverass\server\Monolith
python agent-runner/mcp_server.py --ss14-url http://127.0.0.1:1212 --ss14-token devsecret --agent 4107
```

Где `4107` — ID твоего агента в игре.

### 4. Подключи LM Studio

Самый простой способ — отправить запрос к LM Studio с ephemeral MCP сервером:

```bash
curl http://localhost:1234/api/v1/chat \
  -H "Authorization: Bearer sk-lm-7ditUxo3:hgnekkXTRqCCYJ4dkxQU" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "local-model",
    "input": "Осмотрись вокруг и скажи, что ты видишь",
    "integrations": [
      {
        "type": "ephemeral_mcp",
        "server_label": "ss14-agent",
        "server_url": "http://127.0.0.1:1212/mcp",
        "allowed_tools": ["observe", "say", "follow", "stop", "listen"],
        "headers": {
          "Authorization": "Bearer devsecret"
        }
      }
    ],
    "context_length": 8000
  }'
```

### 5. Или настрой mcp.json в LM Studio

Создай/отредактируй `mcp.json` в LM Studio:

```json
{
  "mcp/saiga-agent": {
    "command": "python",
    "args": [
      "C:\\Users\\SimpleX\\Desktop\\serverass\\server\\Monolith\\agent-runner\\mcp_server.py",
      "--ss14-url", "http://127.0.0.1:1212",
      "--ss14-token", "devsecret",
      "--agent", "4107"
    ]
  }
}
```

После этого в LM Studio можно будет использовать инструменты агента (observe, say, follow и т.д.) через MCP.

### 6. Используй headless runner (автономный режим)

Если не хочешь подключать LM Studio и хочешь, чтобы агент работал через локальную LLM (Ollama / LM Studio):

```bash
python agent-runner/runner.py --agent 4107 --token devsecret --model local-model --backend openai --backend-url http://localhost:1234/v1 --goal "Осмотрись и веди себя как обычный член экипажа."
```

## Тестирование

```bash
# Тесты без агента (только ping, tools, auth, ошибки)
python agent-runner\tests\test_api.py

# Тесты с агентом
python agent-runner\tests\test_api.py --agent 4107
