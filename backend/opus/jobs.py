"""The job lifecycle: grab → track → cancel, and the one place that knows a
job's engine.

OPUS owns the job id and the row; the engine owns its own handle (job_ref) and
stays stateless about OPUS. Polling is on demand — a consumer asks for a job's
status and the owning engine is asked right then, so there is no background
poller whose staleness could disagree with the engine.

A job ends at its landing path. What the file is called and where it goes from
there is the caller's business, and so is the move.
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from opus.db import SessionLocal
from opus import claims
from opus.engines.base import Engine, EngineMode, Grabber
from opus.engines.registry import EngineRefused, UnknownEngine, build_engine
from opus.models import Job, JobEvent, JobState
from opus.redaction import redacted
from opus.settings_store import RuntimeConfig

log = logging.getLogger(__name__)

# How long an engine may hold no record of a job before the job is failed.
# Right after a grab that is lag; after this long the engine has lost it —
# deleted in its own interface, a purged history — and it is never coming back.
FORGOTTEN_AFTER = timedelta(minutes=15)
MAX_EVENT_DETAIL = 500


class JobError(Exception):
    """A job cannot do what was asked of it in the state it is in."""


class JobNotFound(JobError):
    pass


def _reachable(engine: Engine) -> bool:
    """Whether OPUS can act on the engine's jobs. An in-process engine's jobs
    are OPUS's own runs, which do not stop being answerable because the engine
    was switched off for new grabs."""
    return engine.enabled or engine.mode is EngineMode.IN_PROCESS


def _request_fingerprint(grab_ref: dict, namespace: str) -> str:
    """A stable digest lets us reject one key being reused for another grab."""
    body = json.dumps({"grab_ref": grab_ref, "namespace": namespace},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


async def _already_started(app: str, key: str, fingerprint: str) -> Job:
    async with SessionLocal() as session:
        result = await session.execute(select(Job).where(
            Job.app == app, Job.idempotency_key == key))
        job = result.scalar_one()
    if job.idempotency_fingerprint != fingerprint:
        raise JobError("idempotency key was already used for another grab")
    return job


async def _record(job_id: str, kind: str, detail: str = "") -> None:
    """Append a deliberately bounded and redacted job event.

    The engine's opaque job reference never belongs in this history. Errors are
    useful, but may carry an endpoint or credential, so use the same redactor
    as the public API before storing them.
    """
    safe = redacted(detail)[:MAX_EVENT_DETAIL] if detail else ""
    async with SessionLocal() as session:
        session.add(JobEvent(job_id=job_id, kind=kind, detail=safe))
        await session.commit()


async def start(runtime: RuntimeConfig, grab_ref: dict, namespace: str,
                app: str | None, idempotency_key: str | None = None) -> Job:
    name = grab_ref.get("engine")
    if not name:
        raise EngineRefused("grab_ref does not name an engine")
    engine = build_engine(runtime, name)
    if not isinstance(engine, Grabber):
        raise EngineRefused(f"{name} cannot grab; it only feeds releases to other engines")
    if not engine.enabled:
        raise EngineRefused(f"{name}: engine is not configured")
    if idempotency_key and not app:
        raise JobError("an idempotency key needs an app")

    if idempotency_key:
        async with SessionLocal() as session:
            prior = (await session.execute(select(Job).where(
                Job.app == app, Job.idempotency_key == idempotency_key))).scalar_one_or_none()
        if prior is not None:
            return await _already_started(app, idempotency_key,
                                          _request_fingerprint(grab_ref, namespace))
    job_id = uuid.uuid4().hex
    prepared = await engine.reference(grab_ref, namespace, job_id)
    claim = prepared["landing"] if engine.claims_directory else None
    async with claims.locked(claim):
        async with claims.locked(f"opus:enqueue:{engine.name}" if engine.claims_directory else None):
            return await _start_reserved(engine, grab_ref, namespace, app, idempotency_key,
                                         job_id, prepared, claim)


async def _start_reserved(engine, grab_ref: dict, namespace: str, app: str | None,
                          idempotency_key: str | None, job_id: str, prepared: dict,
                          claim: str | None) -> Job:

    # the row before the download: a download nothing records is one nobody can
    # find again, let alone stop
    job = Job(
        id=job_id,
        engine=engine.name,
        app=app,
        namespace=namespace,
        title=grab_ref.get("title", ""),
        grab_ref=grab_ref,
        idempotency_key=idempotency_key,
        idempotency_fingerprint=(_request_fingerprint(grab_ref, namespace)
                                 if idempotency_key else None),
        job_ref=prepared,
        landing_claim=claim,
        state=JobState.QUEUED,
        seen_at=datetime.now(timezone.utc),
    )
    try:
        await claims.reserve(job)
    except claims.Occupied as exc:
        raise JobError(str(exc)) from exc
    except IntegrityError:
        if idempotency_key:
            return await _already_started(app, idempotency_key, job.idempotency_fingerprint)
        raise
    await _record(job.id, "accepted")
    try:
        ref = {**grab_ref, "_prepared": prepared} if prepared else grab_ref
        job_ref = await engine.grab(ref, namespace)
    except Exception as exc:
        error = redacted(str(exc))
        await _written(job.id, state=JobState.FAILED, error=error)
        await _record(job.id, "failed", error)
        raise
    if not await _written(job.id, job_ref=job_ref):
        await engine.cancel(job_ref)
        raise JobNotFound(f"job {job.id} was dropped while its download was being started")
    job.job_ref = job_ref
    await _record(job.id, "started")
    return job


async def _written(job_id: str, **values) -> bool:
    async with SessionLocal() as session:
        written = await session.execute(update(Job).where(Job.id == job_id).values(**values))
        await session.commit()
    return bool(written.rowcount)


async def get(job_id: str, app: str | None = None) -> Job:
    async with SessionLocal() as session:
        if app is None:
            job = await session.get(Job, job_id)
        else:
            result = await session.execute(select(Job).where(Job.id == job_id, Job.app == app))
            job = result.scalar_one_or_none()
    if job is None:
        raise JobNotFound(f"job {job_id} does not exist")
    return job


async def listing() -> list[Job]:
    async with SessionLocal() as session:
        result = await session.execute(select(Job).order_by(Job.created_at.desc()))
        return list(result.scalars())


async def history(job_id: str, app: str | None = None) -> list[JobEvent]:
    """Return only the recent OPUS decisions for a job the caller may see."""
    await get(job_id, app)
    async with SessionLocal() as session:
        result = await session.execute(select(JobEvent).where(JobEvent.job_id == job_id).order_by(
            JobEvent.created_at.desc(), JobEvent.id.desc()).limit(20))
        return list(result.scalars())


async def refresh(runtime: RuntimeConfig, job_id: str, app: str | None = None) -> tuple[Job, dict]:
    """Ask the owning engine where the download stands and fold the answer into
    the row.

    A job that has landed is final. Its files are the caller's to take, and an
    engine that has since archived, purged or re-checked it has nothing to say
    that could make them less there — SABnzbd's archive being cleared would
    otherwise send a finished job back to queued.

    The engine is asked with no database connection held: an engine can take
    tens of seconds to answer, and every caller polling at once would otherwise
    hold the pool empty for all of it."""
    job = await get(job_id, app)
    if job.state is JobState.COMPLETE and job.landing_path:
        return job, {}
    now = datetime.now(timezone.utc)
    if not job.job_ref:
        return await _starting(job, now)

    engine = build_engine(runtime, job.engine)
    if not _reachable(engine):
        raise JobError(f"{job.engine}: engine is not configured")
    status = await engine.status(job.job_ref)

    if status.seen:
        job.seen_at = now
        job.state = JobState(status.state)
        job.progress = status.progress
        job.error = redacted(status.detail) if status.state == "failed" else None
    elif job.state is not JobState.FAILED and now - job.seen_at > FORGOTTEN_AFTER:
        job.state = JobState.FAILED
        job.error = redacted(
            f"{job.engine} has held no record of this job for "
            f"{int((now - job.seen_at).total_seconds() // 60)} minutes: {status.detail}")
    if job.state is JobState.COMPLETE and not job.landing_path:
        job.landing_path = engine.landed(await engine.completed_path(job.job_ref))

    async with SessionLocal() as session:
        current = (await session.execute(select(Job).where(Job.id == job_id)
                                        .with_for_update())).scalar_one_or_none()
        if current is None:
            raise JobNotFound(f"job {job_id} was dropped while its engine was being asked")
        if current.state is JobState.COMPLETE and current.landing_path:
            return current, {}
        if current.seen_at > now:
            return current, {}
        previous_state = current.state
        if current.state is JobState.DOWNLOADING and job.state is JobState.QUEUED:
            job.state = current.state
        current.seen_at = max(current.seen_at, job.seen_at)
        current.state = job.state
        current.progress = max(current.progress, job.progress)
        current.error = job.error
        current.landing_path = job.landing_path or current.landing_path
        await session.commit()
        job = current
    if job.state != previous_state:
        await _record(job_id, "state")
    return job, {
        "speed_bps": status.speed_bps,
        "eta_seconds": status.eta_seconds,
        "detail": job.error if job.state is JobState.FAILED else redacted(status.detail),
    }


async def _starting(job: Job, now: datetime) -> tuple[Job, dict]:
    """A job whose download has not reported back from being started. A start
    this old is one the process that made it did not live to finish."""
    if job.state is not JobState.FAILED and now - job.created_at > FORGOTTEN_AFTER:
        job.state = JobState.FAILED
        job.error = "its download was never started: the grab did not come back"
        async with SessionLocal() as session:
            written = await session.execute(update(Job).where(
                Job.id == job.id, Job.job_ref == {}, Job.state == JobState.QUEUED
            ).values(state=job.state, error=job.error))
            await session.commit()
        if written.rowcount:
            await _record(job.id, "failed", job.error)
        else:
            job = await get(job.id)
    return job, {"detail": "" if job.state is JobState.FAILED else "starting"}


async def cancel(runtime: RuntimeConfig, job_id: str, app: str | None = None) -> None:
    """Stop the download, discard what it fetched and drop the job.

    An engine OPUS can no longer reach — switched off, or gone from the catalog —
    cannot be asked to stop anything. Its job is dropped all the same, because a
    row nobody can act on only stands in the way, and what the engine may still
    hold is said in the log."""
    job = await get(job_id, app)
    async with claims.locked(job.landing_claim):
        await _cancel_owned(runtime, job_id, app)


async def _cancel_owned(runtime: RuntimeConfig, job_id: str, app: str | None) -> None:
    job = await get(job_id, app)
    try:
        engine = build_engine(runtime, job.engine)
    except UnknownEngine:
        engine = None
    # a job with no job_ref is still being started, or never was: the start sees
    # the row gone and stops its own download
    if engine is not None and isinstance(engine, Grabber) and engine.claims_directory and not job.landing_claim:
        log.info("job %s no longer owns a landing directory; dropping bookkeeping", job.id)
    elif job.job_ref and engine is not None and _reachable(engine):
        await engine.cancel(job.job_ref)
    elif job.job_ref:
        log.warning("job %s dropped without stopping it: %s is not configured, so "
                    "whatever it holds for %s is left there", job.id, job.engine, job.job_ref)
    async with SessionLocal() as session:
        await session.execute(delete(Job).where(Job.id == job_id))
        await session.commit()


def to_dict(job: Job, live: dict | None = None) -> dict:
    return {
        "id": job.id,
        "engine": job.engine,
        "app": job.app,
        "namespace": job.namespace,
        "title": job.title,
        "state": job.state,
        "progress": job.progress,
        "speed_bps": (live or {}).get("speed_bps"),
        "eta_seconds": (live or {}).get("eta_seconds"),
        "detail": (live or {}).get("detail", ""),
        "landing_path": job.landing_path,
        "error": job.error,
        # A row may remain queued while an engine has stopped answering. This
        # timestamp is deliberately the last verified engine observation, not
        # the database row's write time.
        "seen_at": job.seen_at.isoformat() if job.seen_at else None,
        "created_at": job.created_at.isoformat(),
    }


def event_to_dict(event: JobEvent) -> dict:
    return {
        "kind": event.kind,
        "detail": event.detail,
        "created_at": event.created_at.isoformat(),
    }
