from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

app = FastAPI(title="HTTP to HTTPS Redirect")

HTTPS_PORT = int(os.getenv("HTTPS_PORT", "3000"))


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def redirect_to_https(path: str, request: Request) -> RedirectResponse:
    host_header = request.headers.get("host", "localhost")
    host = host_header.split(":")[0]
    port_part = "" if HTTPS_PORT == 443 else f":{HTTPS_PORT}"
    query_part = f"?{request.url.query}" if request.url.query else ""
    target = f"https://{host}{port_part}/{path}{query_part}"
    return RedirectResponse(url=target, status_code=307)
