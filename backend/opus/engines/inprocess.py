"""What every direct-source engine shares: the download happens here.

An indexer-mediated engine hands work to a service and then asks after it —
three different HTTP dialects for the same three questions. A direct-source
engine has no service to ask, so all three answers come from the runner, and
they are the same for every one of them. Only two things differ per engine:
what a search of that service's catalog returns, and how the bytes are fetched.

So an adapter here implements `_download` (and `search`, when it has a catalog),
and inherits the rest. `job_ref` is `{"run": <run id>}` — the runner's row is the
engine's job state, which is why an in-process download reports the same way
after a restart as before one (as failed, loudly)."""

import abc
import re
from pathlib import Path

from opus import runner
from opus.engines.base import EngineError, Grabber, JobStatus

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    """A folder or file name that survives every filesystem the landing zone
    might sit on. Service metadata is arbitrary text — a track called
    `AC/DC: Who Made Who?` is not a path."""
    cleaned = _UNSAFE.sub("-", name).strip(" .")
    return cleaned[:180] or "untitled"


class InProcessEngine(Grabber):
    """A direct-source engine: it downloads inside this backend rather than
    telling a sidecar to."""

    @abc.abstractmethod
    async def _download(self, grab_ref: dict) -> tuple[str, runner.Download]:
        """Return the folder name this grab writes into, and the coroutine that
        does the fetching. The runner owns the folder from there."""

    def landing_root(self, namespace: str) -> Path:
        """Where this engine's downloads land. Unlike a sidecar's there is
        nothing to translate: OPUS writes the files itself, so the path it
        chooses is already the path it sees."""
        return Path(self.landing_dir()) / safe_name(namespace or "default")

    async def grab(self, grab_ref: dict, namespace: str) -> dict:
        folder, download = await self._download(grab_ref)
        run = await runner.start(self.name, self.landing_root(namespace), safe_name(folder),
                                 download, self.spec.parallel)
        return {"run": run}

    async def status(self, job_ref: dict) -> JobStatus:
        run = await runner.state(job_ref["run"])
        if run is None:
            return JobStatus(state="failed", detail="the download no longer exists")
        if run.cancelled:
            return JobStatus(state="failed",
                             detail="cancelled; its files go once the download stops")
        return JobStatus(
            state=run.state.value,
            progress=run.progress,
            speed_bps=run.speed_bps,
            eta_seconds=run.eta_seconds,
            detail=run.detail,
        )

    async def cancel(self, job_ref: dict) -> None:
        await runner.cancel(job_ref["run"])

    async def completed_path(self, job_ref: dict) -> str:
        try:
            return await runner.landing_path(job_ref["run"])
        except runner.RunError as exc:
            raise EngineError(str(exc)) from exc
