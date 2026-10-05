import asyncio

import pytest

from opus.api.routers import engines
from opus.engines.base import EngineHealth, EngineMode
from opus.settings_store import RuntimeConfig


class Probed:
    def __init__(self):
        self.name, self.mode, self.spec, self.calls = "slskd", EngineMode.BUNDLED, None, 0

        class Config:
            use_vpn = True
        self.config = Config()

    async def health(self):
        self.calls += 1
        await asyncio.sleep(0.05)
        return EngineHealth(True, f"call {self.calls}")


async def test_a_health_answer_is_shared_until_it_is_stale_or_the_settings_move():
    engine = Probed()
    runtime = RuntimeConfig({"a": "1"})
    answers = await asyncio.gather(*(engines._health(engine, runtime) for _ in range(6)))
    assert engine.calls == 1 and {a.detail for a in answers} == {"call 1"}
    await engines._health(engine, runtime)
    assert engine.calls == 1
    await engines._health(engine, RuntimeConfig({"a": "2"}))
    assert engine.calls == 2
    engines._answers["slskd"].at -= engines.FRESH_SECONDS + 1
    await engines._health(engine, RuntimeConfig({"a": "2"}))
    assert engine.calls == 3


@pytest.mark.parametrize(("own", "exit_ip", "leaking"), [
    ("1.1.1.1", "2.2.2.2", False),
    ("1.1.1.1", "1.1.1.1", True),
    (None, "2.2.2.2", None),
    ("1.1.1.1", None, None),
])
async def test_a_tunnel_leaks_only_when_it_comes_out_where_the_install_does(monkeypatch, own,
                                                                           exit_ip, leaking):
    async def describe(docker, spec, use_vpn):
        return {"running": True, "vpn": {"running": True, "exit_ip": exit_ip}}

    async def own_exit():
        return own
    monkeypatch.setattr(engines.provision, "describe", describe)
    monkeypatch.setattr(engines, "own_exit", own_exit)
    state = await engines._container(None, Probed())
    assert state["vpn"]["leaking"] is leaking


async def test_a_container_that_cannot_be_described_says_why_without_secrets(monkeypatch):
    async def describe(docker, spec, use_vpn):
        raise OSError("docker at http://docker/v1.44/containers?token=hunter2 is gone")
    monkeypatch.setattr(engines.provision, "describe", describe)
    state = await engines._container(None, Probed())
    assert state["running"] is False and "hunter2" not in state["error"]


class Adopted(Probed):
    def __init__(self):
        super().__init__()
        self.name, self.mode, self.config.use_vpn = "slskd", EngineMode.EXTERNAL, False


async def test_an_adopted_engine_shows_a_container_only_when_one_would_not_go(monkeypatch):
    asked = []

    async def describe(docker, spec, use_vpn):
        asked.append(use_vpn)
        return {"present": True, "running": True, "error": engines.provision.failures["slskd"]}
    monkeypatch.setattr(engines.provision, "describe", describe)
    assert await engines._container(None, Adopted()) is None and asked == []
    engines.provision.failures["slskd"] = "opus_slskd was not created by OPUS"
    state = await engines._container(None, Adopted())
    assert state["running"] and "not created by OPUS" in state["error"] and asked == [True]


async def test_stop_keeps_a_bundled_engines_container(api, monkeypatch):
    from opus.main import app
    paused, taken = [], []
    app.dependency_overrides[engines.current_runtime] = lambda: RuntimeConfig({"slskd_mode": "bundled"})

    async def pause(docker, spec, use_vpn):
        paused.append(spec.name)
        return {"present": True, "running": False}

    async def take_down(docker, spec, use_vpn):
        taken.append(spec.name)
        return {"present": False, "running": False}
    monkeypatch.setattr(engines.provision, "pause", pause)
    monkeypatch.setattr(engines.provision, "take_down", take_down)
    assert (await api.post("/api/engines/slskd/stop")).status_code == 200
    app.dependency_overrides.clear()
    assert paused == ["slskd"] and taken == []


async def test_stop_takes_down_an_adopted_engines_leftover_but_not_an_in_process_one(api, monkeypatch):
    taken = []

    async def take_down(docker, spec, use_vpn):
        taken.append(spec.name)
        return {"present": False, "running": False}
    monkeypatch.setattr(engines.provision, "take_down", take_down)
    assert (await api.post("/api/engines/slskd/stop")).status_code == 200
    resp = await api.post("/api/engines/ytdlp/stop")
    assert resp.status_code == 400 and "no container" in resp.json()["detail"]
    assert taken == ["slskd"]


@pytest.mark.parametrize(("name", "embeddable"), [
    ("qbittorrent", False), ("prowlarr", True), ("sabnzbd", True), ("slskd", True),
])
def test_bundled_ui_capability_keeps_engine_pages_on_their_own_origin(name, embeddable):
    from opus.api.shared import engine_to_dict
    from opus.config import settings
    from opus.engines.registry import build_engine

    engine = build_engine(RuntimeConfig({f"{name}_mode": "bundled"}), name)
    answer = engine_to_dict(engine, EngineHealth(True, "ready"))
    assert answer["ui_embeddable"] is embeddable
    assert answer["ui_url"] == f"{settings.engines_url}/api/engines/{name}/ui/"
