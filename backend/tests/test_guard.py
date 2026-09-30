import json
import uuid
from compression import zstd

import httpx
import opus_auth
import pytest

from opus import main, provision
from opus.config import settings
from opus.db import SessionLocal
from opus.engines.ytdlp import YtdlpEngine
from opus.main import app
from opus.models import Job, JobState
from opus.settings_store import store_credentials

SIBLING = {"origin": "https://player.example.com", "sec-fetch-site": "same-site"}
OWN = {"origin": "http://opus", "sec-fetch-site": "same-origin"}


@pytest.fixture
async def browser():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://opus") as client:
        yield client


def carrying(cookie, **headers):
    return {"cookie": f"{opus_auth.SESSION_COOKIE}={cookie}", **headers}


async def test_nobody_is_not_authenticated_and_a_member_is_not_let_in(browser, roster):
    assert (await browser.get("/api/jobs")).status_code == 401
    assert (await browser.get("/api/jobs", headers=carrying("forged"))).status_code == 401
    member = await browser.get("/api/jobs", headers=carrying(roster("ana")))
    assert member.status_code == 403 and "not yours" in member.json()["detail"]
    assert (await browser.get("/api/jobs", headers=carrying(roster("filip")))).status_code == 200


async def test_a_json_answer_leaves_the_door_compressed(browser, roster):
    answer = await browser.get("/api/settings", headers=carrying(roster("filip"), **{"accept-encoding": "zstd"}))
    assert answer.headers["content-encoding"] == "zstd"
    assert json.loads(zstd.decompress(answer.content))


async def test_a_change_carrying_the_cookie_from_a_sibling_site_is_refused(browser, roster):
    grab = {"grab_ref": {"engine": "ytdlp", "url": "https://x"}}
    refused = await browser.post("/api/inspect", json=grab, headers=carrying(roster("filip"), **SIBLING))
    assert refused.status_code == 403 and "this module's pages" in refused.json()["detail"]
    assert (await browser.post("/api/inspect", json=grab,
                               headers=carrying(roster("filip"), **OWN))).status_code == 200


async def test_the_module_token_passes_without_a_session(browser, roster, token):
    # A module can follow one job, but does not receive the global job list or
    # any of the installation/admin surface.
    assert (await browser.get("/api/jobs/missing", headers={opus_auth.TOKEN_HEADER: token})).status_code == 404
    assert (await browser.get("/api/jobs", headers={opus_auth.TOKEN_HEADER: token})).status_code == 403
    assert (await browser.get("/api/jobs", headers={opus_auth.TOKEN_HEADER: "wrong"})).status_code == 401
    assert (await browser.post("/api/inspect", json={"grab_ref": {"engine": "ytdlp"}},
                               headers={opus_auth.TOKEN_HEADER: token,
                                        **carrying(roster("ana"), **SIBLING)})).status_code == 200


async def test_a_module_token_can_only_follow_its_library_jobs(browser, token):
    async def add(app):
        job = Job(id=uuid.uuid4().hex, engine="ytdlp", app=app, namespace="music", title="x",
                  grab_ref={"engine": "ytdlp"}, job_ref={}, state=JobState.QUEUED)
        async with SessionLocal() as session:
            session.add(job)
            await session.commit()
        return job.id

    library, other = await add("library"), await add("another-app")
    headers = {opus_auth.TOKEN_HEADER: token}
    assert (await browser.get(f"/api/jobs/{library}", headers=headers)).status_code == 200
    assert (await browser.get(f"/api/jobs/{other}", headers=headers)).status_code == 404
    assert (await browser.delete(f"/api/jobs/{other}", headers=headers)).status_code == 404


async def test_a_module_grab_cannot_spoof_its_app(browser, token, monkeypatch):
    async def download(self, grab_ref):
        async def finished(progress, workdir):
            return str(workdir)
        return "test", finished

    monkeypatch.setattr(YtdlpEngine, "_download", download)
    await store_credentials({"ytdlp_enabled": "true", "ytdlp_landing_dir": f"{settings.landing_root}/web"})
    response = await browser.post("/api/grab", headers={opus_auth.TOKEN_HEADER: token}, json={
        "grab_ref": {"engine": "ytdlp", "url": "https://example.test/video"},
        "namespace": "video", "app": "another-app",
    })
    assert response.status_code == 200, response.text
    async with SessionLocal() as session:
        job = await session.get(Job, response.json()["job_id"])
    assert job.app == "library"


@pytest.mark.parametrize(("method", "path", "body"), [
    ("get", "/api/settings", None),
    ("put", "/api/settings", {"content_folders": "music"}),
    ("get", "/api/engines", None),
    ("post", "/api/engines/slskd/start", None),
    ("get", "/api/auth/token", None),
    ("post", "/api/auth/token", None),
])
async def test_a_module_token_cannot_reach_administration(browser, token, method, path, body):
    response = await browser.request(method, path, json=body,
                                     headers={opus_auth.TOKEN_HEADER: token})
    assert response.status_code == 403
    assert response.json()["detail"] == "service token may only acquire and track jobs"


async def test_the_open_paths_answer_anyone(browser):
    assert (await browser.get("/api/ping")).json() == {"ok": True}
    assert (await browser.get("/api/ready")).json() == {"ok": True}
    assert (await browser.get("/api/auth/session")).json()["authenticated"] is False
    assert (await browser.post("/api/auth/logout")).status_code == 200


async def test_an_open_path_still_refuses_a_cookie_sent_from_elsewhere(browser, roster):
    cookie = roster("filip")
    assert (await browser.post("/api/auth/logout", headers=carrying(cookie, **SIBLING))).status_code == 403
    assert (await browser.post("/api/auth/login", json={"username": "filip", "password": "x"},
                               headers=carrying(cookie, **SIBLING))).status_code == 403
    assert (await browser.post("/api/auth/logout", headers=carrying(cookie, **OWN))).status_code == 200


@pytest.mark.parametrize("path", ["/api/auth/login", "/api/auth/logout"])
async def test_a_page_elsewhere_cannot_use_the_open_door_without_a_cookie(browser, path):
    body = {"username": "filip", "password": "x"}
    for elsewhere in (SIBLING, {"sec-fetch-site": "cross-site"}, {"origin": "https://evil.example"}):
        refused = await browser.post(path, json=body, headers=elsewhere)
        assert refused.status_code == 403 and "this module's pages" in refused.json()["detail"]
        assert "set-cookie" not in refused.headers
    for here in (OWN, {}):
        assert "this module's pages" not in (await browser.post(path, json=body, headers=here)).text


async def test_the_main_app_no_longer_serves_engine_interfaces(browser, roster):
    resp = await browser.get("/api/engines/prowlarr/ui/", headers=carrying(roster("filip")))
    assert resp.status_code == 404


async def test_a_bundled_engine_without_an_origin_for_its_interface_stops_the_start(monkeypatch):
    applied = []
    monkeypatch.setattr(provision, "apply_saved", applied.append)
    await store_credentials({"slskd_mode": "bundled"})
    monkeypatch.setattr(settings, "engines_url", "")
    with pytest.raises(RuntimeError, match="OPUS_ENGINES_URL is not set, and slskd run bundled"):
        async with main.lifespan(app):
            pass
    monkeypatch.setattr(settings, "engines_url", "http://engines.test:8099")
    async with main.lifespan(app):
        pass
    assert applied == [{}]


async def test_every_answer_carries_the_browser_headers(browser):
    for answer in (await browser.get("/api/ping"), await browser.get("/api/jobs")):
        assert answer.headers["x-content-type-options"] == "nosniff"
        assert answer.headers["referrer-policy"] == "same-origin"
        assert answer.headers["content-security-policy"] == "frame-ancestors 'self'"


async def test_the_admin_reissues_the_token_library_calls_with(api, token):
    listed = await api.get("/api/auth/token")
    assert listed.json() == {"tokens": [{"consumer": "library", "token": token}]}
    issued = await api.post("/api/auth/token", json={"consumer": "library"})
    assert issued.json()["consumer"] == "library" and issued.json()["token"] != token
    assert (await api.post("/api/auth/token", json={"consumer": "player"})).status_code == 404


async def test_the_api_does_not_describe_itself(browser, roster):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await browser.get(path, headers=carrying(roster("filip")))).status_code == 404
