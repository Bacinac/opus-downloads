import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from opus.db import SessionLocal
from opus.engines.base import EngineError
from opus.engines.registry import build_engine
from opus.models import Job, JobState, Setting


class Occupied(Exception):
    pass


@asynccontextmanager
async def locked(path: str | None):
    if path is None:
        yield
        return
    engine = create_async_engine(SessionLocal.kw["bind"].url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:path))"),
                                     {"path": f"opus:landing:{path}"})
            yield
    finally:
        await engine.dispose()


async def reserve(job: Job) -> None:
    owner = None
    if job.landing_claim is not None:
        async with SessionLocal() as session:
            owner = (await session.execute(select(Job).where(
                Job.landing_claim == job.landing_claim))).scalar_one_or_none()
        if owner is not None:
            raise Occupied(f"landing directory is already owned by job {owner.id}")
    async with SessionLocal() as session:
        session.add(job)
        await session.commit()


async def adopt() -> None:
    from opus.settings_store import current_runtime

    checkpoint = "landing_claims_version"
    async with SessionLocal() as session:
        if await session.get(Setting, checkpoint) is not None:
            return
        rows = (await session.execute(select(Job).where(Job.job_ref != {})
                                      .order_by(Job.created_at.desc()))).scalars().all()
        runtime = await current_runtime()
        claimed = {}
        for job in rows:
            if job.engine != "slskd":
                continue
            if "batch" in job.job_ref:
                continue
            engine = build_engine(runtime, job.engine)
            async with engine.http(15) as client:
                root = await engine._downloads_root(client)
                username = quote(job.job_ref["username"], safe="")
                response = await client.get(f"/transfers/downloads/{username}")
                response.raise_for_status()
            from opus.engines.slskd import _folder, _normalize

            path = engine._landed(root, _folder(job.job_ref["directory"]))
            wanted = set(job.job_ref["files"])
            transfers = [file["id"] for directory in response.json().get("directories", [])
                         for file in directory.get("files", [])
                         if file.get("filename") in wanted and file.get("id")]
            job.job_ref = {"username": job.job_ref["username"], "batch": None,
                           "transfers": transfers, "landing": path,
                           "files": [Path(_normalize(name)).name for name in wanted]}
            owner = claimed.get(path)
            if owner is not None:
                if (job.state in (JobState.QUEUED, JobState.DOWNLOADING)
                        and owner.state in (JobState.QUEUED, JobState.DOWNLOADING)):
                    raise EngineError(f"multiple active jobs already share {path}")
                if job.state not in (JobState.QUEUED, JobState.DOWNLOADING):
                    continue
                owner.landing_claim = None
            job.landing_claim = path
            claimed[path] = job
        session.add(Setting(key=checkpoint, value="1"))
        await session.commit()


if __name__ == "__main__":
    asyncio.run(adopt())
