"""What the engine-facing routers share: one JSON shape per engine dataclass,
and one answer per kind of failure."""

import httpx
from fastapi import HTTPException
from fastapi.routing import APIRoute

from opus import jobs
from opus.config import settings
from opus.containers import ContainerError
from opus.engines.base import Engine, EngineError, EngineHealth, EngineMode, Release
from opus.engines.catalog import ui_base_path
from opus.engines.registry import EngineRefused, UnknownEngine
from opus.redaction import redacted

# Most specific first. An engine that fails, or answers with nonsense, is a bad
# gateway; a job asked for something its state does not allow is a conflict.
_ANSWERS: tuple[tuple[type[Exception], int], ...] = (
    (jobs.JobNotFound, 404),
    (EngineRefused, 400),
    (UnknownEngine, 400),
    (jobs.JobError, 409),
    (EngineError, 502),
    (ContainerError, 502),
    (httpx.HTTPError, 502),
)


class Answered(APIRoute):
    """Turns the failures the service layer raises into the status they mean,
    so a route says what it does and not, again, what can go wrong with it."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def answered(request):
            try:
                return await handler(request)
            except tuple(kind for kind, _ in _ANSWERS) as exc:
                status = next(code for kind, code in _ANSWERS if isinstance(exc, kind))
                detail = f"unknown engine: {exc}" if isinstance(exc, UnknownEngine) else str(exc)
                raise HTTPException(status, redacted(detail)) from exc

        return answered


def release_to_dict(r: Release) -> dict:
    return {
        "title": r.title,
        "protocol": r.protocol,
        "source": r.source,
        "grab_ref": r.grab_ref,
        "size": r.size,
        "seeders": r.seeders,
        "speed_bps": r.speed_bps,
        "queue_length": r.queue_length,
        "age_days": r.age_days,
        "bitrate": r.bitrate,
        "subs_hint": r.subs_hint,
        "indexer": r.indexer,
        "guid": r.guid,
        "tracks": r.tracks,
        "bit_depth": r.bit_depth,
        "sample_rate": r.sample_rate,
        "categories": r.categories,
    }


def engine_to_dict(e: Engine, health: EngineHealth) -> dict:
    return {
        "name": e.name,
        "kind": e.kind,
        "family": e.family,
        "mode": e.mode,
        # where the engine answers, and where a person opens it — the same
        # address until an adopted instance is published somewhere of its own.
        # A direct-source engine has neither, and says so with empty strings.
        "url": e.config.url,
        "public_url": e.config.public_url,
        "enabled": e.enabled,
        "can_search": e.can_search,
        "can_grab": e.can_grab,
        "can_link": e.can_link,
        "use_vpn": e.config.use_vpn,
        "ui_url": (f"{settings.engines_url}{ui_base_path(e.name)}/"
                   if e.mode is EngineMode.BUNDLED else ""),
        "health": {"ok": health.ok, "detail": health.detail},
    }
