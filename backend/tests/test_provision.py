import asyncio
import configparser
import json
from types import SimpleNamespace

import httpx
import pytest
import yaml

from opus import provision
from opus.config import settings
from opus.containers import OWNER_LABEL, ContainerError
from opus.engines.base import EngineMode
from opus.engines.catalog import SPEC_BY_NAME, ui_base_path
from opus.provision import common, prowlarr, qbittorrent, sabnzbd, slskd
from opus.settings_store import RuntimeConfig, update_settings

SLSKD = SPEC_BY_NAME["slskd"]
QBITTORRENT = SPEC_BY_NAME["qbittorrent"]
SUBNET = "172.20.0.0/16"


@pytest.mark.parametrize("foreign", ["opus_prowlarr", "opus_solver_prowlarr", "opus_vpn_prowlarr"])
async def test_pause_checks_ownership_of_every_container_before_stopping_any(foreign):
    class Docker:
        def __init__(self):
            self.stopped = []

        async def inspect(self, name):
            return {"Config": {"Labels": {} if name == foreign else {OWNER_LABEL: "engine"}},
                    "State": {"Running": True}}

        async def stop(self, name):
            self.stopped.append(name)

    docker = Docker()
    with pytest.raises(ContainerError, match="refusing to stop"):
        await provision.pause(docker, SPEC_BY_NAME["prowlarr"], True)
    assert docker.stopped == []


async def test_pause_stops_owned_containers_and_skips_absent_companions(monkeypatch):
    stopped = []

    class Docker:
        async def inspect(self, name):
            if name == "opus_solver_prowlarr":
                return None
            return {"Config": {"Labels": {OWNER_LABEL: "engine"}}, "State": {"Running": True}}

        async def stop(self, name):
            stopped.append(name)

    async def describe(*args):
        return {"paused": True}

    monkeypatch.setattr(provision, "describe", describe)
    assert await provision.pause(Docker(), SPEC_BY_NAME["prowlarr"], True) == {"paused": True}
    assert stopped == ["opus_prowlarr", "opus_vpn_prowlarr"]


@pytest.fixture
def fast(monkeypatch):
    real_sleep = asyncio.sleep

    async def sleep(_):
        await real_sleep(0.001)
    monkeypatch.setattr(common, "CONFIG_WAIT_SECONDS", 1)
    monkeypatch.setattr(common, "asyncio", SimpleNamespace(
        sleep=sleep, get_running_loop=asyncio.get_running_loop))


def soulseek(username="filip", password="p@ss: word #1"):
    return RuntimeConfig({"slskd_soulseek_username": username,
                          "slskd_soulseek_password": password})


async def test_slskd_is_seeded_with_a_key_that_is_read_back_on_every_start():
    assert slskd.seed(SLSKD, soulseek(), SUBNET) is True
    written = yaml.safe_load(common.config_path(SLSKD, "slskd.yml").read_text())
    assert written["web"]["url_base"] == ui_base_path("slskd")
    assert written["web"]["authentication"]["disabled"] is True
    assert written["soulseek"]["password"] == "p@ss: word #1"
    assert written["directories"]["downloads"] == f"{settings.landing_root}/soulseek"

    learned = await slskd.configure(SLSKD, soulseek(), None, {})
    assert learned == {"slskd_bundled_api_key": written["web"]["authentication"]["api_keys"]["opus"]["key"]}
    assert await slskd.configure(SLSKD, soulseek(), None, {}) == learned


async def test_slskd_without_its_key_is_refused(fast):
    path = common.config_path(SLSKD, "slskd.yml")
    path.parent.mkdir(parents=True)
    path.write_text("web:\n  port: 5030\n")
    with pytest.raises(ContainerError, match="no API key"):
        await slskd.configure(SLSKD, soulseek(), None, {})


def test_a_changed_soulseek_login_is_written_and_the_rest_of_the_file_is_kept():
    slskd.seed(SLSKD, soulseek(), SUBNET)
    path = common.config_path(SLSKD, "slskd.yml")
    edited = yaml.safe_load(path.read_text())
    edited["shares"] = {"directories": ["/music"]}
    key = edited["web"]["authentication"]["api_keys"]["opus"]["key"]
    path.write_text(yaml.safe_dump(edited))

    assert slskd.seed(SLSKD, soulseek(), SUBNET) is False
    assert slskd.seed(SLSKD, soulseek(password="n3w'pass"), SUBNET) is True
    now = yaml.safe_load(path.read_text())
    assert now["soulseek"]["password"] == "n3w'pass"
    assert now["shares"] == {"directories": ["/music"]}
    assert now["web"]["authentication"]["api_keys"]["opus"]["key"] == key

    assert slskd.seed(SLSKD, soulseek(username=""), SUBNET) is False
    assert yaml.safe_load(path.read_text())["soulseek"]["username"] == "filip"


def test_slskd_given_the_landing_root_itself_is_moved_into_its_own_folder():
    path = common.config_path(SLSKD, "slskd.yml")
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({"directories": {"downloads": settings.landing_root}}))
    assert slskd.seed(SLSKD, soulseek(), SUBNET) is True
    assert yaml.safe_load(path.read_text())["directories"]["downloads"] == \
        f"{settings.landing_root}/soulseek"


def test_qbittorrent_gets_a_machine_login_once():
    made = qbittorrent.credentials(RuntimeConfig({}))
    assert made["qbittorrent_bundled_user"] == "opus" and len(made["qbittorrent_bundled_password"]) > 20
    assert qbittorrent.credentials(RuntimeConfig(made)) == {}


@pytest.mark.parametrize(("value", "written"), [
    ("opus", "opus"),
    ("/landing/.incomplete", "/landing/.incomplete"),
    ("abc #123", '"abc #123"'),
    ("a,b", '"a,b"'),
    ('say "hi"\\', '"say \\"hi\\"\\\\"'),
    ("@home", '"@@home"'),
    ("@ByteArray(abc=:def=)", '"@ByteArray(abc=:def=)"'),
])
def test_qbittorrent_values_are_written_as_qt_reads_them(value, written):
    assert qbittorrent.conf_value(value) == written


def test_qbittorrent_is_seeded_once_with_its_login_and_its_network():
    runtime = RuntimeConfig({"qbittorrent_bundled_user": "abc #123",
                             "qbittorrent_bundled_password": "secret"})
    assert qbittorrent.seed(QBITTORRENT, runtime, SUBNET) is True
    path = common.config_path(QBITTORRENT, "qBittorrent", "qBittorrent.conf")
    conf = configparser.RawConfigParser()
    conf.optionxform = str
    conf.read_string(path.read_text())
    preferences = conf["Preferences"]
    assert preferences["WebUI\\Username"] == '"abc #123"'
    assert preferences["WebUI\\Password_PBKDF2"].startswith('"@ByteArray(')
    assert preferences["WebUI\\AuthSubnetWhitelist"] == SUBNET
    assert "WebUI\\Port=8080" in path.read_text()

    before = path.read_text()
    assert qbittorrent.seed(QBITTORRENT, runtime, "10.0.0.0/8") is False
    assert path.read_text() == before


class FakeProwlarr:
    """Answers its API only under the base path it runs with, and only with the
    key; a restart is a moment of refused connections and then the base path it
    was last told."""

    def __init__(self, url_base, apply_on_restart=True, solver_warming=0):
        self.url_base = self.told = url_base
        self.apply_on_restart = apply_on_restart
        self.down = 0
        self.asked = []
        self.solver_warming = solver_warming
        self.tags = []
        self.proxies = []

    def handle(self, request):
        if self.down:
            self.down -= 1
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        self.asked.append(f"{request.method} {path}")
        if not (path == self.url_base or path.startswith(f"{self.url_base}/")):
            return httpx.Response(404)
        rest = path[len(self.url_base):]
        if not rest.startswith("/api/"):
            return httpx.Response(200, text="<html>prowlarr</html>")
        if request.headers.get("x-api-key") != "k3y":
            return httpx.Response(401)
        if request.method == "GET" and rest == "/api/v1/config/host":
            return httpx.Response(200, json={"id": 1, "urlBase": self.url_base})
        if request.method == "PUT" and rest == "/api/v1/config/host/1":
            self.told = json.loads(request.content)["urlBase"]
            return httpx.Response(202, json={})
        return self.solver(request, rest)

    def solver(self, request, rest):
        sent = json.loads(request.content) if request.content else None
        if rest == "/api/v1/tag":
            if request.method == "POST":
                self.tags.append({"id": len(self.tags) + 1, **sent})
                return httpx.Response(201, json=self.tags[-1])
            return httpx.Response(200, json=self.tags)
        if rest == "/api/v1/indexerproxy/schema":
            return httpx.Response(200, json=[{
                "implementation": "FlareSolverr", "name": "", "tags": [],
                "fields": [{"name": "host", "value": "http://localhost:8191/"},
                           {"name": "requestTimeout", "value": 60}],
            }])
        if rest == "/api/v1/indexerproxy/test":
            if self.solver_warming:
                self.solver_warming -= 1
                return httpx.Response(400, json=[{"errorMessage": "Unable to connect"}])
            return httpx.Response(200, json={})
        if rest == "/api/v1/indexerproxy" and request.method == "POST":
            self.proxies.append({**sent, "id": len(self.proxies) + 1})
            return httpx.Response(201, json=self.proxies[-1])
        if rest == "/api/v1/indexerproxy":
            return httpx.Response(200, json=self.proxies)
        if request.method == "PUT" and rest.startswith("/api/v1/indexerproxy/"):
            self.proxies = [sent if p["id"] == sent["id"] else p for p in self.proxies]
            return httpx.Response(202, json=sent)
        return httpx.Response(404)

    async def restart(self, name):
        self.down = 2
        if self.apply_on_restart:
            self.url_base = self.told


async def configured_prowlarr(monkeypatch, config_xml, fake, use_vpn="false"):
    path = common.config_path(SPEC_BY_NAME["prowlarr"], "config.xml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_xml)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(fake.handle)}))
    return await prowlarr.configure(SPEC_BY_NAME["prowlarr"],
                                    RuntimeConfig({"prowlarr_use_vpn": use_vpn}), fake, {})


BASE = ui_base_path("prowlarr")


async def test_a_fresh_prowlarr_is_asked_at_its_root_and_then_under_its_base_path(monkeypatch, fast):
    fresh = FakeProwlarr("")
    learned = await configured_prowlarr(monkeypatch, "<Config><ApiKey>k3y</ApiKey></Config>", fresh)
    assert learned == {"prowlarr_bundled_api_key": "k3y"}
    assert fresh.asked[1:3] == ["GET /api/v1/config/host", "PUT /api/v1/config/host/1"]
    assert fresh.url_base == BASE and f"GET {BASE}/api/v1/config/host" in fresh.asked[3:]


async def test_a_prowlarr_already_moved_is_asked_there_and_not_restarted(monkeypatch, fast):
    moved = FakeProwlarr(BASE)
    await configured_prowlarr(
        monkeypatch, f"<Config><ApiKey>k3y</ApiKey><UrlBase>{BASE}</UrlBase></Config>", moved)
    assert all(a.split(" ", 1)[1].startswith(BASE) for a in moved.asked)
    assert moved.down == 0


async def test_a_prowlarr_that_did_not_take_its_base_path_is_refused(monkeypatch, fast):
    stuck = FakeProwlarr("", apply_on_restart=False)
    with pytest.raises(ContainerError, match="never started answering"):
        await configured_prowlarr(monkeypatch, "<Config><ApiKey>k3y</ApiKey><UrlBase></UrlBase></Config>",
                                  stuck)


async def test_prowlarr_is_given_its_solver_once_the_browser_answers(monkeypatch, fast):
    moved = FakeProwlarr(BASE, solver_warming=2)
    await configured_prowlarr(
        monkeypatch, f"<Config><ApiKey>k3y</ApiKey><UrlBase>{BASE}</UrlBase></Config>", moved)
    assert moved.asked.count(f"POST {BASE}/api/v1/indexerproxy/test") == 3
    assert moved.tags == [{"id": 1, "label": prowlarr.SOLVER_TAG}]
    [proxy] = moved.proxies
    assert proxy["tags"] == [1] and proxy["name"] == "FlareSolverr"
    assert {f["name"]: f["value"] for f in proxy["fields"]} == {
        "host": "http://opus_solver_prowlarr:8191/", "requestTimeout": 60}


async def test_a_solver_that_never_answers_fails_the_start(monkeypatch, fast):
    cold = FakeProwlarr(BASE, solver_warming=10**6)
    with pytest.raises(ContainerError, match="never passed its test: Unable to connect"):
        await configured_prowlarr(
            monkeypatch, f"<Config><ApiKey>k3y</ApiKey><UrlBase>{BASE}</UrlBase></Config>", cold)
    assert cold.proxies == []


async def test_prowlarr_moved_into_its_tunnel_keeps_one_solver_reached_across_it(monkeypatch, fast):
    moved = FakeProwlarr(BASE)
    xml = f"<Config><ApiKey>k3y</ApiKey><UrlBase>{BASE}</UrlBase></Config>"
    await configured_prowlarr(monkeypatch, xml, moved)
    await configured_prowlarr(monkeypatch, xml, moved, use_vpn="true")
    [proxy] = moved.proxies
    assert moved.tags == [{"id": 1, "label": prowlarr.SOLVER_TAG}]
    assert proxy["fields"][0]["value"] == "http://localhost:8191/"


class Stop(Exception):
    pass


@pytest.mark.parametrize("use_vpn", ["true", "false"])
async def test_an_engine_inside_its_tunnel_is_reached_through_the_tunnel(monkeypatch, use_vpn):
    asked = []

    async def file(path):
        return "<Config><ApiKey>abc</ApiKey></Config>\napi_key = abc\n"

    async def http(url, headers=None):
        asked.append(url)
        raise Stop()

    async def address(docker, name):
        asked.append(name)
        raise Stop()
    monkeypatch.setattr(common, "await_file", file)
    monkeypatch.setattr(common, "await_http", http)
    monkeypatch.setattr(sabnzbd, "address", address)
    for name, module in (("prowlarr", prowlarr), ("sabnzbd", sabnzbd)):
        asked.clear()
        with pytest.raises(Stop):
            await module.configure(SPEC_BY_NAME[name], RuntimeConfig({f"{name}_use_vpn": use_vpn}),
                                   None, {})
        expected = f"opus_vpn_{name}" if use_vpn == "true" else f"opus_{name}:"
        assert expected in asked[0] + ":"


async def test_a_changed_usenet_host_replaces_the_server_the_old_host_named(monkeypatch, fast):
    config = common.config_path(SPEC_BY_NAME["sabnzbd"], "sabnzbd.ini")
    config.parent.mkdir(parents=True)
    config.write_text("api_key = abc\n")
    calls = []

    def handler(request):
        params = dict(request.url.params)
        calls.append(params)
        if params.get("mode") == "get_config" and params.get("section") == "servers":
            return httpx.Response(200, json={"config": {"servers": [{"name": "old.host"}]}})
        if params.get("mode") == "set_config" and "value" in params:
            return httpx.Response(200, json={"config": {params["section"]: {
                params["keyword"]: params["value"]}}})
        return httpx.Response(200, json={"status": True})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))

    async def address(docker, name):
        return "10.0.0.5"
    monkeypatch.setattr(sabnzbd, "address", address)
    runtime = RuntimeConfig({"sabnzbd_usenet_host": "new.host", "sabnzbd_usenet_username": "filip",
                             "sabnzbd_usenet_password": "n3w", "sabnzbd_usenet_port": "119",
                             "sabnzbd_usenet_ssl": "false", "sabnzbd_usenet_connections": "8"})
    learned = await sabnzbd.configure(SPEC_BY_NAME["sabnzbd"], runtime, None,
                                      {"sabnzbd_usenet_host": "old.host"})
    assert learned == {"sabnzbd_bundled_api_key": "abc"}
    deleted = [c for c in calls if c.get("mode") == "del_config"]
    assert deleted == [{"apikey": "abc", "output": "json", "mode": "del_config",
                        "section": "servers", "keyword": "old.host"}]
    server = next(c for c in calls if c.get("section") == "servers" and c.get("mode") == "set_config")
    assert (server["keyword"], server["username"], server["password"]) == ("new.host", "filip", "n3w")
    assert (server["port"], server["ssl"], server["connections"]) == ("119", "0", "8")


class FakeEngine:
    def __init__(self, name):
        self.name, self.mode = name, EngineMode.BUNDLED
        self.spec = SPEC_BY_NAME[name]


class RunningDocker:
    async def inspect(self, name):
        return {"State": {"Running": True}}


@pytest.fixture
def bundled(monkeypatch):
    """Two running bundled engines whose containers match their definitions, and
    a bring-up that records who was brought up and how many at once."""
    seen = {"running": 0, "most": 0, "started": []}

    async def bring(name, previous):
        seen["running"] += 1
        seen["most"] = max(seen["most"], seen["running"])
        await asyncio.sleep(0.05)
        seen["running"] -= 1
        seen["started"].append(name)
        return {}

    async def settled(docker, engine, runtime):
        return True
    monkeypatch.setattr(provision, "Docker", RunningDocker)
    monkeypatch.setattr(provision, "build_engines",
                        lambda runtime: [FakeEngine("sabnzbd"), FakeEngine("slskd")])
    monkeypatch.setattr(provision, "_settled", settled)
    monkeypatch.setattr(provision, "_start", bring)
    return seen


async def applied(previous):
    provision.apply_saved(previous)
    while provision._applies:
        await asyncio.sleep(0.01)


async def test_a_save_that_touches_no_engine_restarts_none(bundled):
    await applied({"ytdlp_landing_dir": "/landing/web"})
    assert bundled["started"] == []


async def test_a_changed_soulseek_login_brings_up_slskd_alone(bundled):
    await applied({"slskd_soulseek_password": "old"})
    assert bundled["started"] == ["slskd"]


async def test_a_changed_usenet_account_brings_up_sabnzbd_alone(bundled):
    await applied({"sabnzbd_usenet_username": "old"})
    assert bundled["started"] == ["sabnzbd"]


class LostDocker:
    async def inspect(self, name):
        return None if name == "opus_sabnzbd" else {"State": {"Running": True}}


async def test_a_bundled_engine_whose_container_is_gone_is_brought_back(bundled, monkeypatch):
    monkeypatch.setattr(provision, "Docker", LostDocker)
    await applied({})
    assert bundled["started"] == ["sabnzbd"]


@pytest.mark.parametrize("name", sorted(n for n, s in SPEC_BY_NAME.items() if s.image))
def test_every_bundled_engine_has_a_definition(name):
    body = provision._definition(SPEC_BY_NAME[name], RuntimeConfig({}), False)
    assert body["Image"] == SPEC_BY_NAME[name].image


class SolverlessDocker:
    async def inspect(self, name):
        return None if name == "opus_solver_prowlarr" else {"State": {"Running": True}}


async def test_a_prowlarr_whose_solver_is_gone_is_brought_up_again(bundled, monkeypatch):
    monkeypatch.setattr(provision, "Docker", SolverlessDocker)
    monkeypatch.setattr(provision, "build_engines", lambda runtime: [
        FakeEngine("prowlarr"), FakeEngine("sabnzbd")])
    await applied({})
    assert bundled["started"] == ["prowlarr"]


async def test_prowlarr_comes_up_after_the_clients_it_is_wired_to(bundled, monkeypatch):
    async def unsettled(docker, engine, runtime):
        return False
    monkeypatch.setattr(provision, "build_engines", lambda runtime: [
        FakeEngine("prowlarr"), FakeEngine("sabnzbd"), FakeEngine("qbittorrent")])
    monkeypatch.setattr(provision, "_settled", unsettled)
    await applied({})
    assert bundled["started"] == ["sabnzbd", "qbittorrent", "prowlarr"]


async def test_a_start_and_an_apply_of_the_same_engine_never_overlap(bundled):
    await asyncio.gather(provision.start("slskd"), applied({"slskd_soulseek_password": "old"}),
                         provision.start("slskd"))
    assert bundled["most"] == 1 and bundled["started"] == ["slskd"] * 3


async def test_a_failed_bring_up_is_kept_for_its_card_until_one_succeeds(monkeypatch):
    async def failing(name, previous):
        raise ContainerError("the image would not pull from https://registry/x?token=hunter2")

    async def fine(name, previous):
        return {"running": True}

    async def absent(docker, name):
        return {"present": True, "running": False}
    monkeypatch.setattr(provision, "_start", failing)
    monkeypatch.setattr(provision, "state", absent)
    with pytest.raises(ContainerError):
        await provision.start("prowlarr")
    described = await provision.describe(None, SPEC_BY_NAME["prowlarr"], False)
    assert "would not pull" in described["error"] and "hunter2" not in described["error"]

    monkeypatch.setattr(provision, "_start", fine)
    await provision.start("prowlarr")
    assert "prowlarr" not in provision.failures


@pytest.mark.parametrize(("reseeded", "created", "restarted"), [
    (True, False, True),
    (True, True, False),
    (False, False, False),
])
async def test_a_running_engine_whose_configuration_changed_is_restarted(monkeypatch, reseeded,
                                                                         created, restarted):
    restarts = []

    class Docker:
        async def restart(self, name):
            restarts.append(name)

        async def inspect(self, name):
            return None

    async def seed(docker, spec, runtime):
        return reseeded

    async def ensure(docker, name, definition):
        return created

    async def describe(docker, spec, use_vpn):
        return {}
    monkeypatch.setattr(provision, "_seed", seed)
    monkeypatch.setattr(provision, "ensure", ensure)
    monkeypatch.setattr(provision, "describe", describe)
    await provision.bring_up(Docker(), SLSKD, RuntimeConfig({}), False)
    assert restarts == (["opus_slskd"] if restarted else [])


async def test_an_engine_taken_out_of_its_tunnel_leaves_no_tunnel_behind(monkeypatch):
    removed = []

    class Docker:
        async def inspect(self, name):
            return {"Config": {"Labels": {OWNER_LABEL: "engine"}}} if name == "opus_vpn_slskd" else None

        async def stop(self, name):
            removed.append(f"stop {name}")

        async def remove(self, name):
            removed.append(f"remove {name}")

    async def seed(docker, spec, runtime):
        return False

    async def ensure(docker, name, definition):
        return True

    async def describe(docker, spec, use_vpn):
        return {}
    monkeypatch.setattr(provision, "_seed", seed)
    monkeypatch.setattr(provision, "ensure", ensure)
    monkeypatch.setattr(provision, "describe", describe)
    await provision.bring_up(Docker(), SLSKD, RuntimeConfig({}), False)
    assert removed == ["stop opus_vpn_slskd", "remove opus_vpn_slskd"]


@pytest.fixture
def taken_down(monkeypatch):
    taken = []

    async def take_down(docker, spec, use_vpn):
        taken.append(spec.name)
        if spec.name == "slskd":
            raise ContainerError("opus_slskd was not created by OPUS; refusing to remove it")
        return {}
    monkeypatch.setattr(provision, "take_down", take_down)
    return taken


async def test_a_save_that_takes_an_engine_off_bundled_takes_its_container_down(taken_down):
    await update_settings({"sabnzbd_mode": "external", "prowlarr_mode": "bundled"})
    await provision.retire({"sabnzbd_mode": "bundled", "prowlarr_mode": "external"})
    assert taken_down == ["sabnzbd"] and "sabnzbd" not in provision.failures


async def test_a_container_that_would_not_go_says_so_on_its_card(taken_down):
    await provision.retire({"slskd_mode": "bundled"})
    assert taken_down == ["slskd"] and "not created by OPUS" in provision.failures["slskd"]


async def test_qbittorrent_is_told_the_engines_network_on_every_start(monkeypatch, fast):
    told = []

    def handler(request):
        if request.url.path == "/api/v2/app/setPreferences":
            told.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(403 if len(told) == 1 else 200)
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        return httpx.Response(200)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))

    class Docker:
        async def network_subnet(self, name):
            assert name == settings.engine_network
            return SUBNET
    runtime = RuntimeConfig({"qbittorrent_use_vpn": "false", "qbittorrent_bundled_user": "opus",
                             "qbittorrent_bundled_password": "p"})
    assert await qbittorrent.configure(QBITTORRENT, runtime, Docker(), {}) == {}
    assert len(told) == 2
    assert json.loads(told[-1]["json"]) == {"bypass_auth_subnet_whitelist_enabled": True,
                                            "bypass_auth_subnet_whitelist": SUBNET}


def test_a_share_outside_the_media_directory_is_never_bound(monkeypatch):
    monkeypatch.setattr(settings, "media_host_dir", "/mnt/media")
    with pytest.raises(ContainerError, match="media directory"):
        provision._definition(SLSKD, RuntimeConfig({"slskd_share_dir": "/etc"}), False)
    definition = provision._definition(SLSKD, RuntimeConfig({"slskd_share_dir": "/mnt/media/music"}),
                                       False)
    assert "/mnt/media/music:/music:ro" in definition["HostConfig"]["Binds"]
