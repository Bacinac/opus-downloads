import asyncio
import json
import uuid
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

    def has(self, engine, key):
        return True


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
    batches, posted, deleted = {}, [], []

    def handler(request):
        path = request.url.path
        if path == "/api/v0/options":
            return httpx.Response(200, json={"directories": {"downloads": "/downloads"}})
        if path == "/api/v0/transfers/downloads/batches" and request.method == "POST":
            body = json.loads(request.content)
            posted.append(body)
            batches[body["id"]] = []
            return httpx.Response(201, json={"batch": {"id": body["id"]}, "failures": []})
        if path.startswith("/api/v0/transfers/downloads/batches/") and request.method == "GET":
            batch_id = path.rsplit("/", 1)[1]
            if batch_id not in batches:
                return httpx.Response(404)
            return httpx.Response(200, json={"transfers": batches[batch_id]})
        if request.method == "DELETE":
            deleted.append(path)
            return httpx.Response(204)
        return httpx.Response(404)
    route(handler)
    landing = f"{settings.landing_root}/soulseek"
    return engine(slskd.SlskdEngine, "slskd", "http://slskd:5030", landing), batches, posted, deleted


async def batch(eng, peer, namespace="music"):
    reference = await eng.reference(peer, namespace, uuid.uuid4().hex)
    return await eng.grab({**peer, "_prepared": reference}, namespace)


async def test_slskd_grabs_with_explicit_isolated_batch_destinations(soulseek, peer):
    eng, _, posted, _ = soulseek
    first, second = await asyncio.gather(batch(eng, peer), batch(eng, peer, "other"))
    assert first["landing"] != second["landing"]
    assert {body["options"]["destination"] for body in posted} == {
        first["destination"], second["destination"]}


async def test_slskd_cancel_removes_only_its_batch_transfers_and_files(soulseek, peer):
    eng, batches, _, deleted = soulseek
    ref = await batch(eng, peer)
    landed = Path(await eng.completed_path(ref))
    landed.mkdir(parents=True)
    for name in ref["files"]:
        (landed / name).write_text(name)
    batches[ref["batch"]] = [
        {"id": "t1", "filename": peer["files"][0]["filename"], "state": "Completed, Succeeded"},
        {"id": "t2", "filename": peer["files"][1]["filename"], "state": "InProgress"}]
    await eng.cancel(ref)
    assert sorted(deleted) == ["/api/v0/transfers/downloads/peer1/t1",
                               "/api/v0/transfers/downloads/peer1/t2"]
    assert not landed.exists()


async def test_concurrent_jobs_persist_distinct_folder_ownership_and_old_cleanup_keeps_new_files(soulseek, peer, monkeypatch):
    from opus import jobs
    from opus.models import Job
    from opus.db import SessionLocal
    from opus.settings_store import current_runtime

    eng, batches, posted, deleted = soulseek
    monkeypatch.setattr(jobs, "build_engine", lambda runtime, name: eng)
    runtime = await current_runtime()
    first, second = await asyncio.gather(
        jobs.start(runtime, peer, "music", "library"),
        jobs.start(runtime, {**peer, "username": "peer2"}, "other", "test"))
    assert len(posted) == 2
    assert first.landing_claim != second.landing_claim
    first_dir, second_dir = Path(first.landing_claim), Path(second.landing_claim)
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    for name in first.job_ref["files"]:
        (second_dir / name).write_bytes(b"new acquisition")
    batches[first.id] = []
    batches[second.id] = [{"id": "new-transfer", "filename": peer["files"][0]["filename"]}]
    await jobs.cancel(runtime, first.id)
    assert deleted == []
    assert all((second_dir / name).read_bytes() == b"new acquisition" for name in first.job_ref["files"])
    async with SessionLocal() as session:
        assert await session.get(Job, first.id) is None
        assert (await session.get(Job, second.id)).landing_claim == second.landing_claim


async def test_a_bundled_engines_root_survives_batch_path_translation(route, peer):
    def handler(request):
        if request.url.path == "/api/v0/options":
            return httpx.Response(200, json={
                "directories": {"downloads": f"{settings.landing_root}/soulseek"}})
        return httpx.Response(201, json={"failures": []})
    route(handler)
    eng = engine(slskd.SlskdEngine, "slskd", "http://slskd:5030", settings.landing_root,
                 mode="bundled")
    ref = await batch(eng, peer)
    assert await eng.completed_path(ref) == str(Path(settings.landing_root, "soulseek", ref["destination"]))


async def test_legacy_upgrade_pins_transfer_ids_and_old_history_cannot_clean_the_current_owner(route, peer, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from opus import claims, jobs
    from opus.db import SessionLocal
    from opus.models import Job, JobState, Setting
    from opus.settings_store import current_runtime

    deleted = []

    def handler(request):
        path = request.url.path
        if path.endswith("/options"):
            return httpx.Response(200, json={"directories": {"downloads": "/downloads"}})
        if request.method == "DELETE":
            deleted.append(path)
            return httpx.Response(204)
        user = path.rsplit("/", 1)[1]
        return httpx.Response(200, json={"directories": [{"files": [
            {"id": user + "-transfer", "filename": peer["files"][0]["filename"]}]}]})

    route(handler)
    eng = engine(slskd.SlskdEngine, "slskd", "http://slskd:5030", f"{settings.landing_root}/soulseek")
    monkeypatch.setattr(claims, "build_engine", lambda runtime, name: eng)
    monkeypatch.setattr(jobs, "build_engine", lambda runtime, name: eng)
    ids = [uuid.uuid4().hex for _ in range(2)]
    async with SessionLocal() as session:
        for index, job_id in enumerate(ids):
            ref = {"username": f"peer{index}", "directory": peer["directory"],
                   "files": [file["filename"] for file in peer["files"]]}
            session.add(Job(id=job_id, engine="slskd", namespace="music", grab_ref=peer,
                            job_ref=ref, state=JobState.COMPLETE,
                            created_at=datetime.now(timezone.utc) + timedelta(seconds=index)))
        await session.commit()
    await claims.adopt()
    await claims.adopt()
    landing = Path(settings.landing_root, "soulseek", "Album (2001)")
    landing.mkdir(parents=True)
    newer_file = landing / "01 A.flac"
    newer_file.write_bytes(b"current owner's acquisition")
    async with SessionLocal() as session:
        assert (await session.get(Setting, "landing_claims_version")).value == "1"
        assert (await session.get(Job, ids[0])).landing_claim is None
        current = await session.get(Job, ids[1])
        assert current.landing_claim == str(landing)
        assert current.job_ref["transfers"] == ["peer1-transfer"]
    await jobs.cancel(await current_runtime(), ids[0])
    assert newer_file.read_bytes() == b"current owner's acquisition"
    assert deleted == []


@pytest.mark.parametrize(("states", "expected", "seen"), [
    ([], "queued", False),
    (["Completed, Succeeded", "Completed, Errored"], "failed", True),
    (["Completed, Succeeded", "Completed, Succeeded"], "complete", True),
    (["Completed, Succeeded", "InProgress"], "downloading", True),
])
async def test_slskd_status(soulseek, peer, states, expected, seen):
    eng, batches, _, _ = soulseek
    job_ref = await batch(eng, peer)
    batches[job_ref["batch"]] = [
        {"filename": file["filename"], "state": state, "size": 10, "bytesTransferred": 5}
        for file, state in zip(peer["files"], states)]
    status = await eng.status(job_ref)
    assert (status.state, status.seen) == (expected, seen)


@pytest.mark.parametrize("namespace", ["../etc", "/etc", "music/../../etc"])
async def test_slskd_refuses_a_batch_destination_outside_its_root(soulseek, peer, namespace):
    eng, _, posted, _ = soulseek
    with pytest.raises(EngineError, match="invalid batch namespace"):
        await eng.reference(peer, namespace, uuid.uuid4().hex)
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
        await eng.reference({"username": "peer", "files": []}, "music", uuid.uuid4().hex)


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
