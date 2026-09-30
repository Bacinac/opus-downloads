import enum
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint, false, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(AsyncAttrs, DeclarativeBase):
    pass


class JobState(enum.StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETE = "complete"
    FAILED = "failed"


class Setting(Base):
    """Flat key/value runtime configuration edited through the Settings page.
    Per-engine keys are `<engine>_<field>` — see opus.settings_store, which
    derives the whole spec from the engine catalog."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Job(Base):
    """A grab in flight. The id round-trips to the caller (`/jobs/{id}`); the
    grab_ref is the caller's stateless handle on the release and the job_ref is
    what the engine handed back to address the download in its own terms (an
    nzo_id, an infohash, a soulseek user plus file list). landing_path is filled
    when the file lands in the zone."""

    __tablename__ = "jobs"
    __table_args__ = (
        # A caller owns a key within its own app. NULL keeps the existing
        # one-shot browser API behaviour: PostgreSQL permits more than one
        # NULL in a unique constraint.
        UniqueConstraint("app", "idempotency_key", name="uq_jobs_app_idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    engine: Mapped[str] = mapped_column(String(32))
    # the consuming app (library | …) and its isolation namespace, mapped to
    # per-app categories in the underlying engines
    app: Mapped[str | None] = mapped_column(String(32))
    namespace: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(Text, default="")
    grab_ref: Mapped[dict] = mapped_column(JSONB)
    # A repeated transport request must return the first job rather than make
    # its engine fetch the release twice. The fingerprint detects reuse of a
    # key for another release without storing a second copy of its body.
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    idempotency_fingerprint: Mapped[str | None] = mapped_column(String(64))
    job_ref: Mapped[dict] = mapped_column(JSONB, default=dict)
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, values_callable=lambda e: [m.value for m in e]),
        default=JobState.QUEUED,
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    landing_path: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    # the last time the engine had any record of the job
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class JobEvent(Base):
    """A short, safe audit trail of the decisions OPUS made for one job.

    This deliberately stores OPUS events rather than raw engine replies. Engine
    references and URLs can be sensitive, while a person still needs to know
    whether a request was accepted, handed to an engine or failed later.
    """

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    """One download OPUS is running inside its own process.

    A sidecar engine keeps this state in its own process and OPUS only asks for
    it. A direct-source engine has no such process, so the state has nowhere to
    live but here — and durably, because a task lives in one process's memory:
    a restart ends every download it was carrying, and a row is what turns that
    into a loud failure instead of a job that quietly stops reporting.

    `workdir` is the scratch folder the runner owns for this download; it is
    removed when the run fails or is cancelled, so a half-finished file never
    survives to look like a finished one. `cancelled` is set before the
    download is asked to stop, and a run carrying it is dropped by whatever
    sees it next — its supervisor, or the next start of this process."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    engine: Mapped[str] = mapped_column(String(32))
    workdir: Mapped[str] = mapped_column(Text)
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, values_callable=lambda e: [m.value for m in e]),
        default=JobState.QUEUED,
        index=True,
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    speed_bps: Mapped[int | None] = mapped_column(BigInteger)
    eta_seconds: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str] = mapped_column(Text, default="")
    landing_path: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
