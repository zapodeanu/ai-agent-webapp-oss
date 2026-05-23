from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent.bedrock_agent import BedrockAgent
from app.mcp.mcp_transport_client import McpToolClient, MultiMcpClient, create_mcp_client
from app.storage.chat_store import ChatStore, DEFAULT_CHAT_ID

project_root = Path(__file__).resolve().parent.parent
load_dotenv(project_root / "environment.env")
load_dotenv(project_root / ".env")

APP_LOG_LEVEL = (os.getenv("APP_LOG_LEVEL") or os.getenv("MCP_LOG_LEVEL") or "INFO").upper()
APP_LOG_FORMAT = (
    os.getenv("APP_LOG_FORMAT")
    or os.getenv("MCP_LOG_FORMAT")
    or "%(asctime)s - PID:%(process)d - %(name)s - %(levelname)s - %(message)s"
)

logging.basicConfig(
    level=getattr(logging, APP_LOG_LEVEL, logging.INFO),
    format=APP_LOG_FORMAT,
)
logger = logging.getLogger("ai_agent_webapp")
# Keep backend logs concise: nginx access logs already capture requests.
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("botocore.credentials").setLevel(logging.WARNING)


def as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


PORT = int(os.getenv("PORT", "3000"))
AWS_REGION = os.getenv("AWS_REGION")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL")
ENABLE_HSTS = as_bool(os.getenv("ENABLE_HSTS"), default=True)
MCP_VERIFY_TLS = as_bool(os.getenv("MCP_VERIFY_TLS"), default=True)
MCP_CA_CERT = os.getenv("MCP_CA_CERT")
MCP_HEALTH_URL = os.getenv("MCP_HEALTH_URL")
MCP_API_KEY = (os.getenv("MCP_API_KEY") or "").strip()
CHAT_DB_PATH = os.getenv("CHAT_DB_PATH", str(project_root / "data" / "chat.db"))
APP_USER_ID = os.getenv("APP_USER_ID", "local-user")
APP_USER_NAME = os.getenv("APP_USER_NAME", "Local User")
MCP_SERVER_NAME = os.getenv("MCP_SERVER_NAME", "catalyst-center")


def load_mcp_server_configs() -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    raw_servers = os.getenv("MCP_SERVERS_JSON", "").strip()

    def with_default_mcp_headers(input_headers: Any) -> dict[str, str]:
        headers: dict[str, str] = {}
        if isinstance(input_headers, dict):
            headers = {str(key): str(value) for key, value in input_headers.items()}
        if MCP_API_KEY and "x-api-key" not in {key.lower() for key in headers}:
            headers["X-API-Key"] = MCP_API_KEY
        return headers

    if raw_servers:
        try:
            parsed = json.loads(raw_servers)
        except json.JSONDecodeError as exc:
            raise RuntimeError("MCP_SERVERS_JSON must be valid JSON.") from exc

        if isinstance(parsed, list):
            for idx, item in enumerate(parsed):
                if not isinstance(item, dict):
                    continue
                transport = str(item.get("transport", "streamable_http")).strip() or "streamable_http"
                name = str(item.get("name", f"mcp-{idx + 1}")).strip() or f"mcp-{idx + 1}"
                if transport == "stdio":
                    command = str(item.get("command", "")).strip()
                    if not command:
                        continue
                    args = item.get("args", [])
                    if not isinstance(args, list):
                        args = []
                    cwd = str(item.get("cwd", "")).strip() or None
                    env = item.get("env", {})
                    if not isinstance(env, dict):
                        env = {}
                    configs.append(
                        {
                            "name": name,
                            "transport": "stdio",
                            "command": command,
                            "args": [str(v) for v in args],
                            "cwd": cwd,
                            "env": {str(k): str(v) for k, v in env.items()},
                            "display_endpoint": f"stdio:{command}",
                        }
                    )
                    continue
                url = str(item.get("url", "")).strip()
                if not url:
                    continue
                health_url = str(item.get("health_url", "")).strip() or None
                configs.append(
                    {
                        "name": name,
                        "transport": "streamable_http",
                        "url": url,
                        "health_url": health_url,
                        "headers": with_default_mcp_headers(item.get("headers")),
                        "display_endpoint": url,
                    }
                )
        elif isinstance(parsed, dict):
            for key, item in parsed.items():
                if isinstance(item, str):
                    url = item.strip()
                    if not url:
                        continue
                    configs.append(
                        {
                            "name": str(key).strip() or "mcp",
                            "transport": "streamable_http",
                            "url": url,
                            "health_url": None,
                            "headers": with_default_mcp_headers(None),
                            "display_endpoint": url,
                        }
                    )
                elif isinstance(item, dict):
                    transport = str(item.get("transport", "streamable_http")).strip() or "streamable_http"
                    name = str(item.get("name", key)).strip() or str(key).strip() or "mcp"
                    if transport == "stdio":
                        command = str(item.get("command", "")).strip()
                        if not command:
                            continue
                        args = item.get("args", [])
                        if not isinstance(args, list):
                            args = []
                        cwd = str(item.get("cwd", "")).strip() or None
                        env = item.get("env", {})
                        if not isinstance(env, dict):
                            env = {}
                        configs.append(
                            {
                                "name": name,
                                "transport": "stdio",
                                "command": command,
                                "args": [str(v) for v in args],
                                "cwd": cwd,
                                "env": {str(k): str(v) for k, v in env.items()},
                                "display_endpoint": f"stdio:{command}",
                            }
                        )
                        continue
                    url = str(item.get("url", "")).strip()
                    if not url:
                        continue
                    health_url = str(item.get("health_url", "")).strip() or None
                    configs.append(
                        {
                            "name": name,
                            "transport": "streamable_http",
                            "url": url,
                            "health_url": health_url,
                            "headers": with_default_mcp_headers(item.get("headers")),
                            "display_endpoint": url,
                        }
                    )
        else:
            raise RuntimeError("MCP_SERVERS_JSON must be an object or array.")

    if not configs and MCP_SERVER_URL:
        configs.append(
            {
                "name": MCP_SERVER_NAME,
                "transport": "streamable_http",
                "url": MCP_SERVER_URL,
                "health_url": MCP_HEALTH_URL,
                "headers": with_default_mcp_headers(None),
                "display_endpoint": MCP_SERVER_URL,
            }
        )

    return configs


app = FastAPI(title="Network AI Hub")

static_dir = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

mcp_verify_setting: bool | str = MCP_CA_CERT if MCP_CA_CERT else MCP_VERIFY_TLS
mcp_servers = load_mcp_server_configs()
mcp_clients: dict[str, McpToolClient] = {}
for server in mcp_servers:
    server_name = str(server["name"])
    mcp_clients[server_name] = create_mcp_client(
        server,
        verify_tls=mcp_verify_setting,
    )
primary_mcp_server = str(mcp_servers[0]["name"]) if mcp_servers else None
mcp_router = (
    MultiMcpClient(mcp_clients, default_server=primary_mcp_server) if mcp_servers else None
)

if not AWS_REGION or not BEDROCK_MODEL_ID:
    raise RuntimeError(
        "AWS_REGION and BEDROCK_MODEL_ID environment variables are required."
    )
agent = BedrockAgent(BEDROCK_MODEL_ID, AWS_REGION, mcp_router)
logger.info("agent_mode=bedrock")

chat_store = ChatStore(
    CHAT_DB_PATH,
    default_user_id=APP_USER_ID,
    default_user_name=APP_USER_NAME,
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    chat_id: str | None = None


class CreateChatRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)


class RenameChatRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


async def get_mcp_statuses() -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for server in mcp_servers:
        name = str(server["name"])
        client = mcp_clients.get(name)
        if client is None:
            continue
        connected = False
        error = ""
        try:
            connected = await client.ping()
        except Exception as exc:
            error = str(exc)
            connected = False
        statuses.append(
            {
                "name": name,
                "url": server.get("display_endpoint") or server.get("url") or "",
                "transport": server.get("transport", "streamable_http"),
                "connected": connected,
                "error": error,
            }
        )
    return statuses


def resolve_mcp_client(server_name: str | None = None) -> tuple[str, McpToolClient]:
    if not mcp_servers:
        raise HTTPException(status_code=400, detail="MCP server is not configured")
    if not server_name:
        default_name = str(mcp_servers[0]["name"])
        return default_name, mcp_clients[default_name]

    client = mcp_clients.get(server_name)
    if client is None:
        raise HTTPException(status_code=404, detail=f"unknown MCP server: {server_name}")
    return server_name, client


def agent_mode() -> str:
    return "bedrock"


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    if ENABLE_HSTS:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/api/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    mcp_statuses = await get_mcp_statuses()
    any_mcp_connected = any(bool(item.get("connected")) for item in mcp_statuses)
    connected_count = sum(1 for item in mcp_statuses if bool(item.get("connected")))
    total_servers = len(mcp_statuses)
    primary_server = str(mcp_servers[0]["name"]) if mcp_servers else None
    connected_server = next(
        (
            str(item.get("name", "")).strip()
            for item in mcp_statuses
            if bool(item.get("connected")) and str(item.get("name", "")).strip()
        ),
        None,
    )
    active_server = connected_server or primary_server
    return {
        "user_id": APP_USER_ID,
        "user_name": APP_USER_NAME,
        "agent_mode": agent_mode(),
        "bedrock_enabled": True,
        "bedrock_configured": bool(AWS_REGION and BEDROCK_MODEL_ID),
        "mcp_connected": any_mcp_connected,
        "mcp_configured": bool(mcp_servers),
        "mcp_primary_server": primary_server,
        "mcp_active_server": active_server,
        "mcp_connected_count": connected_count,
        "mcp_total_servers": total_servers,
        "mcp_servers": mcp_statuses,
    }


@app.get("/api/me")
async def me() -> dict[str, str]:
    user = chat_store.get_user(APP_USER_ID)
    if user is None:
        raise HTTPException(status_code=500, detail="user not initialized")
    return user


@app.get("/api/mcp/tools")
async def mcp_tools(server: str | None = None) -> dict[str, Any]:
    if mcp_router is None:
        raise HTTPException(status_code=400, detail="MCP server is not configured")

    try:
        if server:
            selected_name, _client = resolve_mcp_client(server)
            return {
                "server": selected_name,
                "tools": await mcp_router.list_tools_by_server(selected_name),
            }
        return {
            "server": "all",
            "tools": await mcp_router.list_tools(),
        }
    except Exception as exc:
        logger.exception("mcp_tools_list_failed: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to fetch MCP tools") from exc


@app.get("/api/chats")
async def list_chats() -> dict[str, Any]:
    return {"chats": chat_store.list_chats(APP_USER_ID)}


@app.post("/api/chats")
async def create_chat(payload: CreateChatRequest) -> dict[str, Any]:
    return {"chat": chat_store.create_chat(APP_USER_ID, payload.title)}


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str) -> dict[str, Any]:
    chat = chat_store.get_chat_summary(APP_USER_ID, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="chat not found")
    return {"chat": chat, "conversation": chat_store.get_messages(APP_USER_ID, chat_id)}


@app.patch("/api/chats/{chat_id}")
async def rename_chat(chat_id: str, payload: RenameChatRequest) -> dict[str, bool]:
    renamed = chat_store.rename_chat(APP_USER_ID, chat_id, payload.title)
    if not renamed:
        raise HTTPException(status_code=404, detail="chat not found")
    return {"ok": True}


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str) -> dict[str, bool]:
    deleted = chat_store.delete_chat(APP_USER_ID, chat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="chat not found")
    return {"ok": True}


@app.post("/api/chats/{chat_id}/clear")
async def clear_chat_id(chat_id: str) -> dict[str, bool]:
    cleared = chat_store.clear_chat(APP_USER_ID, chat_id)
    if not cleared:
        raise HTTPException(status_code=404, detail="chat not found")
    return {"ok": True}


@app.post("/api/chat/clear")
async def clear_chat() -> dict[str, bool]:
    chat_store.clear_chat(APP_USER_ID, DEFAULT_CHAT_ID)
    return {"ok": True}


@app.post("/api/chat")
async def chat(payload: ChatRequest) -> dict[str, Any]:
    chat_id = (payload.chat_id or DEFAULT_CHAT_ID).strip()
    if not chat_id:
        chat_id = DEFAULT_CHAT_ID

    user_text = payload.message.strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="message is required")

    chat_store.get_or_create_chat(APP_USER_ID, chat_id)
    try:
        chat_store.append_message(APP_USER_ID, chat_id, "user", user_text)
        history = chat_store.get_messages(APP_USER_ID, chat_id)
        reply = await agent.reply(history)
        chat_store.append_message(APP_USER_ID, chat_id, "assistant", reply)
        conversation = chat_store.get_messages(APP_USER_ID, chat_id)

        logger.info(
            "chat_response_generated",
            extra={"conversation_length": len(conversation), "chat_id": chat_id},
        )
        return {"reply": reply, "conversation": conversation, "chat_id": chat_id}
    except Exception as exc:  # baseline PoC error handling
        logger.exception("chat_failed: %s", exc)
        raise HTTPException(status_code=500, detail="chat request failed") from exc


@app.post("/api/chat/stream")
async def chat_stream(payload: ChatRequest) -> StreamingResponse:
    chat_id = (payload.chat_id or DEFAULT_CHAT_ID).strip()
    if not chat_id:
        chat_id = DEFAULT_CHAT_ID

    user_text = payload.message.strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="message is required")

    chat_store.get_or_create_chat(APP_USER_ID, chat_id)

    async def event_gen():
        try:
            chat_store.append_message(APP_USER_ID, chat_id, "user", user_text)
            history = chat_store.get_messages(APP_USER_ID, chat_id)
            full_reply = ""
            async for chunk in agent.stream_reply(history):
                if isinstance(chunk, dict):
                    chunk_type = str(chunk.get("type", ""))
                    if chunk_type == "status":
                        text = str(chunk.get("text", "")).strip()
                        if text:
                            yield f"data: {json.dumps({'type': 'status', 'text': text})}\n\n"
                        continue
                    if chunk_type == "token":
                        token_text = str(chunk.get("text", ""))
                        full_reply += token_text
                        yield f"data: {json.dumps({'type': 'token', 'text': token_text})}\n\n"
                        continue

                token_text = str(chunk)
                full_reply += token_text
                yield f"data: {json.dumps({'type': 'token', 'text': token_text})}\n\n"

            chat_store.append_message(APP_USER_ID, chat_id, "assistant", full_reply)
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as exc:
            logger.exception("chat_stream_failed: %s", exc)
            yield (
                f"data: {json.dumps({'type': 'error', 'error': f'chat stream failed: {exc}'})}\n\n"
            )

    return StreamingResponse(event_gen(), media_type="text/event-stream")
