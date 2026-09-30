"""GET /engines — the catalog with each engine's live state: its kind, its mode
(bundled | external | in_process), whether it is enabled (credentials present)
and its health channel. This is the onboarding surface: every engine is listed,
the ones without credentials shown-but-disabled.

A bundled engine also carries the container behind it, and the two endpoints
that start and stop it. That is the whole point of bundling: the service is
OPUS's to run, so running it is not a shell command somebody has to know."""

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Depends, HTTPException

from opus import provision
from opus.api.shared import Answered, engine_to_dict
from opus.containers import Docker
from opus.engines.base import Engine, EngineHealth, EngineMode, Linker
from opus.engines.registry import EngineRefused, build_engine, build_engines
from opus.redaction import redacted
from opus.settings_store import RuntimeConfig, current_runtime

log = logging.getLogger(__name__)

router = APIRouter(route_class=Answered)

# where this install appears from without a tunnel. Kept once known: it is the
# thing every VPN-routed engine is checked against, and asking the internet for
# it on every look at a page would be its own kind of leak.
_own_exit: str | None = None


async def own_exit() -> str | None:
    global _own_exit
    if _own_exit is None:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get("https://api.ipify.org")
                resp.raise_for_status()
                _own_exit = resp.text.strip() or None
        except httpx.HTTPError as exc:
            log.warning("this install's own exit address is unknown: %s", exc)
    return _own_exit


# How long one engine's health answer serves everyone who asks. A probe is not
# free — a store logs in, the services cross the network — and a page, the layout
# around it and a consuming module all ask at once. A question already on its
# way is joined rather than asked again.
FRESH_SECONDS = 15


@dataclass
class _Answer:
    settings: int
    task: asyncio.Task | None = None
    at: float | None = None


_answers: dict[str, _Answer] = {}


async def _probe(engine: Engine, answer: _Answer) -> EngineHealth:
    try:
        return await engine.health()
    finally:
        answer.at = time.monotonic()


async def _health(engine: Engine, runtime: RuntimeConfig) -> EngineHealth:
    settings = hash(frozenset(runtime.values.items()))
    held = _answers.get(engine.name)
    if held is None or held.settings != settings or (
        held.at is not None and time.monotonic() - held.at > FRESH_SECONDS
    ):
        held = _answers[engine.name] = _Answer(settings)
        held.task = asyncio.create_task(_probe(engine, held))
    return await asyncio.shield(held.task)


async def _container(docker: Docker, engine: Engine) -> dict | None:
    """Only a bundled engine has a container of OPUS's to report on — or one that
    left bundled mode and whose container could not be taken down. An adopted
    instance is somebody else's process and is deliberately not inspected."""
    bundled = engine.mode is EngineMode.BUNDLED
    if not bundled and engine.name not in provision.failures:
        return None
    try:
        state = await provision.describe(docker, engine.spec, engine.config.use_vpn or not bundled)
    except Exception as exc:
        return {"present": False, "running": False, "error": redacted(str(exc))}
    # a tunnel that is up and carrying nothing looks exactly like one that
    # works, so what is reported is where the engine comes out — and whether
    # that is the address it was supposed to be hiding. Not knowing either is
    # reported as not knowing.
    tunnel = state.get("vpn")
    if tunnel and tunnel.get("running"):
        own = await own_exit()
        exit_ip = tunnel.get("exit_ip")
        tunnel["leaking"] = exit_ip == own if own and exit_ip else None
    return state


@router.get("/engines")
async def list_engines(runtime: RuntimeConfig = Depends(current_runtime)):
    engines = build_engines(runtime)
    docker = Docker()
    healths, containers = await asyncio.gather(
        asyncio.gather(*(_health(e, runtime) for e in engines)),
        asyncio.gather(*(_container(docker, e) for e in engines)),
    )
    return [
        {**engine_to_dict(e, h), "container": c}
        for e, h, c in zip(engines, healths, containers)
    ]


def _bundled(runtime: RuntimeConfig, name: str) -> Engine:
    engine = build_engine(runtime, name)
    if engine.mode is not EngineMode.BUNDLED:
        raise EngineRefused(
            f"{name} is set to {engine.mode}; only a bundled engine is OPUS's to run")
    return engine


@router.post("/engines/{name}/start")
async def start(name: str, runtime: RuntimeConfig = Depends(current_runtime)):
    _bundled(runtime, name)
    _answers.pop(name, None)
    return await provision.start(name)


@router.post("/engines/{name}/stop")
async def stop(name: str, runtime: RuntimeConfig = Depends(current_runtime)):
    engine = build_engine(runtime, name)
    if engine.mode is EngineMode.IN_PROCESS:
        raise EngineRefused(f"{name} runs inside OPUS and has no container to stop")
    _answers.pop(name, None)
    if engine.mode is not EngineMode.BUNDLED:
        return await provision.take_down(Docker(), engine.spec, engine.config.use_vpn)
    return await provision.pause(Docker(), engine.spec, engine.config.use_vpn)


def _linker(runtime: RuntimeConfig, name: str) -> Linker:
    engine = build_engine(runtime, name)
    if not isinstance(engine, Linker):
        raise EngineRefused(f"{name} is not linked through an approval; its account is typed")
    return engine


# An engine whose account arrives by a device-code approval is linked in three
# steps rather than through a field on the settings form.
@router.get("/engines/{name}/link")
async def link_state(name: str, runtime: RuntimeConfig = Depends(current_runtime)):
    return await _linker(runtime, name).link_status()


@router.post("/engines/{name}/link")
async def link_start(name: str, runtime: RuntimeConfig = Depends(current_runtime)):
    """Returns the address to approve at; the answer arrives on a later poll of
    this same endpoint."""
    engine = _linker(runtime, name)
    try:
        return await engine.start_link()
    except Exception as exc:
        raise HTTPException(502, redacted(f"{name} would not start a login: {exc}"))


@router.delete("/engines/{name}/link")
async def link_remove(name: str, runtime: RuntimeConfig = Depends(current_runtime)):
    return await _linker(runtime, name).unlink()
