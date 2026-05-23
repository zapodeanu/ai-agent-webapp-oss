# AI Agent Webapp PoC

> Note: This project code was created with Cursor using Codex 5.3.

Python-first proof-of-concept web app with:

- Browser chat UI
- Server that hosts the frontend and API
- Persistent multi-chat management in SQLite (create/select/delete/clear)
- Chat rename support
- Single local user model stored in DB (preparation for multi-user)
- Agent connected to AWS Bedrock
- MCP tool execution through a separate collocated MCP client layer
- Streaming token output in chat UI
- Status badges for agent mode and MCP reachability
- Clear conversation button

No user authentication is included (per PoC scope).

Note: the app supports either a single MCP server endpoint or multiple named MCP servers.

## Quickstart (5 minutes)

Use this path if you want the app running quickly before exploring details.

### Prerequisites

- macOS or Linux shell environment
- Python 3.11+
- `make`
- OpenSSL CLI (`openssl`)
- `nginx` (required for `make up` flow)
- Optional: AWS CLI credentials if you want live Bedrock responses

### Quickstart steps

1) Clone and enter repo:

```bash
git clone <your-public-repo-url>
cd ai-agent-webapp-oss
```

2) Create local env file from template:

```bash
cp environment.env.example environment.env
```

3) Set minimum required values in `environment.env`:

- `HTTPS_PORT=3000`
- `CHAT_DB_PATH=data/chat.db`
- `APP_USER_ID=local-user`
- `APP_USER_NAME=Local User`

4) Install dependencies:

```bash
pip install -r requirements.txt
```

5) Start app stack:

```bash
make up
```

6) Validate:

```bash
make status
curl -k -i https://localhost:3000/api/status
```

7) Open in browser:

- https://localhost:3000

### Expected results

- Browser UI loads over HTTPS on port 3000.
- `GET /api/status` returns HTTP `200`.
- You can create chats and see persisted history across restarts.
- If Bedrock is not configured yet, app still runs in non-Bedrock mode.

## Project structure

- `app/web_app_server.py` - FastAPI app, static hosting, chat API
- `app/agent/bedrock_agent.py` - Bedrock agent logic
- `app/mcp/mcp_transport_client.py` - MCP JSON-RPC transport client (HTTP/stdio)
- `static/index.html` + `static/app.js` - browser UI

## Configure

1. Create env file:

   ```bash
   cp environment.env.example environment.env
   ```

2. Set values in `environment.env`:

   - `HTTPS_PORT` (where your TLS app listens)
   - `ENABLE_HSTS` (`true` by default)
   - `CHAT_DB_PATH` (SQLite database path, default `data/chat.db`)
   - `APP_USER_ID` (current local user id, default `local-user`)
   - `APP_USER_NAME` (current local user display name)
   - `AWS_REGION`
   - `BEDROCK_MODEL_ID`
   - `MCP_SERVER_URL` (single-server mode, streamable HTTP)
   - `MCP_SERVER_NAME` (single-server display name, default `catalyst-center`)
   - `MCP_HEALTH_URL` (single-server optional health endpoint)
   - `MCP_SERVERS_JSON` (multi-server mode; object or array of named servers)
   - `MCP_API_KEY` (optional; sent as `X-API-Key` to remote MCP servers)
   - `MCP_VERIFY_TLS` (`true` by default)
   - `MCP_CA_CERT` (optional custom CA path)

`environment.env` is loaded first, then `.env` is loaded as a fallback.

Chat data is persisted in SQLite at `CHAT_DB_PATH`, so chats survive backend restarts.

### MCP transport options

Each server in `MCP_SERVERS_JSON` can define one of:

- `transport: "streamable_http"` with `url` (and optional `health_url`)
- `transport: "stdio"` with `command` and optional `args`, `cwd`, `env`

Example mixed configuration:

```json
[
  {
    "name": "remote-http",
    "transport": "streamable_http",
    "url": "http://127.0.0.1:8001/mcp/",
    "health_url": "http://127.0.0.1:8001/health",
    "headers": {
      "X-API-Key": "${MCP_API_KEY}"
    }
  },
  {
    "name": "local-stdio",
    "transport": "stdio",
    "command": "python",
    "args": ["-m", "my_mcp_server"]
  }
]
```

Remote MCP auth:

- If `MCP_API_KEY` is set, the webapp MCP client automatically sends `X-API-Key` on remote MCP requests.
- You can override/add per-server headers explicitly via `MCP_SERVERS_JSON[*].headers`.

## Run

## Make Workflow (Recommended)

From project root:

```bash
make up
```

`make up` now performs a restart sequence:
- stops existing webapp processes
- starts webapp (nginx + backend)

Useful targets:

```bash
make down        # stop webapp stack
make status      # check webapp status
```

Use your existing virtual environment in this folder:

```bash
source .venv/bin/activate
pip install -U pip
pip install -e .
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes \
  -keyout certs/local-dev.key \
  -out certs/local-dev.crt \
  -subj "/CN=localhost"
uvicorn app.web_app_server:app --host 0.0.0.0 --port 3000 --reload \
  --ssl-keyfile certs/local-dev.key \
  --ssl-certfile certs/local-dev.crt
```

Open:

- https://localhost:3000

HTTP redirect listener is intentionally disabled in this setup. Use HTTPS directly.

## Local nginx reverse proxy test

1) Install nginx (macOS/Homebrew):

```bash
brew install nginx
```

2) Start app behind nginx (single command):

```bash
bash scripts/start_with_nginx.sh
```

This starts:
- backend on `127.0.0.1:3001` (HTTP, internal only)
- nginx HTTPS proxy on `https://localhost:3000`

Override ports if needed:

```bash
BACKEND_PORT=3101 HTTPS_PROXY_PORT=3443 bash scripts/start_with_nginx.sh
```

## Enable AWS Bedrock

1) In `environment.env`:

```bash
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=anthropic.claude-sonnet-4-20250514-v1:0
```

2) Configure AWS credentials on the host (example):

```bash
aws configure
```

3) Restart backend and verify `/api/status` returns:
- `agent_mode=bedrock`
- `bedrock_enabled=true`
- `bedrock_configured=true`

## HTTPS behavior

- HTTPS is the only frontend listener in this setup.
- HSTS is enabled when `ENABLE_HSTS=true`.
- MCP HTTPS uses TLS verification by default (`MCP_VERIFY_TLS=true`).
- For self-signed MCP certs in dev, set `MCP_CA_CERT` to your CA file.

## Tool execution format

For baseline PoC tool testing from chat input:

```text
/tool <tool_name> {"key":"value"}
```

The server forwards this to MCP using JSON-RPC `tools/call`.
Use the **MCP Tools** panel in the UI (`Refresh tools`) to verify tool discovery from your local MCP server.
When multiple MCP servers are configured:
- Use `server::tool_name` to target one server explicitly.
- For duplicate tool names, calling unqualified `tool_name` fans out to all matching servers.
- You can force a server with tool args using `{"__mcp_server":"server-name"}`.

## New API endpoints

- `GET /api/health` - liveness check
- `GET /api/status` - current agent mode and MCP connectivity
- `GET /api/me` - current user identity
- `GET /api/mcp/tools` - list merged tools from all configured MCP servers
- `GET /api/mcp/tools?server=<name>` - list tools for one specific MCP server
- `GET /api/chats` - list chats
- `POST /api/chats` - create chat
- `GET /api/chats/{chat_id}` - get one chat + conversation
- `PATCH /api/chats/{chat_id}` - rename a chat
- `DELETE /api/chats/{chat_id}` - delete a chat
- `POST /api/chats/{chat_id}/clear` - clear a specific chat
- `POST /api/chat/clear` - clears the in-memory conversation
- `POST /api/chat` - non-streaming chat response
- `POST /api/chat/stream` - Server-Sent Events token stream

## Logging

- Default consolidated log file: `logs/ai_agent_webapp.log`
- Launcher + backend + nginx entries are written to the same file.
- Uvicorn access noise is reduced so nginx access lines are the primary request log source.

## Sanity Check

```bash
make up
make status
curl -k -i https://localhost:3000/api/status
```

Expected:
- HTTP `200` from `/api/status`
- MCP server connectivity is reported in `/api/status` when configured

## Troubleshooting

- Port conflict (`3000` already in use): set `HTTPS_PORT` to another value in `environment.env`, then rerun `make up`.
- `make up` fails because `nginx` is missing: install it (`brew install nginx` on macOS) and retry.
- TLS warning in browser: expected for local/self-signed certs; proceed in local dev only.
- `bedrock_configured=false`: set `AWS_REGION` and `BEDROCK_MODEL_ID`, then run `aws configure`.
- MCP tools not visible: verify `MCP_SERVER_URL` or `MCP_SERVERS_JSON` values, then use the UI "Refresh tools" button.

## Scope and publishing notes

- This project is a PoC/demo and is not production-hardened.
- Do not commit `environment.env`; keep secrets only in local env files or a secret manager.
- This public repo intentionally uses `environment.env.example` as the safe template.

## License

This repository is distributed under the terms in `LICENSE` (Cisco Sample Code License 1.1).
See `NOTICE` for copyright information.
