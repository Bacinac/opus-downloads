"""Running the engines OPUS bundles.

Bundled mode means the engines are OPUS's to start, stop and replace, and that
everything about them is set from its interface — so their definition lives here
rather than in a compose file somebody has to edit and apply by hand. The price
is the Docker socket: a web interface cannot bring up a container without it,
and that is stated plainly rather than hidden.

Only containers OPUS created are ever touched. Every one it starts is stamped
with an ownership label, and anything without that label is left alone even when
the name matches — an install that adopts an existing downloader host must not
have its running services stopped by a stray provision.
"""

import hashlib
import json
import logging
from typing import Any

import httpx

from opus import paths
from opus.config import settings
from opus.engines.catalog import ui_base_path
from opus.engines.spec import EngineSpec
from opus.redaction import redacted

log = logging.getLogger(__name__)

API_VERSION = "v1.44"
OWNER_LABEL = "biz.boskovic.opus"
SPEC_LABEL = f"{OWNER_LABEL}.spec"
SOLVER_IMAGE = "ghcr.io/flaresolverr/flaresolverr:latest"
SOLVER_PORT = 8191
BASE_URL = "http://docker"


class ContainerError(Exception):
    """The engine could not be brought to the state that was asked for."""

    def __init__(self, message: str):
        super().__init__(redacted(message))


def container_name(engine: str) -> str:
    return f"opus_{engine}"


def vpn_name(engine: str) -> str:
    """Each VPN-routed engine gets its own tunnel: one subscription, but a
    shared namespace would mean one engine's stall is every engine's stall."""
    return f"opus_vpn_{engine}"


def solver_name(engine: str) -> str:
    return f"opus_solver_{engine}"


class Docker:
    """The slice of the Docker Engine API this needs, over the unix socket.
    httpx already speaks it, so no extra dependency and no daemon client to keep
    in step with the server."""

    def __init__(self, socket_path: str | None = None):
        self._socket = socket_path or settings.docker_socket

    def _client(self) -> httpx.AsyncClient:
        if settings.docker_controller_url:
            return httpx.AsyncClient(
                base_url=f"{settings.docker_controller_url.rstrip('/')}/{API_VERSION}",
                timeout=60,
            )
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=self._socket),
            base_url=f"{BASE_URL}/{API_VERSION}",
            timeout=60,
        )

    async def inspect(self, name: str) -> dict[str, Any] | None:
        async with self._client() as client:
            resp = await client.get(f"/containers/{name}/json")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    async def create(self, name: str, spec: dict) -> None:
        async with self._client() as client:
            resp = await client.post("/containers/create", params={"name": name}, json=spec)
            if resp.status_code >= 400:
                raise ContainerError(f"could not create {name}: {resp.text.strip()}")

    async def start(self, name: str) -> None:
        async with self._client() as client:
            resp = await client.post(f"/containers/{name}/start")
            # 304 is "already running", which is the state we wanted anyway
            if resp.status_code not in (204, 304):
                raise ContainerError(f"could not start {name}: {resp.text.strip()}")

    async def stop(self, name: str) -> None:
        async with self._client() as client:
            resp = await client.post(f"/containers/{name}/stop", params={"t": 20})
            if resp.status_code not in (204, 304, 404):
                raise ContainerError(f"could not stop {name}: {resp.text.strip()}")

    async def remove(self, name: str) -> None:
        async with self._client() as client:
            resp = await client.delete(f"/containers/{name}", params={"v": "false"})
            if resp.status_code not in (204, 404):
                raise ContainerError(f"could not remove {name}: {resp.text.strip()}")

    async def restart(self, name: str) -> None:
        async with self._client() as client:
            resp = await client.post(f"/containers/{name}/restart", params={"t": 20})
            if resp.status_code not in (204, 304):
                raise ContainerError(f"could not restart {name}: {resp.text.strip()}")

    async def network_subnet(self, name: str) -> str:
        """The address range the engines share with OPUS. It is what tells an
        engine which callers are already on the inside."""
        async with self._client() as client:
            resp = await client.get(f"/networks/{name}")
            if resp.status_code >= 400:
                raise ContainerError(f"could not read the network {name}: {resp.text.strip()}")
            for entry in resp.json().get("IPAM", {}).get("Config") or []:
                if entry.get("Subnet"):
                    return entry["Subnet"]
            raise ContainerError(f"the network {name} has no subnet of its own")

    async def pull(self, image: str) -> None:
        """The daemon answers a pull with 200 before it has fetched anything and
        reports a registry failure inside the stream, so the stream is read to
        its end and the image is then asked for."""
        async with self._client() as client:
            async with client.stream("POST", "/images/create",
                                     params={"fromImage": image}) as resp:
                if resp.status_code >= 400:
                    raise ContainerError(
                        f"could not pull {image}: {(await resp.aread()).decode().strip()}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        said = json.loads(line)
                    except ValueError as exc:
                        raise ContainerError(f"pulling {image} answered {line!r}") from exc
                    if said.get("error"):
                        raise ContainerError(f"could not pull {image}: {said['error']}")
            resp = await client.get(f"/images/{image}/json")
            if resp.status_code != 200:
                raise ContainerError(f"{image} is not there after pulling it: {resp.text.strip()}")


def _ours(info: dict) -> bool:
    return (info.get("Config", {}).get("Labels") or {}).get(OWNER_LABEL) == "engine"


def stamped(spec: dict) -> dict:
    """The definition, carrying a fingerprint of itself.

    A running container cannot be asked what it would be created as today, so
    what it WAS created as is written on it. Anything OPUS decides — the image,
    the binds, the network, whether a port is published at all — changes the
    fingerprint, and a container whose stamp no longer matches is one built to
    an instruction that has since been withdrawn."""
    digest = hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    labelled = {**spec, "Labels": {**spec.get("Labels", {}), SPEC_LABEL: digest}}
    return labelled


def engine_spec(spec: EngineSpec, env: dict[str, str], use_vpn: bool,
                share_dir: str = "") -> dict:
    """The container definition for one bundled engine.

    Nothing is published to the host. The engine answers on the network it
    shares with OPUS and nowhere else, which is what lets it stop asking for a
    login of its own: the only way in is through a door that already asked.

    An engine routed through the VPN holds no network of its own either — it
    joins its tunnel's namespace, which is also why a VPN that will not come up
    takes its engine's network with it, the behaviour worth having."""
    # host-side paths: the daemon resolves binds, not this container
    config_dir = f"{settings.engines_host_dir}/{spec.name}"
    binds = [
        f"{config_dir}:{spec.config_mount}",
        f"{settings.landing_host_dir}:/landing",
    ]
    if spec.share_mount and share_dir:
        if paths.share_refusal(share_dir):
            raise ContainerError(
                f"{spec.name}: {share_dir} is not inside the media directory this "
                "install may share from")
        # read-only: sharing a library is offering it, not handing it over
        binds.append(f"{share_dir}:{spec.share_mount}:ro")
    host: dict[str, Any] = {
        "Binds": binds,
        "RestartPolicy": {"Name": "unless-stopped"},
        "Memory": settings.engine_memory_limit_bytes,
        "PidsLimit": settings.engine_pids_limit,
    }
    if use_vpn:
        host["NetworkMode"] = f"container:{vpn_name(spec.name)}"
    container: dict[str, Any] = {
        "Image": spec.image,
        "Env": [f"{k}={v}" for k, v in sorted(env.items())],
        "Labels": {OWNER_LABEL: "engine", f"{OWNER_LABEL}.name": spec.name},
        "HostConfig": host,
    }
    if not use_vpn:
        # inside a tunnel the engine has no network of its own to attach
        container["NetworkingConfig"] = {
            "EndpointsConfig": {settings.engine_network: {}}
        }
    return container


def vpn_spec(spec: EngineSpec, env: dict[str, str]) -> dict:
    """The tunnel an engine sits inside. It publishes nothing either: an engine
    OPUS runs is reached through OPUS, on the network they share.

    gluetun drops inbound traffic to everything except the ports it was told to
    expect, and an engine inside it has no firewall of its own to argue with —
    so without this the engine answers nobody, itself included, and says
    nothing about why."""
    env = {**env, "FIREWALL_INPUT_PORTS": str(spec.service_port)}
    return {
        "Image": "qmcgaw/gluetun",
        "Env": [f"{k}={v}" for k, v in sorted(env.items())],
        "Labels": {OWNER_LABEL: "engine", f"{OWNER_LABEL}.name": f"vpn-{spec.name}"},
        "NetworkingConfig": {"EndpointsConfig": {settings.engine_network: {}}},
        "HostConfig": {
            "Binds": [f"{settings.engines_host_dir}/{spec.name}/tunnel:/gluetun/auth"],
            "CapAdd": ["NET_ADMIN"],
            "Devices": [{
                "PathOnHost": "/dev/net/tun",
                "PathInContainer": "/dev/net/tun",
                "CgroupPermissions": "rwm",
            }],
            "RestartPolicy": {"Name": "unless-stopped"},
            "Memory": settings.vpn_memory_limit_bytes,
            "PidsLimit": settings.vpn_pids_limit,
        },
    }


def solver_spec(spec: EngineSpec, use_vpn: bool) -> dict:
    """The browser beside an engine that answers Cloudflare's challenge for it.
    A clearance holds only for the address that earned it, so the solver goes
    out wherever its engine does: inside the same tunnel, or on the same
    network when there is none. It keeps nothing, so it mounts nothing."""
    host: dict[str, Any] = {
        "Binds": [],
        "RestartPolicy": {"Name": "unless-stopped"},
        "Memory": settings.engine_memory_limit_bytes,
        "PidsLimit": settings.engine_pids_limit,
    }
    container: dict[str, Any] = {
        "Image": SOLVER_IMAGE,
        "Env": [f"TZ={settings.timezone}"],
        "Labels": {OWNER_LABEL: "engine", f"{OWNER_LABEL}.name": f"solver-{spec.name}"},
        "HostConfig": host,
    }
    if use_vpn:
        host["NetworkMode"] = f"container:{vpn_name(spec.name)}"
    else:
        container["NetworkingConfig"] = {"EndpointsConfig": {settings.engine_network: {}}}
    return container


def solver_url(spec: EngineSpec, use_vpn: bool) -> str:
    """Where the engine reaches its solver: across the tunnel's own loopback
    when both sit inside it, by name on the network otherwise."""
    host = "localhost" if use_vpn else solver_name(spec.name)
    return f"http://{host}:{SOLVER_PORT}/"


async def tunnel_exit(name: str) -> str | None:
    """The address an engine inside this tunnel appears from. Not that the
    tunnel is up — where it comes out, which is the only form of the question
    worth asking: a tunnel can be running and carrying nothing.

    None when the tunnel would not say. That is not an answer, and a leak check
    that reads it as one has quietly stopped checking."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://{name}:8000/v1/publicip/ip")
            resp.raise_for_status()
            return resp.json().get("public_ip") or None
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("%s: its exit address is unknown: %s", name, exc)
        return None


def bundled_url(spec: EngineSpec, use_vpn: bool) -> str:
    """Where a bundled engine answers OPUS. An engine inside a tunnel has no
    address of its own — the tunnel holds the namespace, so that is the name
    that resolves.

    An engine told its base path answers on it whole: its API moves there too,
    and a call to the root is redirected rather than served."""
    host = vpn_name(spec.name) if use_vpn else container_name(spec.name)
    base = ui_base_path(spec.name) if spec.serves_at_base_path else ""
    return f"http://{host}:{spec.service_port}{base}"


async def matches(docker: Docker, name: str, spec: dict) -> bool:
    """Whether what is running was built to the definition in force now. A
    setting that changes a container is only a setting once it has been applied,
    and this is how a save finds the engines it has to bring up again."""
    info = await docker.inspect(name)
    if info is None:
        return True
    current = (info.get("Config", {}).get("Labels") or {}).get(SPEC_LABEL)
    return current == stamped(spec)["Labels"][SPEC_LABEL]


async def state(docker: Docker, name: str) -> dict:
    """What the interface shows next to an engine: absent, or running with the
    image it is actually on — which is how a controlled update is noticed."""
    info = await docker.inspect(name)
    if info is None:
        return {"present": False, "running": False, "ours": False, "status": "absent"}
    return {
        "present": True,
        "running": bool(info.get("State", {}).get("Running")),
        "ours": _ours(info),
        "status": info.get("State", {}).get("Status", "?"),
        "image": info.get("Config", {}).get("Image", ""),
    }


async def address(docker: Docker, name: str) -> str:
    """The container's own address on OPUS's network.

    Needed exactly once per engine: a service that verifies the Host header it
    was reached by will refuse its own container name until it has been told to
    accept it, and an IP is never subject to that check."""
    info = await docker.inspect(name)
    if info is None:
        raise ContainerError(f"{name} is not running")
    networks = info.get("NetworkSettings", {}).get("Networks", {})
    for net in networks.values():
        if net.get("IPAddress"):
            return net["IPAddress"]
    raise ContainerError(f"{name} has no address on any network")


async def ensure(docker: Docker, name: str, spec: dict) -> bool:
    """Bring the container up, creating it if it is missing and replacing it if
    its definition changed. A container OPUS does not own is never replaced:
    the name matching is not proof it is ours to destroy.

    Returns whether it had to be created rather than merely started — which is
    what a container joined to its namespace needs to know about it."""
    info = await docker.inspect(name)
    if info is not None and not _ours(info):
        raise ContainerError(
            f"{name} exists but was not created by OPUS; refusing to replace it"
        )
    spec = stamped(spec)
    wanted = spec["Labels"][SPEC_LABEL]
    current = (info.get("Config", {}).get("Labels") or {}).get(SPEC_LABEL) if info else None
    if current == wanted:
        await docker.start(name)
        return False
    # a registry that cannot be reached must leave the running engine as it was
    await docker.pull(spec["Image"])
    if info is not None:
        log.info("%s: definition changed (%s → %s), recreating", name, current, wanted)
        await docker.stop(name)
        await docker.remove(name)
    await docker.create(name, spec)
    await docker.start(name)
    return True


async def teardown(docker: Docker, name: str) -> None:
    info = await docker.inspect(name)
    if info is None:
        return
    if not _ours(info):
        raise ContainerError(f"{name} was not created by OPUS; refusing to remove it")
    await docker.stop(name)
    await docker.remove(name)
