from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse, urlunparse
import uuid
import os
from typing import Any, Protocol

import httpx


class McpToolClient(Protocol):
    async def ping(self) -> bool:
        ...

    async def list_tools(self) -> list[dict[str, Any]]:
        ...

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        ...


class McpClient:
    """
    Minimal MCP JSON-RPC client over HTTP.
    This transport is streamable HTTP-compatible for future scale.
    """

    def __init__(
        self,
        endpoint: str,
        timeout_seconds: float = 10.0,
        verify_tls: bool | str = True,
        health_endpoint: str | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.verify_tls = verify_tls
        self.health_endpoint = health_endpoint or self._derive_health_endpoint(endpoint)
        self.default_headers = dict(default_headers or {})
        self._session_id: str | None = None
        self._session_lock = asyncio.Lock()

    async def ping(self) -> bool:
        """
        Best-effort connectivity check.
        Prefer a generic /health endpoint to avoid generating MCP protocol noise.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                verify=self.verify_tls,
            ) as client:
                response = await client.get(
                    self.health_endpoint,
                    headers={
                        **self.default_headers,
                        "accept": "application/json, text/event-stream",
                    },
                )
            return response.status_code < 500
        except Exception:
            return False

    def _derive_health_endpoint(self, endpoint: str) -> str:
        parsed = urlparse(endpoint)
        # For endpoint paths like /mcp or /mcp/, probe /health.
        health_path = "/health"
        return urlunparse(
            (parsed.scheme, parsed.netloc, health_path, "", "", "")
        )

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method != "initialize":
            await self._ensure_session()

        for attempt in range(2):
            payload = {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": method,
                "params": params or {},
            }

            headers = {
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                **self.default_headers,
            }
            if self._session_id:
                headers["mcp-session-id"] = self._session_id

            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                verify=self.verify_tls,
            ) as client:
                response = await client.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                )

            if response.status_code >= 400:
                if (
                    attempt == 0
                    and method != "initialize"
                    and response.status_code in {400, 401, 403}
                ):
                    # Session may be missing/expired. Reinitialize once and retry.
                    self._session_id = None
                    await self._ensure_session()
                    continue
                response.raise_for_status()

            body = self._parse_jsonrpc_response(response)
            error = body.get("error")
            if error:
                message = str(error.get("message", ""))
                if attempt == 0 and method != "initialize" and (
                    "session" in message.lower() or int(error.get("code", 0)) == -32600
                ):
                    self._session_id = None
                    await self._ensure_session()
                    continue
                raise RuntimeError(f"MCP error {error.get('code')}: {error.get('message')}")
            return body.get("result", {})

        raise RuntimeError("MCP request failed after retry")

    async def _ensure_session(self) -> None:
        if self._session_id:
            return
        async with self._session_lock:
            if self._session_id:
                return
            await self._initialize_session()

    async def _initialize_session(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "mcp-client-webapp",
                    "version": "0.1.0",
                },
            },
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            verify=self.verify_tls,
        ) as client:
            response = await client.post(
                self.endpoint,
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                    **self.default_headers,
                },
                json=payload,
            )

        response.raise_for_status()
        body = self._parse_jsonrpc_response(response)
        error = body.get("error")
        if error:
            raise RuntimeError(
                f"MCP initialize error {error.get('code')}: {error.get('message')}"
            )

        self._session_id = response.headers.get("mcp-session-id")
        if not self._session_id:
            raise RuntimeError("MCP initialize did not return mcp-session-id header")

    def _parse_jsonrpc_response(self, response: httpx.Response) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            parsed = response.json()
            return parsed if isinstance(parsed, dict) else {}

        if "text/event-stream" in content_type:
            text = response.text
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    parsed = json.loads(payload)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    continue
            return {}

        return {}

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._rpc("tools/list")
        tools = result.get("tools", [])
        return tools if isinstance(tools, list) else []

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        return await self._rpc(
            "tools/call",
            {
                "name": name,
                "arguments": args,
            },
        )


class StdioMcpClient:
    """
    MCP JSON-RPC client over stdio transport.
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 10.0,
        framing: str = "jsonl",
    ) -> None:
        self.command = command
        self.args = args or []
        self.cwd = cwd
        self.env = env
        self.timeout_seconds = timeout_seconds
        self.framing = framing.strip().lower() if isinstance(framing, str) else "jsonl"
        self._process: asyncio.subprocess.Process | None = None
        self._started = False
        self._process_lock = asyncio.Lock()
        self._io_lock = asyncio.Lock()

    async def ping(self) -> bool:
        try:
            await self._ensure_started()
            return self._process is not None and self._process.returncode is None
        except Exception:
            return False

    async def _ensure_started(self) -> None:
        if self._started and self._process is not None and self._process.returncode is None:
            return

        async with self._process_lock:
            if self._started and self._process is not None and self._process.returncode is None:
                return

            if self._process is not None and self._process.returncode is not None:
                self._process = None
                self._started = False

            proc_env = os.environ.copy()
            if self.env:
                proc_env.update(self.env)

            self._process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                cwd=self.cwd,
                env=proc_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._started = True

            init_result = await self._rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "mcp-client-webapp",
                        "version": "0.1.0",
                    },
                },
            )
            if not isinstance(init_result, dict):
                raise RuntimeError("MCP stdio initialize returned invalid payload.")

            await self._rpc("notifications/initialized", {})

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method != "initialize":
            await self._ensure_started()

        async with self._io_lock:
            if self._process is None or self._process.stdin is None or self._process.stdout is None:
                raise RuntimeError("MCP stdio process is not available.")
            if self._process.returncode is not None:
                raise RuntimeError(f"MCP stdio process exited with code {self._process.returncode}.")

            payload = {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": method,
                "params": params or {},
            }

            request_id = payload["id"]
            await self._write_message(payload)

            # notifications/* methods are one-way and may not produce responses.
            if str(method).startswith("notifications/"):
                return {}

            while True:
                message = await asyncio.wait_for(self._read_message(), timeout=self.timeout_seconds)
                if not isinstance(message, dict):
                    continue
                if message.get("id") != request_id:
                    # Ignore notifications and unrelated responses.
                    continue
                error = message.get("error")
                if error:
                    raise RuntimeError(f"MCP error {error.get('code')}: {error.get('message')}")
                return message.get("result", {})

    async def _write_message(self, payload: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("MCP stdio stdin is unavailable.")
        body = json.dumps(payload).encode("utf-8")
        if self.framing == "content_length":
            header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            self._process.stdin.write(header + body)
        else:
            # MCP Python stdio servers commonly expect one JSON-RPC object per line.
            self._process.stdin.write(body + b"\n")
        await self._process.stdin.drain()

    async def _read_message(self) -> dict[str, Any]:
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("MCP stdio stdout is unavailable.")

        if self.framing != "content_length":
            while True:
                line = await self._process.stdout.readline()
                if line == b"":
                    raise RuntimeError("MCP stdio stream closed unexpectedly.")
                text = line.decode("utf-8", errors="ignore").strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    # Ignore non-JSON log/output noise on stdout.
                    continue
                return parsed if isinstance(parsed, dict) else {}

        content_length = -1
        while True:
            line = await self._process.stdout.readline()
            if line == b"":
                raise RuntimeError("MCP stdio stream closed unexpectedly.")
            stripped = line.decode("ascii", errors="ignore").strip()
            if not stripped:
                break
            if stripped.lower().startswith("content-length:"):
                value = stripped.split(":", 1)[1].strip()
                try:
                    content_length = int(value)
                except ValueError as exc:
                    raise RuntimeError("Invalid Content-Length from MCP stdio server.") from exc

        if content_length < 0:
            raise RuntimeError("Missing Content-Length from MCP stdio server.")

        body = await self._process.stdout.readexactly(content_length)
        parsed = json.loads(body.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._rpc("tools/list")
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return tools if isinstance(tools, list) else []

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        return await self._rpc(
            "tools/call",
            {
                "name": name,
                "arguments": args,
            },
        )


def create_mcp_client(
    config: dict[str, Any],
    verify_tls: bool | str = True,
    timeout_seconds: float = 10.0,
) -> McpToolClient:
    raw_timeout = config.get("timeout_seconds")
    configured_timeout: float | None = None
    if isinstance(raw_timeout, (int, float)) and float(raw_timeout) > 0:
        configured_timeout = float(raw_timeout)
    elif isinstance(raw_timeout, str):
        try:
            parsed_timeout = float(raw_timeout.strip())
            if parsed_timeout > 0:
                configured_timeout = parsed_timeout
        except ValueError:
            configured_timeout = None

    transport = str(config.get("transport", "streamable_http")).strip().lower()
    if transport in {"streamable_http", "http", "https"}:
        endpoint = str(config.get("url", "")).strip()
        if not endpoint:
            raise ValueError("MCP HTTP transport requires a non-empty url.")
        raw_headers = config.get("headers", {})
        default_headers = (
            {str(key): str(value) for key, value in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else {}
        )
        effective_timeout = configured_timeout if configured_timeout is not None else timeout_seconds
        return McpClient(
            endpoint=endpoint,
            timeout_seconds=effective_timeout,
            verify_tls=verify_tls,
            health_endpoint=config.get("health_url"),
            default_headers=default_headers,
        )

    if transport == "stdio":
        command = str(config.get("command", "")).strip()
        if not command:
            raise ValueError("MCP stdio transport requires a non-empty command.")
        effective_timeout = (
            configured_timeout if configured_timeout is not None else max(timeout_seconds, 60.0)
        )
        raw_args = config.get("args", [])
        args = [str(item) for item in raw_args] if isinstance(raw_args, list) else []
        cwd = str(config.get("cwd", "")).strip() or None
        framing = str(config.get("stdio_framing", "jsonl")).strip() or "jsonl"
        raw_env = config.get("env", {})
        env = (
            {str(key): str(value) for key, value in raw_env.items()}
            if isinstance(raw_env, dict)
            else None
        )
        return StdioMcpClient(
            command=command,
            args=args,
            cwd=cwd,
            env=env,
            timeout_seconds=effective_timeout,
            framing=framing,
        )

    raise ValueError(f"Unsupported MCP transport: {transport}")


class MultiMcpClient:
    """
    Routes MCP tool calls across multiple MCP servers.

    Naming behavior:
    - Unique tool names are exposed as-is.
    - Duplicate tool names are exposed as:
      - aggregate unqualified name: `tool_name` (fan-out across matching servers)
      - explicit targets: `server_name::tool_name`
    """

    def __init__(
        self,
        clients: dict[str, McpToolClient],
        default_server: str | None = None,
    ) -> None:
        self.clients = clients
        self.default_server = default_server
        self._catalog_lock = asyncio.Lock()
        self._catalog_ready = False
        self._tools_by_server: dict[str, list[dict[str, Any]]] = {}
        self._servers_by_tool: dict[str, list[str]] = {}

    async def _ensure_catalog(self) -> None:
        if self._catalog_ready:
            return
        async with self._catalog_lock:
            if self._catalog_ready:
                return
            await self._refresh_catalog()
            self._catalog_ready = True

    async def _refresh_catalog(self) -> None:
        tools_by_server: dict[str, list[dict[str, Any]]] = {}
        servers_by_tool: dict[str, list[str]] = {}

        for server_name, client in self.clients.items():
            try:
                tools = await client.list_tools()
            except Exception:
                tools = []
            normalized_tools = [tool for tool in tools if isinstance(tool, dict)]
            tools_by_server[server_name] = normalized_tools
            for tool in normalized_tools:
                tool_name = str(tool.get("name", "")).strip()
                if not tool_name:
                    continue
                servers_by_tool.setdefault(tool_name, [])
                if server_name not in servers_by_tool[tool_name]:
                    servers_by_tool[tool_name].append(server_name)

        self._tools_by_server = tools_by_server
        self._servers_by_tool = servers_by_tool

    async def list_tools(self) -> list[dict[str, Any]]:
        await self._ensure_catalog()
        merged: list[dict[str, Any]] = []

        for tool_name in sorted(self._servers_by_tool.keys()):
            server_names = self._servers_by_tool.get(tool_name, [])
            if not server_names:
                continue

            first_server = server_names[0]
            first_tool = self._find_server_tool(first_server, tool_name)
            if first_tool is None:
                continue

            if len(server_names) == 1:
                merged.append(first_tool)
                merged.append(self._as_server_specific_tool(first_tool, first_server, tool_name))
                continue

            # Aggregate alias available on multiple servers.
            merged.append(self._as_aggregate_tool(first_tool, tool_name, server_names))
            # Explicit per-server aliases to force a specific target.
            for server_name in server_names:
                specific_tool = self._find_server_tool(server_name, tool_name)
                if specific_tool is None:
                    continue
                merged.append(
                    self._as_server_specific_tool(specific_tool, server_name, tool_name)
                )

        return merged

    async def list_tools_by_server(self, server_name: str) -> list[dict[str, Any]]:
        await self._ensure_catalog()
        return list(self._tools_by_server.get(server_name, []))

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        await self._ensure_catalog()
        call_args = dict(args)
        requested_server = (
            str(call_args.pop("__mcp_server", "")).strip()
            if "__mcp_server" in call_args
            else ""
        )
        raw_name = str(name).strip()
        if not raw_name:
            raise ValueError("Tool name is required.")

        # Explicit server selector via name: "server::tool".
        if "::" in raw_name:
            server_name, tool_name = raw_name.split("::", 1)
            return await self._call_on_server(
                server_name.strip(),
                tool_name.strip(),
                call_args,
            )

        # Explicit server selector via args: {"__mcp_server": "..."}.
        if requested_server:
            return await self._call_on_server(requested_server, raw_name, call_args)

        matching_servers = self._servers_by_tool.get(raw_name, [])
        if not matching_servers:
            await self._refresh_catalog()
            matching_servers = self._servers_by_tool.get(raw_name, [])

        if not matching_servers:
            raise ValueError(f"Unknown MCP tool: {raw_name}")

        if len(matching_servers) == 1:
            return await self._call_on_server(matching_servers[0], raw_name, call_args)

        # Ambiguous tool name across multiple servers: fan out and return grouped results.
        return await self._fanout_call(raw_name, call_args, matching_servers)

    async def _call_on_server(
        self,
        server_name: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> Any:
        if not server_name or not tool_name:
            raise ValueError("Tool call requires both server name and tool name.")
        client = self.clients.get(server_name)
        if client is None:
            raise ValueError(f"Unknown MCP server: {server_name}")
        return await client.call_tool(tool_name, args)

    async def _fanout_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        server_names: list[str],
    ) -> dict[str, Any]:
        async def invoke(server_name: str) -> dict[str, Any]:
            client = self.clients.get(server_name)
            if client is None:
                return {"server": server_name, "ok": False, "error": "server not configured"}
            try:
                result = await client.call_tool(tool_name, dict(args))
                return {"server": server_name, "ok": True, "result": result}
            except Exception as exc:
                return {"server": server_name, "ok": False, "error": str(exc)}

        outcomes = await asyncio.gather(*(invoke(server_name) for server_name in server_names))
        return {
            "tool": tool_name,
            "fanout": True,
            "results": outcomes,
        }

    def _find_server_tool(self, server_name: str, tool_name: str) -> dict[str, Any] | None:
        for tool in self._tools_by_server.get(server_name, []):
            if str(tool.get("name", "")).strip() == tool_name:
                return tool
        return None

    def _as_aggregate_tool(
        self,
        tool: dict[str, Any],
        tool_name: str,
        server_names: list[str],
    ) -> dict[str, Any]:
        copy = dict(tool)
        description = str(copy.get("description", "")).strip()
        server_list = ", ".join(server_names)
        suffix = f" [available on servers: {server_list}]"
        copy["name"] = tool_name
        copy["description"] = (description + suffix).strip()
        return copy

    def _as_server_specific_tool(
        self,
        tool: dict[str, Any],
        server_name: str,
        tool_name: str,
    ) -> dict[str, Any]:
        copy = dict(tool)
        description = str(copy.get("description", "")).strip()
        suffix = f" [target server: {server_name}]"
        copy["name"] = f"{server_name}::{tool_name}"
        copy["description"] = (description + suffix).strip()
        return copy
