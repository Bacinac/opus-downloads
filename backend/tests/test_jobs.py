import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update

from opus import db
from opus.config import settings
from opus.db import SessionLocal
from opus.engines import sabnzbd
from opus.engines.base import JobStatus
from opus.models import Job, JobState
from opus.settings_store import store_credentials


@pytest.fixture(autouse=True)
async def configured():
    await store_credentials({"sabnzbd_api_key": "k"})


async def add(engine="sabnzbd", state=JobState.QUEUED, seen_ago=0, landing=None, job_ref=None):
    job = Job(id=uuid.uuid4().hex, engine=engine, app="test", namespace="music", title="t",
              grab_ref={"engine": engine}, job_ref=job_ref or {"nzo_id": "x"}, state=state,
              landing_path=landing,
              seen_at=datetime.now(timezone.utc) - timedelta(minutes=seen_ago))
    async with SessionLocal() as session:
        session.add(job)
        await session.commit()
    return job.id


async def stored(job_id):
    async with SessionLocal() as session:
        return await session.get(Job, job_id)


def answering(monkeypatch, status=None, raises=None, calls=None):
    async def fake(self, job_ref):
        if calls is not None:
            calls.append(db.engine.pool.checkedout())
        if raises:
            raise raises
        return status
    monkeypatch.setattr(sabnzbd.SabnzbdEngine, "status", fake)


UNSEEN = JobStatus(state="queued", detail="not in SABnzbd's queue or history", seen=False)


async def test_a_job_unseen_for_a_moment_keeps_its_state(api, monkeypatch):
    answering(monkeypatch, UNSEEN)
    job = await add(state=JobState.DOWNLOADING, seen_ago=2)
    resp = await api.get(f"/api/jobs/{job}")
    assert resp.status_code == 200 and resp.json()["state"] == "downloading"


async def test_a_job_unseen_past_the_grace_fails(api, monkeypatch):
    answering(monkeypatch, UNSEEN)
    job = await add(seen_ago=20)
    body = (await api.get(f"/api/jobs/{job}")).json()
    assert body["state"] == "failed"
    assert "no record of this job for 20 minutes" in body["error"]
    assert (await stored(job)).state is JobState.FAILED


async def test_a_landed_job_never_regresses_and_its_engine_is_not_asked(api, monkeypatch):
    calls = []
    answering(monkeypatch, UNSEEN, calls=calls)
    job = await add(state=JobState.COMPLETE, seen_ago=600, landing="/landing/usenet/music/x")
    body = (await api.get(f"/api/jobs/{job}")).json()
    assert body["state"] == "complete" and calls == []


async def test_a_seen_job_records_its_progress_and_when_it_was_seen(api, monkeypatch):
    answering(monkeypatch, JobStatus(state="downloading", progress=0.4, detail="12 MB left"))
    job = await add(state=JobState.QUEUED, seen_ago=5)
    body = (await api.get(f"/api/jobs/{job}")).json()
    row = await stored(job)
    assert body["progress"] == 0.4 and body["detail"] == "12 MB left"
    assert body["seen_at"] is not None
    assert datetime.now(timezone.utc) - row.seen_at < timedelta(seconds=10)
    assert row.state is JobState.DOWNLOADING


async def test_no_database_connection_is_held_while_the_engine_answers(api, monkeypatch):
    calls = []
    answering(monkeypatch, JobStatus(state="downloading"), calls=calls)
    job = await add()
    assert (await api.get(f"/api/jobs/{job}")).status_code == 200

    async def cancel(self, job_ref):
        calls.append(db.engine.pool.checkedout())
    monkeypatch.setattr(sabnzbd.SabnzbdEngine, "cancel", cancel)
    assert (await api.delete(f"/api/jobs/{job}")).status_code == 204
    assert calls == [0, 0]


async def test_an_engine_failure_is_a_bad_gateway_that_says_nothing_secret(api, monkeypatch):
    answering(monkeypatch, raises=httpx.ConnectError(
        "failed for url 'http://sab:8080/api?apikey=hunter2&mode=queue'"))
    job = await add()
    resp = await api.get(f"/api/jobs/{job}")
    assert resp.status_code == 502 and "hunter2" not in resp.text


async def test_a_job_on_a_switched_off_engine_conflicts_and_can_still_be_dropped(api):
    job = await add(engine="qbittorrent", job_ref={"hash": "h"})
    resp = await api.get(f"/api/jobs/{job}")
    assert resp.status_code == 409 and "not configured" in resp.json()["detail"]
    assert (await api.delete(f"/api/jobs/{job}")).status_code == 204
    assert await stored(job) is None


async def test_a_job_of_an_unknown_engine_is_refused_and_can_be_dropped(api):
    job = await add(engine="nonesuch")
    resp = await api.get(f"/api/jobs/{job}")
    assert resp.status_code == 400 and "unknown engine" in resp.json()["detail"]
    assert (await api.delete(f"/api/jobs/{job}")).status_code == 204


async def test_a_missing_job_is_not_found(api):
    assert (await api.get("/api/jobs/missing")).status_code == 404
    assert (await api.delete("/api/jobs/missing")).status_code == 404


async def test_an_in_process_job_whose_run_is_gone_has_ended_and_drops(api):
    job = await add(engine="ytdlp", job_ref={"run": "f" * 32})
    body = (await api.get(f"/api/jobs/{job}")).json()
    assert body["state"] == "failed" and "no longer exists" in body["error"]
    assert (await api.delete(f"/api/jobs/{job}")).status_code == 204
    assert await stored(job) is None


@pytest.mark.parametrize(("grab", "status", "said"), [
    ({"grab_ref": {"engine": "prowlarr"}, "namespace": "music"}, 400, "cannot grab"),
    ({"grab_ref": {}, "namespace": "music"}, 400, "does not name an engine"),
    ({"grab_ref": {"engine": "qbittorrent"}, "namespace": "music"}, 400, "not configured"),
    ({"grab_ref": {"engine": "ytdlp", "url": "https://x"}, "namespace": "../etc"}, 422, ""),
    ({"grab_ref": {"engine": "ytdlp", "url": "https://x"}, "namespace": "Music"}, 422, ""),
])
async def test_grabs_that_are_refused(api, grab, status, said):
    resp = await api.post("/api/grab", json=grab)
    assert resp.status_code == status
    assert said in resp.text


def grabbing(monkeypatch, *, raises=None, while_grabbing=None):
    cancelled = []

    async def grab(self, grab_ref, namespace):
        async with SessionLocal() as session:
            rows = (await session.execute(select(Job))).scalars().all()
        assert [(r.job_ref, r.state) for r in rows] == [({}, JobState.QUEUED)]
        if while_grabbing:
            await while_grabbing(rows[0].id)
        if raises:
            raise raises
        return {"nzo_id": "n1"}

    async def cancel(self, job_ref):
        cancelled.append(job_ref)
    monkeypatch.setattr(sabnzbd.SabnzbdEngine, "grab", grab)
    monkeypatch.setattr(sabnzbd.SabnzbdEngine, "cancel", cancel)
    return cancelled


GRAB = {"grab_ref": {"engine": "sabnzbd", "nzb_url": "http://indexer/x", "title": "X"},
        "namespace": "music"}


async def test_the_job_is_written_before_its_download_starts(api, monkeypatch):
    grabbing(monkeypatch)
    resp = await api.post("/api/grab", json=GRAB)
    row = await stored(resp.json()["job_id"])
    assert (row.job_ref, row.state) == ({"nzo_id": "n1"}, JobState.QUEUED)


async def test_job_history_shows_safe_lifecycle_decisions(api, monkeypatch):
    grabbing(monkeypatch)
    job = (await api.post("/api/grab", json=GRAB)).json()["job_id"]
    answering(monkeypatch, JobStatus(state="downloading", detail="40%"))
    assert (await api.get(f"/api/jobs/{job}")).status_code == 200

    history = (await api.get(f"/api/jobs/{job}/history")).json()
    assert [event["kind"] for event in history] == ["state", "started", "accepted"]
    assert all("nzo_id" not in event["detail"] for event in history)


async def test_job_history_redacts_a_start_failure(api, monkeypatch):
    grabbing(monkeypatch, raises=httpx.ConnectError("http://sab:8080/api?apikey=hunter2 refused"))
    assert (await api.post("/api/grab", json=GRAB)).status_code == 502
    job = (await api.get("/api/jobs")).json()[0]["id"]
    history = (await api.get(f"/api/jobs/{job}/history")).json()
    assert history[0]["kind"] == "failed" and "hunter2" not in history[0]["detail"]


async def test_repeating_a_grab_key_returns_its_first_job_without_a_second_download(api, monkeypatch):
    calls = []

    async def grab(self, grab_ref, namespace):
        calls.append((grab_ref, namespace))
        return {"nzo_id": "n1"}

    monkeypatch.setattr(sabnzbd.SabnzbdEngine, "grab", grab)
    request = {**GRAB, "app": "library"}
    headers = {"Idempotency-Key": "a" * 32}
    first = await api.post("/api/grab", json=request, headers=headers)
    second = await api.post("/api/grab", json=request, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert calls == [(GRAB["grab_ref"], "music")]


async def test_a_grab_key_cannot_be_reused_for_another_release(api, monkeypatch):
    calls = []

    async def grab(self, grab_ref, namespace):
        calls.append((grab_ref, namespace))
        return {"nzo_id": "n1"}

    monkeypatch.setattr(sabnzbd.SabnzbdEngine, "grab", grab)
    headers = {"Idempotency-Key": "a" * 32}
    first = await api.post("/api/grab", json={**GRAB, "app": "library"}, headers=headers)
    changed = {**GRAB, "app": "library", "namespace": "movies"}
    second = await api.post("/api/grab", json=changed, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 409
    assert "already used for another grab" in second.json()["detail"]
    assert calls == [(GRAB["grab_ref"], "music")]


async def test_a_grab_key_needs_an_app_and_has_a_safe_format(api):
    missing_app = await api.post("/api/grab", json=GRAB, headers={"Idempotency-Key": "a" * 32})
    malformed = await api.post("/api/grab", json={**GRAB, "app": "library"},
                               headers={"Idempotency-Key": "short"})

    assert missing_app.status_code == 409
    assert malformed.status_code == 422


async def test_a_download_that_would_not_start_leaves_a_failed_job(api, monkeypatch):
    grabbing(monkeypatch, raises=httpx.ConnectError("http://sab:8080/api?apikey=hunter2 refused"))
    resp = await api.post("/api/grab", json=GRAB)
    assert resp.status_code == 502
    jobs = (await api.get("/api/jobs")).json()
    assert [j["state"] for j in jobs] == ["failed"] and "hunter2" not in jobs[0]["error"]


async def test_a_job_dropped_while_its_download_starts_stops_that_download(api, monkeypatch):
    async def drop(job_id):
        assert (await api.get(f"/api/jobs/{job_id}")).json()["state"] == "queued"
        assert (await api.delete(f"/api/jobs/{job_id}")).status_code == 204
    cancelled = grabbing(monkeypatch, while_grabbing=drop)
    resp = await api.post("/api/grab", json=GRAB)
    assert resp.status_code == 404 and cancelled == [{"nzo_id": "n1"}]
    assert (await api.get("/api/jobs")).json() == []


async def test_a_start_that_never_came_back_fails(api):
    job = await add()
    async with SessionLocal() as session:
        await session.execute(update(Job).where(Job.id == job).values(
            job_ref={}, created_at=datetime.now(timezone.utc) - timedelta(minutes=20)))
        await session.commit()
    body = (await api.get(f"/api/jobs/{job}")).json()
    assert body["state"] == "failed" and "never started" in body["error"]


async def test_a_landing_path_outside_the_engine_is_refused(api, monkeypatch):
    answering(monkeypatch, JobStatus(state="complete", progress=1.0))

    async def completed_path(self, job_ref):
        return f"{settings.landing_root}/usenet/../../etc"
    monkeypatch.setattr(sabnzbd.SabnzbdEngine, "completed_path", completed_path)
    await store_credentials({"sabnzbd_landing_dir": f"{settings.landing_root}/usenet"})
    job = await add()
    resp = await api.get(f"/api/jobs/{job}")
    assert resp.status_code == 502 and "not inside its landing directory" in resp.json()["detail"]
    assert (await stored(job)).landing_path is None
