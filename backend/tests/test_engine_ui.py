import httpx
import opus_auth
import pytest
from starlette.datastructures import Headers

from opus import engine_ui
from opus.config import settings
from opus.engines.base import EngineMode

HERE = "http://engines.test:8099"


@pytest.fixture
async def browser():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=engine_ui.app),
                                 base_url=HERE) as client:
        yield client


def carrying(cookie, **headers):
    return {"cookie": f"{opus_auth.SESSION_COOKIE}={cookie}", **headers}


class Spec:
    serves_at_base_path = True


class Config:
    url = "http://opus_prowlarr:9696/api/engines/prowlarr/ui"


class FakeProwlarr:
    name, mode, spec, config = "prowlarr", EngineMode.BUNDLED, Spec(), Config()


@pytest.fixture
def engine(monkeypatch):
    """A bundled Prowlarr answering whatever the test says, and every request it
    was handed."""
    forwarded = []
    answer = {"response": httpx.Response(200, text="ok")}

    def respond(request):
        forwarded.append(request)
        return answer["response"]
    real = httpx.AsyncClient
    monkeypatch.setattr(engine_ui.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(respond)}))
    monkeypatch.setattr(engine_ui, "build_engine", lambda runtime, name: FakeProwlarr())
    return forwarded, answer


async def test_the_engine_origin_lets_in_its_admin_and_nobody_else(browser, roster):
    assert (await browser.get("/api/ping")).json() == {"ok": True}
    assert (await browser.get("/api/engines/prowlarr/ui/")).status_code == 401
    assert (await browser.get("/api/engines/prowlarr/ui/",
                              headers=carrying(roster("ana")))).status_code == 403
    assert (await browser.get("/api/engines/prowlarr/ui/",
                              headers=carrying(roster("filip")))).status_code == 409
    assert (await browser.get("/api/engines/nonesuch/ui/",
                              headers=carrying(roster("filip")))).status_code == 404


async def test_the_engine_origin_refuses_a_change_sent_from_another_origin(browser, roster, engine):
    sibling = carrying(roster("filip"), origin="http://engines.test:5282", **{"sec-fetch-site": "same-site"})
    assert (await browser.post("/api/engines/prowlarr/ui/api/v1/indexer", headers=sibling)).status_code == 403
    own = carrying(roster("filip"), origin=HERE, **{"sec-fetch-site": "same-origin"})
    assert (await browser.post("/api/engines/prowlarr/ui/api/v1/indexer", headers=own)).status_code == 200
    assert len(engine[0]) == 1


async def test_an_engine_interface_is_never_handed_opus_credentials(browser, roster, token, engine):
    forwarded, answer = engine
    answer["response"] = httpx.Response(200, text="ok", headers={"set-cookie": "SID=new; Path=/"})
    cookie = roster("filip")
    resp = await browser.get("/api/engines/prowlarr/ui/system/status", headers={
        "cookie": f"opus_session={cookie}; SID=engine-own; opus_device=phone",
        "authorization": "Basic b3B1czpodW50ZXIy",
        opus_auth.TOKEN_HEADER: token,
        "x-custom": "kept",
    })
    assert resp.status_code == 200 and resp.text == "ok"
    sent = forwarded[0]
    assert sent.url.path == "/api/engines/prowlarr/ui/system/status"
    assert sent.headers["cookie"] == "SID=engine-own"
    assert "authorization" not in sent.headers
    assert opus_auth.TOKEN_HEADER.lower() not in sent.headers
    assert sent.headers["x-custom"] == "kept"
    assert token not in str(sent.headers.raw) and cookie not in str(sent.headers.raw)


async def test_only_pages_that_share_the_session_may_frame_an_engine(browser, roster, engine,
                                                                    monkeypatch):
    _, answer = engine
    answer["response"] = httpx.Response(200, text="<html>", headers=[
        ("x-frame-options", "SAMEORIGIN"),
        ("content-security-policy", "default-src 'self'; frame-ancestors 'self'"),
        ("location", "http://opus_prowlarr:9696/api/engines/prowlarr/ui/login"),
    ])
    resp = await browser.get("/api/engines/prowlarr/ui/", headers=carrying(roster("filip")))
    assert "x-frame-options" not in resp.headers
    assert resp.headers.get_list("content-security-policy") == [
        "default-src 'self'", "frame-ancestors engines.test:*"]
    assert resp.headers["location"] == "/api/engines/prowlarr/ui/login"

    monkeypatch.setattr(settings, "cookie_domain", "example.com")
    resp = await browser.get("/api/engines/prowlarr/ui/", headers={
        "host": "engines.example.com", **carrying(roster("filip"))})
    assert resp.headers.get_list("content-security-policy")[-1] == (
        "frame-ancestors engines.example.com:* example.com *.example.com")


def test_a_socket_is_handed_only_the_engines_own_cookies():
    headers = Headers({"cookie": "opus_session=signed; SID=abc", opus_auth.TOKEN_HEADER: "t",
                       "authorization": "Bearer x", "sec-websocket-key": "k", "user-agent": "ua"})
    forwarded = dict(engine_ui._forwarded(headers, engine_ui._HANDSHAKE))
    assert forwarded == {"user-agent": "ua", "cookie": "SID=abc"}


async def handshake(headers: dict[str, str]) -> list[dict]:
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)
    await engine_ui.app({
        "type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "root_path": "",
        "path": "/api/engines/slskd/ui/hub/application", "raw_path": b"/api/engines/slskd/ui/hub/application",
        "query_string": b"", "server": ("engines.test", 8099), "client": ("192.168.1.20", 50000),
        "subprotocols": [],
        "headers": [(k.encode(), v.encode()) for k, v in {"host": "engines.test:8099", **headers}.items()],
    }, receive, send)
    return sent


@pytest.mark.parametrize(("who", "origin"), [
    ("filip", "http://engines.test:5282"),
    ("filip", "https://evil.example"),
    ("ana", HERE),
    (None, HERE),
])
async def test_a_socket_opens_only_from_this_origin_for_its_admin(roster, who, origin):
    headers = {"origin": origin}
    if who:
        headers["cookie"] = f"{opus_auth.SESSION_COOKIE}={roster(who)}"
    assert await handshake(headers) == [{"type": "websocket.close", "code": 1008, "reason": ""}]
