"""The downloads OPUS performs itself, for the engines that have no process of
their own to keep them.

The row is the truth, not the task: a task dies with the process, so status reads
the database and startup fails every run still marked active. Every run writes
into a folder of its own, and only the supervisor that sees the download end
removes it — a cancel that gave up waiting has not made it safe to delete what a
thread is still writing. Progress arrives from worker threads into a plain
`Progress` and reaches the row only through the supervisor's periodic flush."""

import asyncio
import json
import logging
import shutil
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from sqlalchemy import delete, or_, select

from opus.db import SessionLocal
from opus.models import JobState, Run
from opus.redaction import redacted

log = logging.getLogger(__name__)

# how often a running download's progress reaches the row. Callers poll on
# demand, seconds apart, so anything finer buys nothing but writes.
FLUSH_SECONDS = 2

# The final row matters more than a progress flush: it is what allows a caller
# to import a landed file. A temporary database outage is retried, while the
# marker below lets a restart finish the same reconciliation.
FINALIZE_RETRY_SECONDS = 5
_FINAL_MARKER = ".opus-final.json"

# how long cancel waits for a download to notice before it leaves the stopping
# to the supervisor
CANCEL_TIMEOUT = 30


class RunError(Exception):
    """A run cannot do what was asked of it in the state it is in."""


class Cancelled(Exception):
    """Raised inside a download when the caller asked it to stop. The engine's
    progress callback is what turns the request into this exception, which is
    why every in-process engine must report progress even when it has nothing
    interesting to say."""


# A download receives the folder it may write into and a handle to report on,
# and returns the path that landed — a single file when it produced one, the
# folder when it produced several.
Download = Callable[["Progress", Path], Awaitable[str]]


class Progress:
    """What a running download knows about itself, between flushes.

    Deliberately plain and synchronous: engines call `report` from whatever
    thread the download library happens to use, and nothing here touches the
    event loop or the database."""

    def __init__(self) -> None:
        self.state = JobState.QUEUED
        self.fraction = 0.0
        self.speed_bps: int | None = None
        self.eta_seconds: int | None = None
        self.detail = ""
        self._cancelled = False

    def report(self, *, fraction: float | None = None, speed_bps: int | None = None,
               eta_seconds: int | None = None, detail: str | None = None) -> None:
        """Record progress, and stop the download if it has been cancelled.

        The check rides along with the reporting because that is the one moment
        a download inside a blocking library is guaranteed to be in our code."""
        self.raise_if_cancelled()
        self.state = JobState.DOWNLOADING
        if fraction is not None:
            self.fraction = max(0.0, min(1.0, fraction))
        if speed_bps is not None:
            self.speed_bps = int(speed_bps)
        if eta_seconds is not None:
            self.eta_seconds = int(eta_seconds)
        if detail is not None:
            self.detail = detail

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise Cancelled("cancelled")

    def cancel(self) -> None:
        self._cancelled = True
        self.detail = "cancelling"


@dataclass
class _Live:
    progress: Progress
    supervisor: asyncio.Task | None = None
    # None until a slot frees up; until then nothing is writing anywhere
    download: asyncio.Task | None = None


_live: dict[str, _Live] = {}
_slots: dict[str, asyncio.Semaphore] = {}


async def start(engine: str, parent: Path, name: str, download: Download,
                parallel: int) -> str:
    """Begin a download and return the run id the engine hands back as its
    job_ref. The row exists before the task does, so a run is never in flight
    without something on disk saying so.

    At most `parallel` of one engine's downloads fetch at once; the rest wait
    queued. A streaming service watches an account pulling several albums side
    by side the way it watches one being shared."""
    run_id = uuid.uuid4().hex
    workdir = await asyncio.to_thread(_claim, parent, name, run_id)
    try:
        async with SessionLocal() as session:
            session.add(Run(id=run_id, engine=engine, workdir=str(workdir),
                            state=JobState.QUEUED))
            await session.commit()
    except Exception:
        await asyncio.to_thread(_discard, workdir)
        raise

    live = _live[run_id] = _Live(Progress())
    slots = _slots.setdefault(engine, asyncio.Semaphore(parallel))
    live.supervisor = asyncio.create_task(_supervise(run_id, workdir, download, live, slots))
    live.supervisor.add_done_callback(_report_crash)
    return run_id


async def state(run_id: str) -> Run | None:
    """What is known about a run, or None once it has been dropped. The row
    rather than an engine-shaped status: the runner sits under the engine layer
    and does not speak its contract — the adapter that started the run is what
    translates."""
    async with SessionLocal() as session:
        return await session.get(Run, run_id)


async def landing_path(run_id: str) -> str:
    run = await state(run_id)
    if run is None:
        raise RunError(f"run {run_id} does not exist")
    if not run.landing_path:
        raise RunError(f"run {run_id} is {run.state} and has nothing in the landing zone")
    return run.landing_path


async def cancel(run_id: str) -> None:
    """Stop the download, drop what it wrote, and forget the run. A run already
    gone has nothing left to stop.

    A run still waiting for a slot has written nothing and simply ends. One that
    is fetching is asked, and waited for. One that has not stopped within the
    wait keeps its files until it does, and its supervisor removes them then."""
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return
        run.cancelled = True
        await session.commit()
    live = _live.get(run_id)
    if live is not None:
        live.progress.cancel()
        if live.download is None:
            live.supervisor.cancel()
        _, pending = await asyncio.wait({live.supervisor}, timeout=CANCEL_TIMEOUT)
        if pending:
            log.warning("run %s has not stopped within %ds; its files go when it does",
                        run_id, CANCEL_TIMEOUT)
            return
    await _drop(run_id, Path(run.workdir))


async def recover() -> int:
    """Reconcile a completed run that outlived its final database write, fail
    every other in-flight run, and finish dropping cancelled ones.

    Called at startup. Nothing is resumable: the task that was doing the work is
    gone with the process that held it, and a partial file in the landing zone
    is not a download. Failing loud is also what lets a caller do something
    useful — a failed job sends it on to its next candidate."""
    async with SessionLocal() as session:
        result = await session.execute(select(Run).where(or_(
            Run.state.in_((JobState.QUEUED, JobState.DOWNLOADING)), Run.cancelled,
        )))
        orphans = list(result.scalars())
        failed = 0
        reconciled: list[Path] = []
        for run in orphans:
            if run.cancelled:
                await asyncio.to_thread(_discard, Path(run.workdir))
                await session.delete(run)
                continue
            final = await asyncio.to_thread(_completed_marker, Path(run.workdir))
            if final is not None:
                run.state = JobState.COMPLETE
                run.progress = 1.0
                run.speed_bps = run.eta_seconds = None
                run.detail = run.error = ""
                run.landing_path = final
                reconciled.append(Path(run.workdir))
                continue
            await asyncio.to_thread(_discard, Path(run.workdir))
            failed += 1
            run.state = JobState.FAILED
            run.error = run.detail = (
                "the backend restarted while this download was running; in-process "
                "downloads do not survive a restart"
            )
        if orphans:
            await session.commit()
            for workdir in reconciled:
                await asyncio.to_thread(_clear_completed_marker, workdir)
            log.warning("a restart ended %d in-process download(s): %d failed, %d that were "
                        "being cancelled dropped, %d completed from a durable marker",
                        len(orphans), failed, len(orphans) - failed - len(reconciled), len(reconciled))
    return len(orphans)


async def _supervise(run_id: str, workdir: Path, download: Download, live: _Live,
                     slots: asyncio.Semaphore) -> None:
    """Run one download to its end and keep its row honest along the way."""
    progress = live.progress
    try:
        if slots.locked():
            progress.detail = "waiting for another download to finish"
            await _flush(run_id, progress)
        async with slots:
            progress.raise_if_cancelled()
            live.download = asyncio.create_task(download(progress, workdir))
            while not live.download.done():
                await asyncio.wait({live.download}, timeout=FLUSH_SECONDS)
                await _flush(run_id, progress)
        landed = live.download.result()
    except asyncio.CancelledError:
        if not progress.cancelled:
            raise
        await _drop(run_id, workdir)
    except Exception as exc:
        # a library that wraps our Cancelled in an error of its own has still
        # stopped because it was asked to
        if progress.cancelled:
            await _drop(run_id, workdir)
        else:
            error = redacted(str(exc))
            log.warning("run %s failed:\n%s", run_id,
                        redacted("".join(traceback.format_exception(exc))))
            await asyncio.to_thread(_discard, workdir)
            await _finish(run_id, JobState.FAILED, workdir=workdir, error=error)
    else:
        if progress.cancelled:
            await _drop(run_id, workdir)
        else:
            await _finish(run_id, JobState.COMPLETE, workdir=workdir, landed=landed)
    finally:
        _live.pop(run_id, None)


def _report_crash(task: asyncio.Task) -> None:
    """A supervisor failure is exceptional: final state writes retry and a
    completed download leaves a marker recovery can read after a restart."""
    if not task.cancelled() and task.exception() is not None:
        log.error("a run's supervisor failed", exc_info=task.exception())


async def _flush(run_id: str, progress: Progress) -> None:
    """A row that misses one flush is a moment staler than it could be; the
    download under it has done nothing wrong and must not be stopped for it."""
    try:
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if run is None:
                return
            run.state = progress.state
            run.progress = progress.fraction
            run.speed_bps = progress.speed_bps
            run.eta_seconds = progress.eta_seconds
            run.detail = progress.detail
            await session.commit()
    except Exception as exc:
        log.warning("run %s: progress not recorded this time: %s", run_id, exc)


async def _finish(run_id: str, state: JobState, *, workdir: Path, landed: str = "",
                  error: str | None = None) -> None:
    """Persist a terminal result, retaining enough local truth to survive a
    database blip or a restart between the download ending and its commit."""
    if state is JobState.COMPLETE:
        try:
            await asyncio.to_thread(_write_completed_marker, workdir, landed)
        except OSError as exc:
            # The database write is still worthwhile. A filesystem that cannot
            # write this tiny marker cannot promise restart recovery, but the
            # immediate path may still record the result normally.
            log.error("run %s: could not write completion marker: %s", run_id, exc)
    while True:
        try:
            async with SessionLocal() as session:
                run = await session.get(Run, run_id)
                if run is None:
                    break
                run.state = state
                run.error = error
                run.detail = error or ""
                if state is JobState.COMPLETE:
                    run.progress = 1.0
                    run.landing_path = landed
                    run.speed_bps = None
                    run.eta_seconds = None
                await session.commit()
        except Exception as exc:
            log.warning("run %s: final state not recorded; retrying in %ss: %s",
                        run_id, FINALIZE_RETRY_SECONDS, exc)
            await asyncio.sleep(FINALIZE_RETRY_SECONDS)
            continue
        break
    if state is JobState.COMPLETE:
        await asyncio.to_thread(_clear_completed_marker, workdir)


def _write_completed_marker(workdir: Path, landed: str) -> None:
    """Write then replace, so recovery sees either a complete record or none."""
    target = workdir / _FINAL_MARKER
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"state": "complete", "landing_path": landed}), encoding="utf-8")
    temporary.replace(target)


def _completed_marker(workdir: Path) -> str | None:
    try:
        body = json.loads((workdir / _FINAL_MARKER).read_text(encoding="utf-8"))
        landed = body.get("landing_path")
        root = workdir.resolve()
        path = Path(landed).resolve() if isinstance(landed, str) else None
        if body.get("state") != "complete" or path is None or not path.is_relative_to(root) or not path.exists():
            return None
        return str(path)
    except (OSError, ValueError, TypeError):
        return None


def _clear_completed_marker(workdir: Path) -> None:
    try:
        (workdir / _FINAL_MARKER).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove completion marker in %s: %s", workdir, exc)


async def _drop(run_id: str, workdir: Path) -> None:
    """The row goes first, and only whoever removed it removes the folder: a
    second drop arriving late must not empty a folder another run has since
    claimed under the same name."""
    async with SessionLocal() as session:
        removed = (await session.execute(delete(Run).where(Run.id == run_id))).rowcount
        await session.commit()
    if removed:
        await asyncio.to_thread(_discard, workdir)


def _claim(parent: Path, name: str, run_id: str) -> Path:
    """A folder no other run writes into. Two albums can share a title — a
    band's self-titled records — and one folder for both would mix their tracks
    and let either one's failure delete the other's."""
    parent.mkdir(parents=True, exist_ok=True)
    workdir = parent / name
    try:
        workdir.mkdir()
    except FileExistsError:
        workdir = parent / f"{name} [{run_id[:8]}]"
        workdir.mkdir()
    return workdir


def _discard(workdir: Path) -> None:
    try:
        shutil.rmtree(workdir)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.error("could not remove %s: %s", workdir, exc)
