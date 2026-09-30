"""The bundled engines' own interfaces, on an origin of their own.

Proxied because from outside the house only OPUS is published, and kept off the
Downloads origin because a script in any engine's page would otherwise run with
that origin's session and read its service token. Each engine was told its base
path at provision time, so the links it builds already point back under the same
prefix here — except qBittorrent, which asks for everything relative to where its
page was loaded from."""

import asyncio
import logging
import re
from types import SimpleNamespace

import httpx
import opus_auth
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.websockets import WebSocketDisconnect
from opus_auth import ADMIN
from opus_core.door import Door
from starlette.background import BackgroundTask
from websockets.exceptions import InvalidHandshake

from opus import auth, logs
from opus.config import settings
from opus.engines.base import EngineMode
from opus.engines.catalog import UI_PREFIX, ui_base_path
from opus.engines.registry import UnknownEngine, build_engine
from opus.redaction import redacted
from opus.settings_store import current_runtime

logs.configure()
log = logging.getLogger(__name__)

app = FastAPI(title="OPUS engines", docs_url=None, redoc_url=None, openapi_url=None)

ROUTE = UI_PREFIX.format(name="{name}") + "/{path:path}"

# hop-by-hop headers describe one connection and must not be forwarded onto
# another. set-cookie and the engine's own policy are passed on separately: an
# engine may send several, and a dict of headers would join them into one line
STRIPPED = {
    "content-length", "content-encoding", "transfer-encoding", "connection",
    "keep-alive", "x-frame-options", "content-security-policy", "set-cookie",
}

_COOKIE_PATH = re.compile(r";\s*path=[^;]*", re.IGNORECASE)

# what identifies a caller to OPUS is OPUS's business and nobody else's: an
# engine is handed only what its own interface set
_CALLER = {"cookie", "authorization", opus_auth.TOKEN_HEADER.lower()}
_OPUS_COOKIE = "opus_"

# headers the handshake owns: forwarding a client's own upgrade negotiation into
# a second one is how a websocket fails to open with nothing in any log
_HANDSHAKE = {
    "host", "connection", "upgrade", "sec-websocket-key",
    "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-accept",
}


@app.get("/api/ping")
async def ping():
    return {"ok": True}


async def refusal(request: Request) -> JSONResponse | None:
    if request.url.path == "/api/ping":
        return None
    if not opus_auth.same_origin(request):
        return JSONResponse({"detail": "not sent from this origin's pages"}, status_code=403)
    return await auth.refused(request.cookies.get(opus_auth.SESSION_COOKIE))


app.add_middleware(Door, refusal=refusal)


def _engine_cookie(header: str | None) -> str | None:
    kept = [part.strip() for part in (header or "").split(";")
            if part.strip() and not part.strip().startswith(_OPUS_COOKIE)]
    return "; ".join(kept) or None


def _forwarded(headers, dropped: set[str]) -> list[tuple[str, str]]:
    out = [(k, v) for k, v in headers.items() if k.lower() not in dropped | _CALLER]
    if cookie := _engine_cookie(headers.get("cookie")):
        out.append(("cookie", cookie))
    return out


def _upstream_path(engine, path: str) -> str:
    return path[len(ui_base_path(engine.name)):] or "/"


def _scoped_cookie(engine, cookie: str) -> str:
    """An engine that does not know its base path scopes its cookie to the root,
    which here is every engine at once."""
    if engine.spec.serves_at_base_path:
        return cookie
    return f"{_COOKIE_PATH.sub('', cookie)}; Path={ui_base_path(engine.name)}"


def _own_policy(policy: str) -> str:
    return "; ".join(d.strip() for d in policy.split(";")
                     if d.strip() and not d.strip().lower().startswith("frame-ancestors"))


def _framers(request: Request) -> str:
    """Only a page that shares the session may frame an engine: the pages on this
    host, and those on the domain the session cookie is scoped to."""
    host = request.url.hostname or ""
    sources = [f"{host}:*"]
    domain = opus_auth.cookie_domain(settings.cookie_domain, request)
    if domain:
        sources += [domain, f"*.{domain}"]
    return f"frame-ancestors {' '.join(sources)}"


def _bundled(runtime, name: str):
    engine = build_engine(runtime, name)
    return engine if engine.mode is EngineMode.BUNDLED else None


@app.api_route(ROUTE, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy(name: str, path: str, request: Request):
    runtime = await current_runtime()
    try:
        engine = _bundled(runtime, name)
    except UnknownEngine:
        raise HTTPException(404, f"unknown engine: {name}")
    if engine is None:
        raise HTTPException(409, f"{name} is not bundled; OPUS only serves the engines it runs")

    body = await request.body()
    url = f"{engine.config.url.rstrip('/')}{_upstream_path(engine, request.url.path)}"
    # the engine answers as itself, so its own name is the host it expects
    headers = _forwarded(request.headers, {"host", "content-length", "accept-encoding"})
    # no read timeout: an interface that pushes its updates holds the response
    # open for as long as it has nothing to say
    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=None), follow_redirects=False)
    try:
        upstream = await client.send(
            client.build_request(request.method, url, params=request.query_params,
                                 content=body, headers=headers),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, redacted(f"{name}: its interface is not answering: {exc}"))

    passed = {k: v for k, v in upstream.headers.items() if k.lower() not in STRIPPED}
    # a redirect must land back inside this path: the engine answers with its
    # own root, or with its own address, and a browser can reach neither
    location = upstream.headers.get("location")
    if location:
        engine_root = engine.config.url.rstrip("/")
        if location.startswith(engine_root):
            location = location[len(engine_root):] or "/"
        if location.startswith("/") and not location.startswith(ui_base_path(name)):
            location = f"{ui_base_path(name)}{location}"
        passed["location"] = location

    async def close():
        await upstream.aclose()
        await client.aclose()

    # streamed: a long-poll or an event stream never ends. Decoded on the way
    # through, because the content-encoding header is dropped with the rest of
    # the connection's own headers
    response = StreamingResponse(
        upstream.aiter_bytes(),
        status_code=upstream.status_code,
        headers=passed,
        media_type=upstream.headers.get("content-type"),
        background=BackgroundTask(close),
    )
    for key, value in upstream.headers.multi_items():
        if key.lower() == "set-cookie":
            response.raw_headers.append((b"set-cookie", _scoped_cookie(engine, value).encode()))
        elif key.lower() == "content-security-policy" and (kept := _own_policy(value)):
            response.raw_headers.append((b"content-security-policy", kept.encode()))
    response.raw_headers.append((b"content-security-policy", _framers(request).encode()))
    return response


def _socket_from_here(websocket: WebSocket) -> bool:
    # judged as the unsafe request it is: same_origin passes every GET, and a
    # handshake is a GET that opens a channel driving the engine
    return opus_auth.same_origin(SimpleNamespace(method="WEBSOCKET", headers=websocket.headers))


@app.websocket(ROUTE)
async def proxy_socket(websocket: WebSocket, name: str, path: str):
    """Prowlarr and slskd push their updates over a websocket, and the HTTP guard
    does not see an upgrade, so this route asks for itself."""
    if not _socket_from_here(websocket):
        await websocket.close(code=1008)
        return
    found = await auth.person(websocket.cookies.get(opus_auth.SESSION_COOKIE))
    if found is None or found.role != ADMIN:
        await websocket.close(code=1008)
        return
    try:
        engine = _bundled(await current_runtime(), name)
    except UnknownEngine:
        engine = None
    if engine is None:
        await websocket.close(code=1008)
        return

    target = engine.config.url.rstrip("/") + _upstream_path(engine, websocket.url.path)
    target = "ws" + target[len("http"):]
    if websocket.url.query:
        target = f"{target}?{websocket.url.query}"
    headers = _forwarded(websocket.headers, _HANDSHAKE)

    await websocket.accept()
    try:
        async with websockets.connect(target, additional_headers=headers,
                                      open_timeout=20, ping_interval=None) as upstream:
            async def to_engine():
                while True:
                    await upstream.send(await websocket.receive_text())

            async def to_browser():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            pumps = [asyncio.create_task(to_engine()), asyncio.create_task(to_browser())]
            try:
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in pumps:
                    task.cancel()
                # a hangup ends both pumps and each raises about it; an exception
                # nobody retrieves is logged as an error
                await asyncio.gather(*pumps, return_exceptions=True)
    except (OSError, TimeoutError, InvalidHandshake) as exc:
        log.warning("%s: its interface's socket would not open: %s", name, redacted(str(exc)))
    finally:
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
