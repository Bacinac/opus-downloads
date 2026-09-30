import asyncio
import threading
import time
from pathlib import Path

import pytest

from opus import runner
from opus.db import SessionLocal
from opus.engines.base import EngineConfig
from opus.engines.catalog import SPEC_BY_NAME
from opus.engines.ytdlp import YtdlpEngine
from opus.models import JobState, Run


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(runner, "FLUSH_SECONDS", 0.05)
    monkeypatch.setattr(runner, "CANCEL_TIMEOUT", 0.5)


async def until(run_id, *states, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = await runner.state(run_id)
        if run is None or run.state in states:
            return run
        await asyncio.sleep(0.05)
    return await runner.state(run_id)


async def gone(run_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and await runner.state(run_id) is not None:
        await asyncio.sleep(0.05)
    return await runner.state(run_id) is None


def writer(name, steps=5, fail=None):
    async def download(progress, workdir):
        for i in range(steps):
            progress.report(fraction=i / steps, detail=f"{name} {i}")
            (workdir / f"{name}.part").write_text(str(i))
            await asyncio.sleep(0.02)
        if fail:
            raise RuntimeError(fail)
        (workdir / f"{name}.flac").write_text("done")
        return str(workdir)
    return download


def held(gate: threading.Event, then_report=True):
    """A download inside a blocking library that notices nothing until it
    reports again."""
    def blocking(progress, workdir):
        (workdir / "partial.bin").write_text("x")
        gate.wait(10)
        if then_report:
            progress.report(detail="merged")
        return str(workdir)

    async def download(progress, workdir):
        return await asyncio.to_thread(blocking, progress, workdir)
    return download


async def test_same_title_claims_its_own_folder_and_a_failure_removes_only_its_own(tmp_path):
    a = await runner.start("t-claim", tmp_path, "Weezer", writer("a", 10), parallel=4)
    b = await runner.start("t-claim", tmp_path, "Weezer", writer("b", 2, fail="on purpose"),
                           parallel=4)
    ra, rb = await runner.state(a), await runner.state(b)
    assert ra.workdir != rb.workdir

    rb = await until(b, JobState.FAILED)
    assert rb.state is JobState.FAILED and rb.error == "on purpose"
    assert not Path(rb.workdir).exists()
    ra = await until(a, JobState.COMPLETE)
    assert ra.state is JobState.COMPLETE and (Path(ra.workdir) / "a.flac").exists()
    assert ra.landing_path == ra.workdir


async def test_a_failure_says_nothing_secret(tmp_path):
    run = await runner.start("t-secret", tmp_path, "X",
                             writer("x", 1, fail="GET https://api.example/x?apikey=hunter2 failed"),
                             parallel=1)
    failed = await until(run, JobState.FAILED)
    assert "hunter2" not in failed.error and "hunter2" not in failed.detail


async def test_cancelling_a_complete_run_drops_row_and_files(tmp_path):
    run = await runner.start("t-done", tmp_path, "Done", writer("d", 1), parallel=1)
    done = await until(run, JobState.COMPLETE)
    await runner.cancel(run)
    assert await runner.state(run) is None
    assert not Path(done.workdir).exists()


async def test_cancelling_a_run_that_is_gone_is_nothing():
    await runner.cancel("0" * 32)


async def test_a_cancel_that_times_out_leaves_the_run_to_remove_itself(tmp_path):
    gate = threading.Event()
    run = await runner.start("t-slow", tmp_path, "Slow", held(gate), parallel=1)
    await asyncio.sleep(0.2)
    workdir = Path((await runner.state(run)).workdir)

    await runner.cancel(run)
    stopping = await runner.state(run)
    assert stopping is not None and stopping.cancelled
    assert (workdir / "partial.bin").exists()

    engine = YtdlpEngine(SPEC_BY_NAME["ytdlp"], EngineConfig(
        name="ytdlp", mode="in_process", enabled_flag=True, url=""), None)
    status = await engine.status({"run": run})
    assert status.state == "failed"

    gate.set()
    assert await gone(run)
    assert not workdir.exists()
    assert (await engine.status({"run": run})).state == "failed"


async def test_a_cancelled_download_that_finishes_anyway_is_dropped(tmp_path):
    gate = threading.Event()
    run = await runner.start("t-deaf", tmp_path, "Deaf", held(gate, then_report=False),
                             parallel=1)
    await asyncio.sleep(0.2)
    workdir = Path((await runner.state(run)).workdir)
    await runner.cancel(run)
    gate.set()
    assert await gone(run)
    assert not workdir.exists()


async def test_flush_failures_leave_the_download_alone(tmp_path, monkeypatch):
    run = await runner.start("t-flaky", tmp_path, "Flaky", writer("f", 30), parallel=1)
    workdir = Path((await runner.state(run)).workdir)

    def failing():
        raise OSError("database blip")
    monkeypatch.setattr(runner, "SessionLocal", failing)
    await asyncio.sleep(0.3)
    assert (workdir / "f.part").exists()
    monkeypatch.setattr(runner, "SessionLocal", SessionLocal)

    done = await until(run, JobState.COMPLETE, JobState.FAILED)
    assert done.state is JobState.COMPLETE and (workdir / "f.flac").exists()


async def test_a_completed_run_survives_a_final_database_blip_and_restart(tmp_path, monkeypatch):
    released = asyncio.Event()
    final_write_failed = asyncio.Event()

    async def download(progress, workdir):
        (workdir / "finished.flac").write_text("done")
        await released.wait()
        return str(workdir)

    async def no_flush(run_id, progress):
        pass

    monkeypatch.setattr(runner, "_flush", no_flush)
    monkeypatch.setattr(runner, "FINALIZE_RETRY_SECONDS", 0.2)
    run = await runner.start("t-final", tmp_path, "Final", download, parallel=1)
    workdir = Path((await runner.state(run)).workdir)
    original = runner.SessionLocal
    failed_once = False

    class FlakySession:
        def __init__(self):
            self.session = original()

        async def __aenter__(self):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                final_write_failed.set()
                raise OSError("database blip")
            return await self.session.__aenter__()

        async def __aexit__(self, *args):
            return await self.session.__aexit__(*args)

    monkeypatch.setattr(runner, "SessionLocal", FlakySession)
    released.set()
    await final_write_failed.wait()

    assert (workdir / runner._FINAL_MARKER).exists()
    assert await runner.recover() == 1
    recovered = await runner.state(run)
    assert recovered.state is JobState.COMPLETE
    assert recovered.landing_path == str(workdir)
    assert (workdir / "finished.flac").exists()

    await runner._live[run].supervisor


async def test_one_slot_queues_the_rest_and_a_queued_run_cancels_at_once(tmp_path):
    first = await runner.start("t-slot", tmp_path, "First", writer("e", 40), parallel=1)
    second = await runner.start("t-slot", tmp_path, "Second", writer("f", 2), parallel=1)
    await asyncio.sleep(0.3)
    running, waiting = await runner.state(first), await runner.state(second)
    assert running.state is JobState.DOWNLOADING
    assert waiting.state is JobState.QUEUED
    assert not any(Path(waiting.workdir).iterdir())

    third = await runner.start("t-slot", tmp_path, "Third", writer("g", 2), parallel=1)
    await runner.cancel(second)
    assert await runner.state(second) is None and not Path(waiting.workdir).exists()

    assert (await until(third, JobState.COMPLETE, timeout=15)).state is JobState.COMPLETE
    assert (await until(first, JobState.COMPLETE)).state is JobState.COMPLETE


async def test_recover_fails_active_runs_and_drops_cancelled_ones(tmp_path):
    folders = {name: tmp_path / name for name in ("active", "cancelled", "complete")}
    for folder in folders.values():
        folder.mkdir()
    async with SessionLocal() as session:
        session.add_all([
            Run(id="a" * 32, engine="t", workdir=str(folders["active"]), state=JobState.DOWNLOADING),
            Run(id="b" * 32, engine="t", workdir=str(folders["cancelled"]),
                state=JobState.COMPLETE, cancelled=True),
            Run(id="c" * 32, engine="t", workdir=str(folders["complete"]), state=JobState.COMPLETE),
        ])
        await session.commit()

    assert await runner.recover() == 2

    active = await runner.state("a" * 32)
    assert active.state is JobState.FAILED and "restarted" in active.error
    assert not folders["active"].exists()
    assert await runner.state("b" * 32) is None and not folders["cancelled"].exists()
    assert (await runner.state("c" * 32)).state is JobState.COMPLETE
    assert folders["complete"].exists()
