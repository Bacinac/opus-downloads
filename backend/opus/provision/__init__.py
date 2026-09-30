"""Bringing a bundled engine up, and keeping it in step with what is saved.

What an engine must be handed before it first runs is seeded into its own
configuration; what it can be told once it runs is set through its API. How each
engine takes that lives in its own module beside this one — this is the order
it happens in, and the rule that decides when it happens again.

An engine OPUS starts is not asked to hold a login of its own. Nothing is
published to the host, so the only way to it is through OPUS, which has already
asked who you are. The machine credentials OPUS calls them with are generated
per engine and never shown to anyone.

Starting bundled engines needs the Docker socket inside this backend — a web
interface cannot create a container without it. That is the price of bundled
mode, and it is paid knowingly.
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from opus.config import settings
from opus.containers import (
    Docker,
    container_name,
    engine_spec,
    ensure,
    matches,
    solver_name,
    solver_spec,
    state,
    teardown,
    tunnel_exit,
    vpn_name,
    vpn_spec,
)
from opus.engines.base import Engine, EngineMode
from opus.engines.catalog import CONTENT_GROUP, content_folders
from opus.engines.spec import EngineSpec
from opus.engines.registry import build_engine, build_engines
from opus.provision import prowlarr, qbittorrent, sabnzbd, slskd, tunnel
from opus.redaction import redacted
from opus.settings_store import RuntimeConfig, current_runtime, store_credentials

log = logging.getLogger(__name__)

CONTENT = f"{CONTENT_GROUP}_folders"


@dataclass(frozen=True)
class Provisioner:
    # machine credentials the engine must be handed before it is seeded
    credentials: Callable[[RuntimeConfig], dict[str, str]] | None = None
    # its own configuration, written before it runs; returns whether it changed
    seed: Callable[[EngineSpec, RuntimeConfig, str], bool] | None = None
    # what is learned from or set on the running engine
    configure: Callable[[EngineSpec, RuntimeConfig, Docker, dict[str, str]],
                        Awaitable[dict[str, str]]] | None = None
    # the settings whose change a running engine has to be told of
    owned: tuple[str, ...] = ()


PROVISIONERS: dict[str, Provisioner] = {
    "prowlarr": Provisioner(configure=prowlarr.configure, owned=(CONTENT,)),
    "sabnzbd": Provisioner(configure=sabnzbd.configure, owned=(*sabnzbd.OWNED, CONTENT)),
    "qbittorrent": Provisioner(credentials=qbittorrent.credentials, seed=qbittorrent.seed,
                               configure=qbittorrent.configure, owned=(CONTENT,)),
    "slskd": Provisioner(seed=slskd.seed, configure=slskd.configure, owned=slskd.OWNED),
}
_NONE = Provisioner()


def base_env() -> dict[str, str]:
    """The same for every engine — the point of bundling them is that an install
    does not end up holding eight subtly different service configurations."""
    return {
        "PUID": str(settings.engine_uid),
        "PGID": str(settings.engine_gid),
        "TZ": settings.timezone,
    }


def engine_env(spec: EngineSpec) -> dict[str, str]:
    env = base_env()
    if spec.name == "qbittorrent":
        # the image serves its UI on this port inside the container; the block
        # position is a host-side binding and not qBittorrent's business
        env["WEBUI_PORT"] = str(spec.service_port)
    return env


def _definition(spec: EngineSpec, runtime: RuntimeConfig, use_vpn: bool) -> dict:
    share_dir = runtime.get(f"{spec.name}_share_dir") if spec.share_mount else ""
    return engine_spec(spec, engine_env(spec), use_vpn, share_dir)


async def bring_up(docker: Docker, spec: EngineSpec, runtime: RuntimeConfig,
                   use_vpn: bool) -> dict:
    """Start the engine, and its tunnel first when it has one — an engine that
    joins a namespace cannot start before the container holding it exists. An
    engine that was already running when its configuration changed is
    restarted, because it read that file when it started."""
    reseeded = await _seed(docker, spec, runtime)
    tunnel_is_new = False
    if use_vpn:
        tunnel.seed(spec)
        tunnel_is_new = await ensure(docker, vpn_name(spec.name),
                                     vpn_spec(spec, tunnel.env(runtime)))
    if tunnel_is_new:
        # the engine holds no network of its own — it is inside the namespace of
        # the tunnel that just went away. It keeps pointing at the dead one and
        # refuses connections from everywhere, including itself, until it is
        # built again against the new one.
        log.info("%s: its tunnel was replaced, so it is too", spec.name)
        await teardown(docker, container_name(spec.name))
        await teardown(docker, solver_name(spec.name))
    if spec.solver:
        await ensure(docker, solver_name(spec.name), solver_spec(spec, use_vpn))
    created = await ensure(docker, container_name(spec.name), _definition(spec, runtime, use_vpn))
    if not use_vpn:
        await teardown(docker, vpn_name(spec.name))
    if reseeded and not created:
        log.info("%s: its configuration changed, restarting it", spec.name)
        await docker.restart(container_name(spec.name))
    return await describe(docker, spec, use_vpn)


async def take_down(docker: Docker, spec: EngineSpec, use_vpn: bool) -> dict:
    """Stop the engine before its tunnel: the other order strands it without a
    network for as long as it takes to notice."""
    async with _lock(spec.name):
        await teardown(docker, container_name(spec.name))
        await teardown(docker, solver_name(spec.name))
        await teardown(docker, vpn_name(spec.name))
        failures.pop(spec.name, None)
        return await describe(docker, spec, use_vpn)


async def pause(docker: Docker, spec: EngineSpec, use_vpn: bool) -> dict:
    """Stop the engine and its tunnel and keep both containers. A bundled engine
    with no container is then always a loss rather than a choice, which is what
    lets a restart of OPUS bring a lost one back instead of leaving the search
    answering "name not known" for as long as nobody opens its card."""
    async with _lock(spec.name):
        await docker.stop(container_name(spec.name))
        if spec.solver:
            await docker.stop(solver_name(spec.name))
        if use_vpn:
            await docker.stop(vpn_name(spec.name))
        failures.pop(spec.name, None)
        return await describe(docker, spec, use_vpn)


async def retire(previous: dict[str, str]) -> None:
    """An engine a save moved off bundled mode is no longer OPUS's to run, so its
    container and its tunnel go with the save. What went wrong stays on its card."""
    docker = Docker()
    for engine in build_engines(await current_runtime()):
        if (previous.get(f"{engine.name}_mode") != EngineMode.BUNDLED
                or engine.mode is EngineMode.BUNDLED):
            continue
        try:
            await take_down(docker, engine.spec, engine.config.use_vpn)
        except Exception as exc:
            failures[engine.name] = redacted(str(exc))
            log.warning("%s: its bundled container could not be taken down: %s",
                        engine.name, failures[engine.name])


async def describe(docker: Docker, spec: EngineSpec, use_vpn: bool) -> dict:
    """What the engine's card shows about the container behind it, and why the
    last attempt to bring it up did not work, if it did not."""
    engine = await state(docker, container_name(spec.name))
    if failure := failures.get(spec.name):
        engine["error"] = failure
    if not use_vpn:
        return engine
    held = await state(docker, vpn_name(spec.name))
    if held.get("running"):
        held["exit_ip"] = await tunnel_exit(vpn_name(spec.name))
    return {**engine, "vpn": held}


async def _seed(docker: Docker, spec: EngineSpec, runtime: RuntimeConfig) -> bool:
    seeder = PROVISIONERS.get(spec.name, _NONE).seed
    if seeder is None:
        return False
    return seeder(spec, runtime, await docker.network_subnet(settings.engine_network))


# A first boot is slow: the image unpacks its config, migrates a database and
# only then listens. Pressing start should mean the engine works, so the call
# waits for it rather than handing back a card that reads red for a while.
READY_WAIT_SECONDS = 60


async def await_ready(engine: Engine) -> dict:
    """Poll the engine's own health until it answers or the wait runs out. A
    timeout is reported, not raised: the container is up either way, and the
    card showing why it is not answering beats an error that hides it."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + READY_WAIT_SECONDS
    health = await engine.health()
    while not health.ok and loop.time() < deadline:
        await asyncio.sleep(2)
        health = await engine.health()
    return {"ok": health.ok, "detail": health.detail}


async def ensure_content(engine: Engine, runtime: RuntimeConfig) -> list[str]:
    """Create the install's content folders up front, under the landing root and
    in the engine itself, so a given kind of download lands in the same place
    whichever engine fetched it."""
    folders = content_folders(runtime)
    for folder in folders:
        Path(settings.landing_root, folder).mkdir(parents=True, exist_ok=True)
    try:
        await engine.reconcile_namespaces(folders)
    except Exception as exc:
        # an engine that cannot hold categories, or one still coming up, must
        # not fail the start — the folders exist in the landing zone either way
        log.warning("%s: could not set its folders: %s", engine.name, redacted(str(exc)))
    return folders


# Why the last attempt to bring each engine up failed, until one succeeds or the
# engine is stopped. A start from the page answers with its own error; one run
# after a settings save has nobody waiting for it, and its engine's card is where
# that is said.
failures: dict[str, str] = {}

# One bring-up per engine at a time, whoever asked for it: the page's start
# button and a save applying itself must not both recreate the same container.
_locks: dict[str, asyncio.Lock] = {}


def _lock(name: str) -> asyncio.Lock:
    return _locks.setdefault(name, asyncio.Lock())


async def start(name: str, previous: dict[str, str] | None = None) -> dict:
    """Start means "make it work": the container comes up and OPUS then learns
    or sets whatever it needs to call the engine, so a started engine is a
    usable one rather than one waiting for a second manual step. `previous`
    holds what the settings a save just changed used to be."""
    async with _lock(name):
        try:
            result = await _start(name, previous or {})
        except Exception as exc:
            failures[name] = redacted(str(exc))
            raise
        failures.pop(name, None)
        return result


async def _start(name: str, previous: dict[str, str]) -> dict:
    docker = Docker()
    runtime = await current_runtime()
    engine = build_engine(runtime, name)
    provisioner = PROVISIONERS.get(name, _NONE)
    learned = provisioner.credentials(runtime) if provisioner.credentials else {}
    if learned:
        await store_credentials(learned)
        runtime = await current_runtime()
    state_now = await bring_up(docker, engine.spec, runtime, engine.config.use_vpn)
    if provisioner.configure:
        found = await provisioner.configure(engine.spec, runtime, docker, previous)
        if found:
            await store_credentials(found)
            learned |= found
    # rebuilt against what was just learned: the engine that was started could
    # not have authenticated with a key it did not yet have
    runtime = await current_runtime()
    engine = build_engine(runtime, name)
    return {
        **state_now,
        "configured": sorted(learned),
        "health": await await_ready(engine),
        "folders": await ensure_content(engine, runtime),
        "linked": await prowlarr.link_clients(docker, runtime),
    }


async def _settled(docker: Docker, engine: Engine, runtime: RuntimeConfig) -> bool:
    use_vpn = engine.config.use_vpn
    if not await matches(docker, container_name(engine.name),
                         _definition(engine.spec, runtime, use_vpn)):
        return False
    if engine.spec.solver and not await matches(docker, solver_name(engine.name),
                                                solver_spec(engine.spec, use_vpn)):
        return False
    return not use_vpn or await matches(docker, vpn_name(engine.name),
                                        vpn_spec(engine.spec, tunnel.env(runtime)))


async def _missing(docker: Docker, spec: EngineSpec) -> bool:
    names = [container_name(spec.name)] + ([solver_name(spec.name)] if spec.solver else [])
    for name in names:
        if await docker.inspect(name) is None:
            return True
    return False


_applying = asyncio.Lock()
_applies: set[asyncio.Task] = set()


def apply_saved(previous: dict[str, str]) -> None:
    """Carry what was just saved into the engines OPUS runs, without holding the
    form that saved it: bringing an engine up can take a minute. Saves queue
    behind each other, so the later one sees what the earlier one left."""
    task = asyncio.create_task(_apply(previous))
    _applies.add(task)
    task.add_done_callback(_applied)


def _applied(task: asyncio.Task) -> None:
    _applies.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.error("applying the saved settings failed", exc_info=task.exception())


async def _apply(previous: dict[str, str]) -> None:
    """A container is built from settings and then stops reading them, so a saved
    change that nobody applies is not a change. Every running bundled engine
    whose definition no longer matches, or whose own settings the save changed,
    is brought up again here.

    Only those: an engine the change did not touch keeps running, so saving one
    engine's setting never disturbs a download somewhere else."""
    async with _applying:
        docker = Docker()
        runtime = await current_runtime()
        # Prowlarr's configuration tests the grab clients it is wired to, so it
        # comes up after any of them this pass is recreating
        for engine in sorted(build_engines(runtime), key=lambda e: e.name == "prowlarr"):
            if engine.mode is not EngineMode.BUNDLED:
                continue
            try:
                if await _missing(docker, engine.spec):
                    log.warning("%s runs bundled and its container is gone; bringing it up",
                                engine.name)
                    await start(engine.name, previous)
                    continue
                told = previous.keys() & set(PROVISIONERS.get(engine.name, _NONE).owned)
                if not told and await _settled(docker, engine, runtime):
                    continue
                log.info("%s: its settings changed, bringing it up again", engine.name)
                await start(engine.name, previous)
            except Exception as exc:
                failures[engine.name] = redacted(str(exc))
                log.warning("%s: could not be brought up after the change: %s",
                            engine.name, failures[engine.name])
