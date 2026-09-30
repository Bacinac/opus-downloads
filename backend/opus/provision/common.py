"""What every engine's provisioning leans on: where its own configuration is, how
long a first boot is waited for, and the name it is reached by."""

import asyncio
import os
from pathlib import Path

import httpx

from opus.config import settings
from opus.containers import ContainerError, container_name, vpn_name
from opus.engines.spec import EngineSpec
from opus.settings_store import RuntimeConfig

# How long to wait for a freshly created engine to write its configuration. It
# is a first boot, not a request, so this is patience rather than a timeout.
CONFIG_WAIT_SECONDS = 60


def config_path(spec: EngineSpec, *parts: str) -> Path:
    """The engine's own config, as OPUS sees it: the same directory it has
    mounted at /config, which is what makes learning its generated key possible
    at all."""
    return Path(settings.engines_dir, spec.name, *parts)


def write_whole(path: Path, text: str) -> None:
    """An engine that watches its file must never read half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.opus-partial")
    partial.write_text(text)
    os.replace(partial, path)


async def await_file(path: Path) -> str:
    deadline = asyncio.get_running_loop().time() + CONFIG_WAIT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if path.exists() and path.stat().st_size:
            return path.read_text()
        await asyncio.sleep(1)
    raise ContainerError(
        f"{path} never appeared — the engine did not finish its first start"
    )


async def await_http(url: str, headers: dict[str, str] | None = None) -> None:
    """Wait until the engine answers. Asked without credentials any answer will
    do — an unauthorised one still proves the service is up. Asked with them,
    only a success will: an engine that answers a path with a 404 is up, but not
    where it was told to be."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CONFIG_WAIT_SECONDS
    while loop.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=5, headers=headers or {}) as client:
                resp = await client.get(url)
            if headers is None or resp.is_success:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(2)
    raise ContainerError(f"{url} never started answering")


async def await_accepted(client: httpx.AsyncClient, path: str, body: dict) -> httpx.Response:
    """Post until the engine accepts, for a check that fails only while what it
    checks is still starting. The last refusal is handed back when patience
    runs out, so what was wrong can still be said."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CONFIG_WAIT_SECONDS
    while (resp := await client.post(path, json=body)).status_code >= 400 and loop.time() < deadline:
        await asyncio.sleep(2)
    return resp


def uses_vpn(spec: EngineSpec, runtime: RuntimeConfig) -> bool:
    return runtime.bool(f"{spec.name}_use_vpn")


def reached_as(spec: EngineSpec, use_vpn: bool) -> str:
    """The name OPUS and its Prowlarr call a bundled engine by. Inside a tunnel
    the engine holds no network of its own, and the tunnel answers for it."""
    return vpn_name(spec.name) if use_vpn else container_name(spec.name)
