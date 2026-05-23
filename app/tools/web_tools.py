from __future__ import annotations

import html
import ipaddress
import re
import socket
from urllib.parse import parse_qs, unquote, urlparse

import httpx


def _is_blocked_host(hostname: str | None) -> bool:
    if not hostname:
        return True
    host = hostname.strip().lower()
    if host in {"localhost", "0.0.0.0"}:
        return True

    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True

    for info in infos:
        raw_ip = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
        except ValueError:
            return True

    return False


def _strip_html_tags(text: str) -> str:
    without_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.I | re.S)
    plain = re.sub(r"<[^>]+>", " ", without_scripts)
    plain = html.unescape(plain)
    return re.sub(r"\s+", " ", plain).strip()


class WebTools:
    @staticmethod
    def tool_specs() -> list[dict]:
        return [
            {
                "toolSpec": {
                    "name": "web_search",
                    "description": (
                        "Search the web for recent information (for example CVEs, advisories, release notes) "
                        "and return top matching links with titles."
                    ),
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Search query string."},
                                "max_results": {
                                    "type": "integer",
                                    "description": "Max number of results (1-10).",
                                    "minimum": 1,
                                    "maximum": 10,
                                },
                            },
                            "required": ["query"],
                        }
                    },
                }
            },
            {
                "toolSpec": {
                    "name": "fetch_web_content",
                    "description": (
                        "Fetch text content from a web page URL and return title + extracted text snippet."
                    ),
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "description": "HTTP/HTTPS URL to fetch."},
                                "max_chars": {
                                    "type": "integer",
                                    "description": "Maximum returned text length.",
                                    "minimum": 500,
                                    "maximum": 20000,
                                },
                            },
                            "required": ["url"],
                        }
                    },
                }
            },
        ]

    async def run(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name == "web_search":
            query = str(tool_input.get("query", "")).strip()
            max_results = int(tool_input.get("max_results", 5))
            return await self.web_search(query=query, max_results=max_results)
        if tool_name == "fetch_web_content":
            url = str(tool_input.get("url", "")).strip()
            max_chars = int(tool_input.get("max_chars", 5000))
            return await self.fetch_web_content(url=url, max_chars=max_chars)
        raise ValueError(f"Unsupported web tool: {tool_name}")

    async def web_search(self, query: str, max_results: int = 5) -> dict:
        q = query.strip()
        if not q:
            raise ValueError("web_search requires a non-empty query")
        limit = max(1, min(max_results, 10))

        url = "https://duckduckgo.com/html/"
        params = {"q": q}

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers={"user-agent": "mcp-client-webapp/1.0"})
            response.raise_for_status()
            html_text = response.text

        title_matches = re.findall(
            r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html_text,
            flags=re.I | re.S,
        )
        snippet_matches = re.findall(
            r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
            html_text,
            flags=re.I | re.S,
        )

        results: list[dict[str, str]] = []
        for idx, (href, raw_title) in enumerate(title_matches):
            if len(results) >= limit:
                break
            link = href
            if "duckduckgo.com/l/?" in href:
                parsed = urlparse(href)
                uddg = parse_qs(parsed.query).get("uddg", [""])[0]
                if uddg:
                    link = unquote(uddg)
            parsed_link = urlparse(link)
            if parsed_link.scheme not in {"http", "https"}:
                continue
            if _is_blocked_host(parsed_link.hostname):
                continue
            title = _strip_html_tags(raw_title)
            snippet_raw = snippet_matches[idx] if idx < len(snippet_matches) else ""
            snippet = _strip_html_tags(snippet_raw)
            results.append({"title": title, "url": link, "snippet": snippet})

        return {"query": q, "total_results": len(results), "results": results}

    async def fetch_web_content(self, url: str, max_chars: int = 5000) -> dict:
        if not url:
            raise ValueError("fetch_web_content requires a URL")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only HTTP/HTTPS URLs are allowed")
        if _is_blocked_host(parsed.hostname):
            raise ValueError("Blocked URL host")

        clipped_chars = max(500, min(max_chars, 20000))
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"user-agent": "mcp-client-webapp/1.0"})
            response.raise_for_status()
            final_url = str(response.url)
            body = response.text

        title_match = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.I | re.S)
        title = _strip_html_tags(title_match.group(1)) if title_match else ""
        plain_text = _strip_html_tags(body)
        excerpt = plain_text[:clipped_chars]

        return {
            "url": url,
            "final_url": final_url,
            "status_code": response.status_code,
            "title": title,
            "content_excerpt": excerpt,
            "content_length": len(plain_text),
        }
