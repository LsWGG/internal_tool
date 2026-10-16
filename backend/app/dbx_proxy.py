"""Same-origin reverse proxy for the bundled DBX process.

DBX sets `dbx_session` with `SameSite=Lax` and `Path=/dbx`. A cross-site iframe
(the tool on :5173 or :80, DBX on 127.0.0.1:4224) cannot keep that cookie, so
login succeeds and the next request is anonymous again. Serving DBX under
`/dbx` on the tool's own host makes the cookie first-party.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator

import requests
import websockets
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import Request

from .dbx_manager import DBXManager

_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_session = requests.Session()
_session.trust_env = False


def rewrite_upstream_location(value: str, upstream: str) -> str:
    """Turn an absolute DBX redirect into a path on this host."""
    prefix = upstream.rstrip("/")
    if value.startswith(prefix):
        rest = value[len(prefix):]
        return rest if rest.startswith("/") else f"/{rest}"
    return value


def _upstream_url(manager: DBXManager, path: str, query: str, *, directory: bool = False) -> str:
    if path:
        url = f"{manager.url}/dbx/{path}"
    else:
        url = f"{manager.url}/dbx/" if directory else f"{manager.url}/dbx"
    if query:
        url = f"{url}?{query}"
    return url


def _request_headers(headers) -> dict[str, str]:
    blocked = _HOP | {"host", "content-length"}
    return {key: value for key, value in headers.items() if key.lower() not in blocked}


def _response_header_pairs(response: requests.Response, upstream: str) -> list[tuple[str, str]]:
    raw = response.raw.headers
    names = list(raw.keys()) if hasattr(raw, "keys") else []
    pairs: list[tuple[str, str]] = []
    for name in names:
        if not name or name.lower() in _HOP:
            continue
        values = raw.getlist(name) if hasattr(raw, "getlist") else [raw[name]]
        for value in values:
            if name.lower() == "location":
                value = rewrite_upstream_location(value, upstream)
            pairs.append((name, value))
    return pairs


def _iter_upstream(response: requests.Response) -> Iterator[bytes]:
    raw = response.raw
    raw.decode_content = False
    try:
        while True:
            chunk = raw.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        response.close()


def proxy_dbx_http(
    method: str,
    headers,
    query: str,
    body: bytes,
    manager: DBXManager,
    path: str = "",
    directory: bool = False,
) -> StreamingResponse | JSONResponse:
    if ".." in path.split("/"):
        return JSONResponse({"error": "DBX 路径无效"}, status_code=404)
    upstream = manager.url
    target = _upstream_url(manager, path, query, directory=directory)
    try:
        upstream_response = _session.request(
            method,
            target,
            headers=_request_headers(headers),
            data=body if body else None,
            allow_redirects=False,
            stream=True,
            timeout=(3, 3600),
        )
    except requests.RequestException as exc:
        return JSONResponse({"error": f"DBX 未响应：{exc}"}, status_code=502)
    body = _iter_upstream(upstream_response)
    response = StreamingResponse(body, status_code=upstream_response.status_code)
    for name, value in _response_header_pairs(upstream_response, upstream):
        if name.lower() == "set-cookie":
            response.headers.append(name, value)
        else:
            response.headers[name] = value
    return response


async def proxy_dbx_ws(websocket: WebSocket, manager: DBXManager, path: str = "") -> None:
    if ".." in path.split("/"):
        await websocket.close(code=1008)
        return
    query = websocket.scope.get("query_string", b"").decode()
    directory = websocket.scope.get("path", "").endswith("/") and not path
    target = _upstream_url(manager, path, query, directory=directory).replace("http://", "ws://", 1)
    headers = []
    if cookie := websocket.headers.get("cookie"):
        headers.append(("Cookie", cookie))
    protocol = websocket.headers.get("sec-websocket-protocol")
    subprotocols = [item.strip() for item in protocol.split(",") if item.strip()] if protocol else None
    try:
        upstream = await websockets.connect(
            target,
            additional_headers=headers,
            subprotocols=subprotocols,
            proxy=None,
            max_size=None,
            open_timeout=5,
        )
    except Exception:
        await websocket.close(code=1011)
        return
    await websocket.accept(subprotocol=getattr(upstream, "subprotocol", None))

    async def client_to_upstream() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("text") is not None:
                await upstream.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream.send(message["bytes"])

    async def upstream_to_client() -> None:
        async for message in upstream:
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(message)

    tasks = {asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())}
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            task.exception()
    finally:
        await upstream.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass


def mount_dbx_proxy(app: FastAPI, manager: DBXManager) -> None:
    @app.api_route("/dbx", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    @app.api_route("/dbx/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    @app.api_route("/dbx/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def dbx_http(request: Request, path: str = ""):
        body = await request.body()
        directory = request.url.path.endswith("/") and not path
        return await asyncio.to_thread(
            proxy_dbx_http, request.method, request.headers, request.url.query, body, manager, path, directory,
        )

    @app.websocket("/dbx")
    @app.websocket("/dbx/")
    @app.websocket("/dbx/{path:path}")
    async def dbx_ws(websocket: WebSocket, path: str = ""):
        await proxy_dbx_ws(websocket, manager, path)
