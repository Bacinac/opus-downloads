from pathlib import Path

import httpx
import pytest

from opus.config import settings
from opus.engines import qbittorrent, sabnzbd, slskd
from opus.engines.base import EngineConfig, EngineError
from opus.engines.catalog import SPEC_BY_NAME


class Creds:
    def get(self, engine, key):
        return "k"


@pytest.fixture
def route(monkeypatch):
    real = httpx.AsyncClient

    def install(handler):
        def client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)
        monkeypatch.setattr(httpx, "AsyncClient", client)
    return install


def engine(kind, name, url, landing, mode="external"):
    return kind(SPEC_BY_NAME[name], EngineConfig(
        name=name, mode=mode, enabled_flag=True, url=url, landing_dir=landing), Creds())


@pytest.fixture
def peer():
    return {"engine": "slskd", "username": "peer1", "directory": "@@x\\Music\\Album (2001)",
            "files": [{"filename": "@@x\\Music\\Album (2001)\\01 A.flac", "size": 1},
                      {"filename": "@@x\\Music\\Album (2001)\\02 B.flac", "size": 1}]}


@pytest.fixture
def soulseek(route):
    transfers, posted, deleted = [], [], []

    def handler(request):
        path = request.url.path
        if path == "/api/v0/options":
            return httpx.Response(200, json={"directories": {"downloads": "/downloads"}})
        if path == "/api/v0/transfers/downloads" and request.method == "GET":
            return httpx.Response(200, json=transfers)
        if path.startswith("/api/v0/transfers/downloads/") and request.method == "POST":
            posted.append(path)
            return httpx.Response(201)
        if path.startswith("/api/v0/transfers/downloads/") and request.method == "GET":
            user = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"directories": [
                d for u in transfers if u["username"] == user for d in u["directories"]]})
        if request.method == "DELETE":
            deleted.append(path)
            return httpx.Response(204)
        return httpx.Response(404)
    route(handler)
    landing = f"{settings.landing_root}/soulseek"
    return engine(slskd.SlskdEngine, "slskd", "http://slskd:5030", landing), transfers, posted, deleted


async def test_slskd_grabs_a_free_folder_and_refuses_one_already_in_use(soulseek, peer):
    eng, transfers, posted, _ = soulseek
    await eng.grab(peer, "music")
    assert posted == ["/api/v0/transfers/downloads/peer1"]

    transfers[:] = [{"username": "peer2", "directories": [{
        "directory": "@@y\\Other\\Album (2001)", "files": [{"filename": "x", "state": "InProgress"}]}]}]
    with pytest.raises(EngineError, match="already arriving from peer2"):
        await eng.grab(peer, "music")

    transfers[0]["directories"][0]["files"][0]["state"] = "Completed, Succeeded"
    landed = Path(eng.landing_dir(), "Album (2001)")
    landed.mkdir(parents=True)
    (landed / "01 A.flac").write_text("a")
    with pytest.raises(EngineError, match="still holds another download's files"):
        await eng.grab(peer, "music")


async def test_slskd_cancel_removes_transfers_files_and_the_emptied_folder(soulseek, peer):
    eng, transfers, _, deleted = soulseek
    job_ref = await eng.grab(peer, "music")
    landed = Path(await eng.completed_path(job_ref))
    assert landed == Path(settings.landing_root, "soulseek", "Album (2001)")
    landed.mkdir(parents=True)
    for name in ("01 A.flac", "02 B.flac"):
        (landed / name).write_text(name)
    transfers[:] = [{"username": "peer1", "directories": [{"directory": peer["directory"], "files": [
        {"id": "t1", "filename": peer["files"][0]["filename"], "state": "Completed, Succeeded"},
        {"id": "t2", "filename": peer["files"][1]["filename"], "state": "InProgress"}]}]}]
    await eng.cancel(job_ref)
    assert sorted(deleted) == ["/api/v0/transfers/downloads/peer1/t1",
                               "/api/v0/transfers/downloads/peer1/t2"]
    assert not landed.exists()


async def test_a_bundled_engines_own_subfolder_survives_the_shared_root_landing_dir(route, peer):
    # registry.py hands a bundled engine landing_dir=settings.landing_root — the
    # shared tree's own top — because a bundled engine has no mount point of its
    # own to translate from. But slskd, unlike sabnzbd/qbittorrent, downloads
    # into its own named subfolder of that tree (production's slskd.yml: shows
    # directories.downloads: /landing/soulseek). Rebasing onto landing_dir the
    # way an adopted engine's path is rebased would silently throw that
    # subfolder away and report the album one level too high.
    def handler(request):
        if request.url.path == "/api/v0/options":
            return httpx.Response(200, json={
                "directories": {"downloads": f"{settings.landing_root}/soulseek"}})
        return httpx.Response(200, json=[])
    route(handler)
    eng = engine(slskd.SlskdEngine, "slskd", "http://slskd:5030", settings.landing_root,
                mode="bundled")
    job_ref = await eng.grab(peer, "music")
    landed = await eng.completed_path(job_ref)
    assert landed == str(Path(settings.landing_root, "soulseek", "Album (2001)"))


@pytest.mark.parametrize(("states", "expected", "seen"), [
    ([], "queued", False),
    (["Completed, Succeeded", "Completed, Errored"], "failed", True),
    (["Completed, Succeeded", "Completed, Succeeded"], "complete", True),
    (["Completed, Succeeded", "InProgress"], "downloading", True),
])
async def test_slskd_status(soulseek, peer, states, expected, seen):
    eng, transfers, _, _ = soulseek
    job_ref = await eng.grab(peer, "music")
    transfers[:] = [{"username": "peer1", "directories": [{"directory": peer["directory"], "files": [
        {"filename": f["filename"], "state": s, "size": 10, "bytesTransferred": 5}
        for f, s in zip(peer["files"], states)]}]}] if states else []
    status = await eng.status(job_ref)
    assert (status.state, status.seen) == (expected, seen)


@pytest.mark.parametrize("directory", ["@@x\\Music\\..", "@@x\\Music\\.", ""])
async def test_slskd_refuses_a_folder_that_is_no_folder_of_its_own(soulseek, peer, directory):
    eng, _, posted, _ = soulseek
    with pytest.raises(EngineError, match="not inside"):
        await eng.grab({**peer, "directory": directory}, "music")
    assert posted == []


@pytest.mark.parametrize("storage", ["/complete/../etc", "/complete", "/complete/x/../..", "complete/x"])
async def test_sabnzbd_refuses_a_finished_folder_outside_its_own(route, storage):
    def handler(request):
        mode = request.url.params.get("mode")
        if mode == "history":
            return httpx.Response(200, json={"history": {"slots": [
                {"nzo_id": "n1", "status": "Completed", "storage": storage}]}})
        return httpx.Response(200, json={"config": {"misc": {"complete_dir": "/complete"}}})
    route(handler)
    eng = engine(sabnzbd.SabnzbdEngine, "sabnzbd", "http://sab:8080",
                 f"{settings.landing_root}/usenet")
    with pytest.raises(EngineError, match="not inside"):
        await eng.completed_path({"nzo_id": "n1"})


async def test_a_finished_path_is_given_normalised(route):
    route(lambda r: httpx.Response(200, json={"history": {"slots": [
        {"nzo_id": "n1", "status": "Completed", "storage": "/complete//music/./x/"}]}})
        if r.url.params.get("mode") == "history"
        else httpx.Response(200, json={"config": {"misc": {"complete_dir": "/complete/"}}}))
    eng = engine(sabnzbd.SabnzbdEngine, "sabnzbd", "http://sab:8080",
                 f"{settings.landing_root}/usenet")
    assert await eng.completed_path({"nzo_id": "n1"}) == f"{settings.landing_root}/usenet/music/x"


async def test_an_engine_landing_outside_the_landing_zone_is_refused(route):
    eng = engine(slskd.SlskdEngine, "slskd", "http://slskd:5030", "/etc")
    route(lambda r: httpx.Response(200, json={"directories": {"downloads": "/downloads"}}))
    with pytest.raises(EngineError, match="outside the landing zone"):
        await eng.completed_path({"directory": "x", "files": []})


@pytest.mark.parametrize(("torrent", "expected"), [
    ({"state": "moving", "progress": 1.0}, "downloading"),
    ({"state": "checkingUP", "progress": 1.0}, "downloading"),
    ({"state": "stalledUP", "progress": 1.0}, "complete"),
    ({"state": "stoppedUP", "progress": 1.0}, "complete"),
    ({"state": "error", "progress": 0.3}, "failed"),
    ({"state": "missingFiles", "progress": 1.0}, "failed"),
])
async def test_qbittorrent_status(route, torrent, expected):
    def handler(request):
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(204)
        return httpx.Response(200, json=[{**torrent, "name": "x", "eta": 8640000}])
    route(handler)
    eng = engine(qbittorrent.QbittorrentEngine, "qbittorrent", "http://qbt:8090",
                 f"{settings.landing_root}/torrent")
    status = await eng.status({"hash": "h"})
    assert status.state == expected
    assert status.eta_seconds is None


async def test_qbittorrent_holding_no_such_torrent_has_not_seen_it(route):
    route(lambda r: httpx.Response(204) if r.url.path.endswith("/login")
          else httpx.Response(200, json=[]))
    eng = engine(qbittorrent.QbittorrentEngine, "qbittorrent", "http://qbt:8090",
                 f"{settings.landing_root}/torrent")
    assert (await eng.status({"hash": "h"})).seen is False


@pytest.mark.parametrize("body", [b"<html>login</html>", b"d4:infoi1e", b"i5e", b"d3:foo"])
async def test_what_is_not_a_torrent_is_refused_without_naming_the_indexer(route, body):
    def handler(request):
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(204)
        if request.url.host == "indexer":
            return httpx.Response(200, content=body)
        if request.url.path == "/api/v2/app/preferences":
            return httpx.Response(200, json={"save_path": "/downloads"})
        return httpx.Response(200)
    route(handler)
    eng = engine(qbittorrent.QbittorrentEngine, "qbittorrent", "http://qbt:8090",
                 f"{settings.landing_root}/torrent")
    with pytest.raises(EngineError, match="not a torrent") as refused:
        await eng.grab({"engine": "qbittorrent", "title": "t",
                        "torrent_url": "http://indexer/dl?apikey=hunter2"}, "music")
    assert "hunter2" not in str(refused.value) and "indexer/dl" not in str(refused.value)


def sab(route, queue=(), history=()):
    def handler(request):
        mode = request.url.params.get("mode")
        if mode == "queue":
            return httpx.Response(200, json={"queue": {"slots": list(queue), "kbpersec": "100"}})
        if mode == "history":
            return httpx.Response(200, json={"history": {"slots": list(history)}})
        return httpx.Response(404)
    route(handler)
    return engine(sabnzbd.SabnzbdEngine, "sabnzbd", "http://sab:8080",
                  f"{settings.landing_root}/usenet")


async def test_sabnzbd_in_the_queue_is_downloading(route):
    eng = sab(route, queue=[{"nzo_id": "n1", "percentage": "40", "timeleft": "0:01:05",
                             "status": "Downloading", "mbleft": "6", "mb": "10"}])
    status = await eng.status({"nzo_id": "n1"})
    assert (status.state, status.progress, status.eta_seconds, status.speed_bps) == (
        "downloading", 0.4, 65, 102400)


@pytest.mark.parametrize(("slot", "expected"), [
    ({"status": "Completed", "storage": "/complete/music/x"}, "complete"),
    ({"status": "Failed", "fail_message": "out of retention"}, "failed"),
    ({"status": "Extracting"}, "downloading"),
])
async def test_sabnzbd_in_the_history(route, slot, expected):
    eng = sab(route, history=[{"nzo_id": "n1", **slot}])
    status = await eng.status({"nzo_id": "n1"})
    assert status.state == expected


async def test_sabnzbd_holding_the_job_nowhere_has_not_seen_it(route):
    assert (await sab(route).status({"nzo_id": "n1"})).seen is False


async def test_an_engine_that_answers_with_a_page_is_an_engine_error(route):
    route(lambda r: httpx.Response(200, text="<html>SABnzbd login</html>"))
    eng = engine(sabnzbd.SabnzbdEngine, "sabnzbd", "http://sab:8080",
                 f"{settings.landing_root}/usenet")
    with pytest.raises(EngineError, match="not JSON"):
        await eng.probe()
