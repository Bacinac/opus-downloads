"""POST /search — the unified search. Fans out across every enabled engine that
searches (Prowlarr for usenet+torrent, slskd for soulseek, the streaming
catalogs), normalizes the results into one Release shape and returns them.
grab_ref on each release is stateless — it round-trips back to /grab, no
server-side candidate cache. An engine failure is returned beside successful
answers, so one outage does not pretend the others found nothing."""

import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from opus.api.shared import Answered, release_to_dict
from opus.engines.base import Engine, EngineError, Searcher
from opus.engines.registry import EngineRefused, UnknownEngine, build_engine, build_engines
from opus.settings_store import RuntimeConfig, current_runtime

router = APIRouter(route_class=Answered)


class SearchRequest(BaseModel):
    query: str
    type: str = "any"       # movie | tv | music | any
    app: str | None = None  # the calling app, for per-app routing/telemetry
    # ask only these engines instead of every one that can search. A consumer
    # that keeps its own per-source pipeline asks each source in turn, and
    # fanning out across all of them every time would make one such search cost
    # what all of them cost — Soulseek alone answers for tens of seconds.
    engines: list[str] | None = None


def _selected(engines: list[Engine], names: list[str] | None) -> list[Searcher]:
    """The engines to ask. A name that cannot answer is refused rather than
    skipped: a caller that asked for Soulseek and got an empty result would
    conclude the network holds nothing, when in fact it was never asked."""
    if names is None:
        return [e for e in engines if e.enabled and isinstance(e, Searcher)]
    by_name = {e.name: e for e in engines}
    chosen = []
    for name in names:
        engine = by_name.get(name)
        if engine is None:
            raise UnknownEngine(name)
        if not isinstance(engine, Searcher):
            raise EngineRefused(f"{name} cannot search; it only grabs")
        if not engine.enabled:
            raise EngineRefused(f"{name} is not configured")
        chosen.append(engine)
    return chosen


@router.post("/search")
async def search(req: SearchRequest, runtime: RuntimeConfig = Depends(current_runtime)):
    engines = _selected(build_engines(runtime), req.engines)
    answers = await asyncio.gather(
        *(e.search(req.query, req.type) for e in engines), return_exceptions=True
    )
    releases = []
    errors = []
    for engine, answer in zip(engines, answers):
        if not isinstance(answer, BaseException):
            releases.extend(release_to_dict(r) for r in answer)
            continue
        # Cancellation belongs to the request that was cancelled. It must not
        # be reported as an engine outage while the caller has already gone.
        if isinstance(answer, asyncio.CancelledError):
            raise answer
        if isinstance(answer, EngineError):
            detail = str(answer)
        else:
            detail = str(EngineError(f"search failed: {answer}"))
        errors.append({"engine": engine.name, "detail": detail})
    return {"releases": releases, "errors": errors}


class InspectRequest(BaseModel):
    grab_ref: dict


@router.post("/inspect")
async def inspect(req: InspectRequest, runtime: RuntimeConfig = Depends(current_runtime)):
    """What a release declares it holds, before anything is downloaded.

    Belongs beside /search rather than /jobs because it serves the decision, not
    the download: a title is a promise and a manifest is a fact, and the gap
    between them is cheapest to discover before the bytes are fetched. What the
    file list means stays the caller's — OPUS reports names, never verdicts."""
    name = req.grab_ref.get("engine")
    if not name:
        raise EngineRefused("grab_ref does not name an engine")
    engine = build_engine(runtime, name)
    if not engine.enabled:
        raise EngineRefused(f"{name} is not configured")
    contents = await engine.inspect(req.grab_ref)
    return {
        "available": contents.available,
        "detail": contents.detail,
        "files": [{"name": f.name, "size": f.size} for f in contents.files],
    }
