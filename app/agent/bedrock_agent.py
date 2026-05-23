from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, Callable

import boto3

from app.mcp.mcp_transport_client import McpToolClient
from app.models import ChatTurn
from app.tools.web_tools import WebTools


class BedrockAgent:
    MAX_TOOL_LOOP_STEPS = 15
    INFERENCE_MAX_TOKENS = 1800

    def __init__(self, model_id: str, region: str, mcp_client: McpToolClient | None) -> None:
        self.model_id = model_id
        self.mcp_client = mcp_client
        self.web_tools = WebTools()
        self.client = boto3.client("bedrock-runtime", region_name=region)
        self.system_prompt = (
            "You are a network operations assistant. "
            "Respond in clean, natural language Markdown with minimal formatting. "
            "Prefer a short paragraph first, then compact bullets if helpful. "
            "Do not use emojis, decorative separators, or verbose template headings. "
            "Do not use Markdown tables by default in this web client. "
            "Use concise prose plus bullets instead. "
            "Only use a table if the user explicitly asks for a table. "
            "Prefer human-friendly identifiers such as hostname, device name, site name, and cluster name. "
            "Avoid listing opaque internal IDs unless the user explicitly asks for IDs or no better identifier exists. "
            "If IDs must be shown, keep them in a short 'IDs' line at the end instead of the main narrative. "
            "If the user asks for 'all clusters' or a network-wide view, ensure coverage across every available cluster "
            "instead of focusing on only one cluster. "
            "When data volume is large, provide per-cluster totals and a few representative examples; "
            "only list every device if the user explicitly asks for a full exhaustive list. "
            "For requests about CVEs/security advisories/news, use web tools to search and fetch source pages. "
            "When tools are used, synthesize a human-language answer first, then include only essential supporting details. "
            "When using tool results, do not invent counts or fields; derive only from provided data. "
            "If data is missing or ambiguous, say so briefly."
        )

    async def reply(self, history: list[ChatTurn]) -> str:
        latest = history[-1] if history else None
        if not latest or latest["role"] != "user":
            return "I need a user message first."

        text = latest["text"].strip()

        # Baseline PoC tool flow:
        # /tool <toolName> {"arg":"value"}
        if text.startswith("/tool "):
            tool_name, result = await self._run_tool_from_message(text)
            return self._format_tool_result(tool_name, result)

        if self.mcp_client is not None or self._has_local_tools():
            try:
                return await self._reply_with_tool_loop(history)
            except Exception:
                # Fall back to plain response if tool orchestration fails.
                return await self._reply_without_tools(history)

        return await self._reply_without_tools(history)

    async def stream_reply(self, history: list[ChatTurn]):
        latest = history[-1] if history else None
        if not latest or latest["role"] != "user":
            yield "I need a user message first."
            return

        text = latest["text"].strip()
        if text.startswith("/tool "):
            yield {"type": "status", "text": "Running tool request..."}
            tool_name, result = await self._run_tool_from_message(text)
            yield self._format_tool_result(tool_name, result)
            return

        if self.mcp_client is not None or self._has_local_tools():
            progress_queue: asyncio.Queue[str] = asyncio.Queue()

            def on_progress(message: str) -> None:
                progress_queue.put_nowait(message)

            task = asyncio.create_task(
                self._reply_with_tool_loop(history, progress_cb=on_progress)
            )

            while not task.done():
                try:
                    update = await asyncio.wait_for(progress_queue.get(), timeout=2.0)
                    yield {"type": "status", "text": update}
                except TimeoutError:
                    yield {"type": "status", "text": "Working on your request..."}

            full = await task
            yield {"type": "status", "text": "Finalizing response..."}
            words = full.split(" ")
            for idx, word in enumerate(words):
                suffix = " " if idx < len(words) - 1 else ""
                yield f"{word}{suffix}"
            return

        messages = [
            {
                "role": turn["role"],
                "content": [{"text": turn["text"]}],
            }
            for turn in history
        ]

        def stream_chunks() -> list[str]:
            response = self.client.converse_stream(
                modelId=self.model_id,
                messages=messages,
                inferenceConfig={
                    "maxTokens": 512,
                    "temperature": 0.4,
                },
            )
            chunks: list[str] = []
            for event in response.get("stream", []):
                delta = event.get("contentBlockDelta", {}).get("delta", {}).get("text")
                if delta:
                    chunks.append(delta)
            return chunks

        chunks = await asyncio.to_thread(stream_chunks)
        if not chunks:
            yield "I could not generate a response."
            return

        for chunk in chunks:
            yield chunk

    async def _run_tool_from_message(self, message: str) -> tuple[str, Any]:
        raw = message.replace("/tool ", "", 1).strip()
        first_space = raw.find(" ")
        if first_space == -1:
            tool_name = raw.strip()
            if not tool_name:
                raise ValueError("Tool format is: /tool <toolName> [jsonArgs]")
            args: dict[str, Any] = {}
            return tool_name, await self._call_any_tool(tool_name, args)
        if first_space == 0:
            raise ValueError("Tool format is: /tool <toolName> [jsonArgs]")

        tool_name = raw[:first_space].strip()
        json_payload = raw[first_space + 1 :].strip()
        if not json_payload:
            return tool_name, await self._call_any_tool(tool_name, {})

        try:
            args = json.loads(json_payload)
            if not isinstance(args, dict):
                raise ValueError("Tool args JSON must be an object.")
        except json.JSONDecodeError as exc:
            raise ValueError("Tool args must be valid JSON.") from exc

        return tool_name, await self._call_any_tool(tool_name, args)

    async def _reply_with_tool_loop(
        self,
        history: list[ChatTurn],
        progress_cb: Callable[[str], None] | None = None,
    ) -> str:
        if self.mcp_client is None and not self._has_local_tools():
            return await self._reply_without_tools(history)

        latest_query = history[-1]["text"] if history else ""
        require_grounding = self._needs_grounded_tooling(latest_query)
        self._emit_progress(progress_cb, "Inspecting available tools...")
        mcp_tools: list[dict[str, Any]] = []
        if self.mcp_client is not None:
            try:
                mcp_tools = await self.mcp_client.list_tools()
            except Exception:
                # Keep local web tools available even if MCP server is temporarily unavailable.
                self._emit_progress(progress_cb, "MCP tools unavailable, continuing with local web tools.")
        mcp_tool_specs, mcp_tool_name_map = self._to_bedrock_tool_specs(
            mcp_tools,
            reserved_names=self._local_tool_names(),
        )
        bedrock_tools = mcp_tool_specs + self._local_tool_specs()
        if not bedrock_tools:
            return await self._reply_without_tools(history)

        messages: list[dict[str, Any]] = [
            {
                "role": turn["role"],
                "content": [{"text": turn["text"]}],
            }
            for turn in history
        ]
        executed_tool_outputs: list[tuple[str, Any]] = []
        tool_used = False

        for step_idx in range(self.MAX_TOOL_LOOP_STEPS):
            tool_config: dict[str, Any] = {"tools": bedrock_tools}
            if require_grounding and not tool_used:
                tool_config["toolChoice"] = {"any": {}}

            self._emit_progress(
                progress_cb,
                f"Reasoning step {step_idx + 1}: asking Bedrock what to do next...",
            )
            response = await asyncio.to_thread(
                self.client.converse,
                modelId=self.model_id,
                messages=messages,
                system=[{"text": self.system_prompt}],
                toolConfig=tool_config,
                inferenceConfig={
                    "maxTokens": self.INFERENCE_MAX_TOKENS,
                    "temperature": 0.4,
                },
            )

            out_message = response.get("output", {}).get("message", {})
            out_content = out_message.get("content", [])
            if not out_content:
                return "I could not generate a response."

            messages.append({"role": "assistant", "content": out_content})

            tool_uses = [item["toolUse"] for item in out_content if "toolUse" in item]
            if not tool_uses:
                if require_grounding and not tool_used:
                    return (
                        "I cannot verify this safely without MCP tool data. "
                        "Please ask me to query the environment data/tools."
                    )
                return self._extract_text(out_content)

            tool_results: list[dict[str, Any]] = []
            for tool_use in tool_uses:
                tool_name = str(tool_use.get("name", "")).strip()
                resolved_tool_name = mcp_tool_name_map.get(tool_name, tool_name)
                tool_use_id = tool_use.get("toolUseId", str(uuid.uuid4()))
                tool_input = tool_use.get("input", {})
                if not isinstance(tool_input, dict):
                    tool_input = {}

                try:
                    is_web_tool = tool_name in self._local_tool_names()
                    if is_web_tool:
                        self._emit_progress(progress_cb, f"Calling web tool: {tool_name}...")
                    else:
                        self._emit_progress(progress_cb, f"Calling MCP tool: {resolved_tool_name}...")
                    tool_output = await self._call_any_tool(resolved_tool_name, tool_input)
                    normalized = self._normalize_tool_output(tool_output)
                    executed_tool_outputs.append((resolved_tool_name, normalized))
                    tool_used = True
                    self._emit_progress(progress_cb, f"Received data from: {resolved_tool_name}.")
                    if isinstance(normalized, (dict, list)):
                        content = [{"json": normalized}]
                    else:
                        content = [{"text": str(normalized)}]
                    tool_results.append(
                        {
                            "toolResult": {
                                "toolUseId": tool_use_id,
                                "content": content,
                            }
                        }
                    )
                except Exception as exc:
                    self._emit_progress(progress_cb, f"Tool failed: {resolved_tool_name}.")
                    tool_results.append(
                        {
                            "toolResult": {
                                "toolUseId": tool_use_id,
                                "status": "error",
                                "content": [{"text": f"Tool execution failed: {exc}"}],
                            }
                        }
                    )

            messages.append({"role": "user", "content": tool_results})

        if executed_tool_outputs:
            tool_name, last_output = executed_tool_outputs[-1]
            return (
                "I could not complete tool orchestration cleanly, "
                "but here is the latest tool data.\n\n"
                + self._format_tool_result(tool_name, last_output)
            )

        return "I could not complete tool execution within the step limit."

    def _has_local_tools(self) -> bool:
        return True

    def _local_tool_specs(self) -> list[dict[str, Any]]:
        return self.web_tools.tool_specs()

    def _local_tool_names(self) -> set[str]:
        return {"web_search", "fetch_web_content"}

    async def _call_any_tool(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        if tool_name in self._local_tool_names():
            return await self.web_tools.run(tool_name, tool_input)
        if self.mcp_client is None:
            raise ValueError("MCP client is not configured and this is not a local web tool.")
        return await self.mcp_client.call_tool(tool_name, tool_input)

    def _emit_progress(
        self, progress_cb: Callable[[str], None] | None, message: str
    ) -> None:
        if progress_cb is not None:
            progress_cb(message)

    def _needs_grounded_tooling(self, query: str) -> bool:
        q = query.lower()
        keywords = [
            "catalyst center",
            "dna center",
            "cluster",
            "site",
            "device",
            "issue",
            "version",
            "release",
            "inventory",
            "assurance",
            "cve",
            "advisory",
            "vulnerability",
            "cisco.com",
        ]
        return any(keyword in q for keyword in keywords)

    async def _reply_without_tools(self, history: list[ChatTurn]) -> str:
        messages = [
            {
                "role": turn["role"],
                "content": [{"text": turn["text"]}],
            }
            for turn in history
        ]
        response = self.client.converse(
            modelId=self.model_id,
            messages=messages,
            system=[{"text": self.system_prompt}],
            inferenceConfig={
                "maxTokens": self.INFERENCE_MAX_TOKENS,
                "temperature": 0.4,
            },
        )
        out_content = response.get("output", {}).get("message", {}).get("content", [])
        return self._extract_text(out_content)

    def _extract_text(self, content: list[dict[str, Any]]) -> str:
        text_items = [item.get("text", "") for item in content if "text" in item]
        answer = "\n".join([item for item in text_items if item.strip()]).strip()
        return answer or "I could not generate a response."

    def _to_bedrock_tool_specs(
        self,
        mcp_tools: list[dict[str, Any]],
        reserved_names: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        specs: list[dict[str, Any]] = []
        name_map: dict[str, str] = {}
        used_names = set(reserved_names or set())
        for tool in mcp_tools:
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                continue
            bedrock_name = self._safe_bedrock_tool_name(name, used_names)
            used_names.add(bedrock_name)
            name_map[bedrock_name] = name
            description = tool.get("description", "")
            schema = tool.get("inputSchema", {"type": "object", "properties": {}})
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            full_description = str(description).strip()
            if full_description:
                full_description = f"{full_description} [mcp tool: {name}]"
            else:
                full_description = f"MCP tool: {name}"

            specs.append(
                {
                    "toolSpec": {
                        "name": bedrock_name[:64],
                        "description": full_description[:1024],
                        "inputSchema": {"json": schema},
                    }
                }
            )
        return specs, name_map

    def _safe_bedrock_tool_name(self, raw_name: str, used_names: set[str]) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_")
        if not cleaned:
            cleaned = "mcp_tool"
        if cleaned[0].isdigit():
            cleaned = f"t_{cleaned}"
        cleaned = cleaned[:64]
        if cleaned not in used_names:
            return cleaned

        suffix = 2
        while True:
            candidate = f"{cleaned[: max(1, 64 - len(str(suffix)) - 1)]}_{suffix}"
            if candidate not in used_names:
                return candidate
            suffix += 1

    def _format_tool_result(self, tool_name: str, value: Any) -> str:
        normalized = self._normalize_tool_output(value)
        pretty = json.dumps(normalized, indent=2, ensure_ascii=False)
        lines: list[str] = []
        lines.append(f"### Tool: `{tool_name}`")
        lines.extend(self._tool_highlights(normalized))
        lines.append("")
        lines.append("<details>")
        lines.append("<summary>Raw JSON</summary>")
        lines.append("")
        lines.append("```json")
        lines.append(pretty)
        lines.append("```")
        lines.append("</details>")
        return "\n".join(lines)

    def _normalize_tool_output(self, value: Any) -> Any:
        if isinstance(value, dict):
            if "structuredContent" in value:
                return self._normalize_tool_output(value["structuredContent"])
            if "content" in value and isinstance(value["content"], list):
                parsed_items: list[Any] = []
                for item in value["content"]:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text", "")
                        if isinstance(text, str):
                            parsed_items.append(self._maybe_parse_json_text(text))
                        else:
                            parsed_items.append(text)
                    else:
                        parsed_items.append(self._normalize_tool_output(item))
                if len(parsed_items) == 1:
                    return parsed_items[0]
                return parsed_items
            return {k: self._normalize_tool_output(v) for k, v in value.items()}

        if isinstance(value, list):
            return [self._normalize_tool_output(v) for v in value]

        if isinstance(value, str):
            return self._maybe_parse_json_text(value)

        return value

    def _maybe_parse_json_text(self, text: str) -> Any:
        candidate = text.strip()
        if not candidate:
            return text
        try:
            parsed = json.loads(candidate)
            return self._normalize_tool_output(parsed)
        except json.JSONDecodeError:
            return text

    def _tool_highlights(self, normalized: Any) -> list[str]:
        bullets: list[str] = []
        if isinstance(normalized, dict):
            total_fields = [
                (k, v) for k, v in normalized.items() if k.startswith("total_") and isinstance(v, int)
            ]
            for k, v in total_fields[:6]:
                bullets.append(f"- **{k}**: {v}")

            if "cluster_names" in normalized and isinstance(normalized["cluster_names"], list):
                names = [str(x) for x in normalized["cluster_names"][:10]]
                bullets.append(f"- **cluster_names**: {', '.join(names)}")

            keys = list(normalized.keys())
            bullets.append(f"- **top-level keys**: {', '.join(keys[:10])}")
        elif isinstance(normalized, list):
            bullets.append(f"- **items**: {len(normalized)}")
            if normalized and isinstance(normalized[0], dict):
                keys = list(normalized[0].keys())
                bullets.append(f"- **item keys**: {', '.join(keys[:10])}")
        else:
            bullets.append(f"- **value**: {str(normalized)[:120]}")
        return bullets

