"""A narrow internal gateway to Docker for bundled download engines.

The web backend must be able to manage the engines OPUS owns, but giving it a
writeable Docker socket also gives a compromised web process host-root powers.
This service is the only container that receives that socket.  It forwards the
small Docker API subset the provisioner uses after checking every name, image,
bind, capability and network against OPUS's catalog.

It has no published port and lives on a Compose-internal network shared only
with the backend.  The checks deliberately live here, on the privileged side
of the boundary: the backend cannot relax them by changing its request.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from opus.config import settings
from opus.containers import (
    API_VERSION,
    OWNER_LABEL,
    SOLVER_IMAGE,
    container_name,
    solver_name,
    vpn_name,
)
from opus.engines.catalog import SPEC_BY_NAME, VPN_ENV
from opus.engines.spec import EngineSpec

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_ENGINE_NAMES = frozenset(name for name, spec in SPEC_BY_NAME.items() if spec.image)
_ROLES = {
    **{container_name(engine): (engine, "engine") for engine in _ENGINE_NAMES},
    **{vpn_name(engine): (engine, "tunnel") for engine in _ENGINE_NAMES},
    **{solver_name(engine): (engine, "solver")
       for engine in _ENGINE_NAMES if SPEC_BY_NAME[engine].solver},
}
_NAMES = frozenset(_ROLES)
_IMAGES = frozenset(
    [spec.image for spec in SPEC_BY_NAME.values() if spec.image] + ["qmcgaw/gluetun", SOLVER_IMAGE]
)


def _refuse(detail: str) -> None:
    raise HTTPException(status_code=403, detail=detail)


def _pairs(values: Iterable[object]) -> dict[str, str] | None:
    result: dict[str, str] = {}
    for value in values:
        if not isinstance(value, str) or "=" not in value:
            return None
        key, item = value.split("=", 1)
        if not key or key in result:
            return None
        result[key] = item
    return result


def _engine_for(name: str) -> tuple[str, str]:
    if name not in _ROLES:
        _refuse("container is not an OPUS bundled engine")
    return _ROLES[name]


def _safe_share(bind: str, target: str) -> bool:
    """Allow at most one explicit, read-only slskd library share."""
    if not settings.media_host_dir or not bind.endswith(f":{target}:ro"):
        return False
    source = bind[:-(len(target) + 4)]
    path, root = PurePosixPath(source), PurePosixPath(settings.media_host_dir)
    # Docker resolves a bind on the host. A prefix alone would let a request
    # such as /mnt/media/../../etc look allowed before that resolution escapes
    # the media tree, so reject traversal before comparing path components.
    if not path.is_absolute() or ".." in path.parts or ".." in root.parts:
        return False
    return path != root and path.is_relative_to(root)


def _valid_engine_spec(name: str, body: object) -> bool:
    if not isinstance(body, dict):
        return False
    engine, role = _engine_for(name)
    if not _valid_envelope(body, engine, role):
        return False
    env = _pairs(body.get("Env", []))
    host = body.get("HostConfig")
    if env is None or not isinstance(host, dict) or not _valid_host(host, role == "tunnel"):
        return False
    config = f"{settings.engines_host_dir.rstrip('/')}/{engine}"
    if role == "tunnel":
        return _valid_tunnel(body, host, env, config, SPEC_BY_NAME[engine])
    if role == "solver":
        return _valid_solver(body, host, env, engine)
    return _valid_engine(body, host, env, config, engine)


def _valid_envelope(body: dict, engine: str, role: str) -> bool:
    allowed = {"Image", "Env", "Labels", "HostConfig", "NetworkingConfig"}
    if set(body) - allowed or body.get("Image") not in _IMAGES:
        return False
    labels = body.get("Labels")
    if not isinstance(labels, dict):
        return False
    expected_label = {"engine": engine, "tunnel": f"vpn-{engine}", "solver": f"solver-{engine}"}[role]
    return (labels.get(OWNER_LABEL) == "engine"
            and labels.get(f"{OWNER_LABEL}.name") == expected_label
            and not set(labels) - {OWNER_LABEL, f"{OWNER_LABEL}.name", f"{OWNER_LABEL}.spec"})


def _valid_host(host: dict, tunnel: bool) -> bool:
    if set(host) - {"Binds", "Tmpfs", "RestartPolicy", "NetworkMode", "CapAdd", "Devices", "Memory",
                    "PidsLimit"}:
        return False
    limits = ((settings.vpn_memory_limit_bytes, settings.vpn_pids_limit)
              if tunnel else (settings.engine_memory_limit_bytes, settings.engine_pids_limit))
    return (host.get("RestartPolicy") == {"Name": "unless-stopped"}
            and (host.get("Memory"), host.get("PidsLimit")) == limits
            and isinstance(host.get("Binds"), list))


def _valid_tunnel(body: dict, host: dict, env: dict[str, str], config: str,
                  spec: EngineSpec) -> bool:
    return (body.get("Image") == "qmcgaw/gluetun"
            and host.get("Binds") == [f"{config}/tunnel:/gluetun/auth"]
            and host.get("CapAdd") == ["NET_ADMIN"] and host.get("NetworkMode") is None
            and host.get("Tmpfs") is None
            and host.get("Devices") == [{
                "PathOnHost": "/dev/net/tun", "PathInContainer": "/dev/net/tun",
                "CgroupPermissions": "rwm",
            }]
            and body.get("NetworkingConfig") == {"EndpointsConfig": {settings.engine_network: {}}}
            and set(env) <= set(VPN_ENV.values()) | {"FIREWALL_INPUT_PORTS"}
            and env.get("FIREWALL_INPUT_PORTS") == str(spec.service_port))


def _valid_engine(body: dict, host: dict, env: dict[str, str], config: str,
                  engine: str) -> bool:
    spec = SPEC_BY_NAME[engine]
    if (body.get("Image") != spec.image or host.get("CapAdd") is not None
            or host.get("Devices") is not None or host.get("Tmpfs") is not None):
        return False
    if not _valid_binds(host["Binds"], config, spec):
        return False
    expected_env = {"PUID": str(settings.engine_uid), "PGID": str(settings.engine_gid), "TZ": settings.timezone}
    if engine == "qbittorrent":
        expected_env["WEBUI_PORT"] = str(spec.service_port)
    if env != expected_env:
        return False
    return _valid_network(body, host, engine)


def _valid_solver(body: dict, host: dict, env: dict[str, str], engine: str) -> bool:
    return (body.get("Image") == SOLVER_IMAGE and host.get("Binds") == []
            and host.get("Tmpfs") == {"/config": ""} and host.get("CapAdd") is None and host.get("Devices") is None
            and env == {"TZ": settings.timezone} and _valid_network(body, host, engine))


def _valid_network(body: dict, host: dict, engine: str) -> bool:
    if host.get("NetworkMode") == f"container:{vpn_name(engine)}":
        return body.get("NetworkingConfig") is None
    return (host.get("NetworkMode") is None
            and body.get("NetworkingConfig") == {"EndpointsConfig": {settings.engine_network: {}}})


def _valid_binds(binds: list, config: str, spec: EngineSpec) -> bool:
    required_binds = [f"{config}:{spec.config_mount}", f"{settings.landing_host_dir}:/landing"]
    if binds[:2] != required_binds or len(binds) not in (2, 3):
        return False
    return len(binds) == 2 or (bool(spec.share_mount) and _safe_share(binds[2], spec.share_mount))


def _allowed(method: str, path: str, query: dict[str, str], body: object) -> bool:
    """Whether one Docker API request is part of OPUS's fixed engine contract."""
    parts = [unquote(part) for part in path.split("/") if part]
    if parts[:1] == ["containers"]:
        return _container_request(method, parts[1:], query, body)
    if parts[:1] == ["images"]:
        return _image_request(method, parts[1:], query, body)
    return method == "GET" and parts == ["networks", settings.engine_network] and not query


def _container_request(method: str, rest: list[str], query: dict[str, str], body: object) -> bool:
    if rest == ["create"]:
        return (method == "POST" and set(query) == {"name"} and query["name"] in _NAMES
                and _valid_engine_spec(query["name"], body))
    if len(rest) == 2 and rest[0] in _NAMES:
        return ((method == "GET" and rest[1] == "json" and not query)
                or (method == "POST" and rest[1] in {"start", "stop", "restart"}
                    and set(query) <= {"t"} and body in (None, b"", "")))
    if len(rest) == 1 and rest[0] in _NAMES:
        return method == "DELETE" and query in ({}, {"v": "false"})
    return False


def _image_request(method: str, rest: list[str], query: dict[str, str], body: object) -> bool:
    if rest == ["create"]:
        return (method == "POST" and set(query) == {"fromImage"}
                and query["fromImage"] in _IMAGES and body in (None, b"", ""))
    return (method == "GET" and len(rest) >= 2 and rest[-1] == "json"
            and "/".join(rest[:-1]) in _IMAGES and not query)


def _daemon() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=settings.docker_socket),
        base_url=f"http://docker/{API_VERSION}", timeout=60,
    )


@app.get("/api/ping")
async def ping() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/ready")
async def ready() -> dict[str, bool]:
    """Confirm that the constrained controller can still reach Docker."""
    async with _daemon() as client:
        response = await client.get("/_ping")
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="Docker daemon is unavailable")
    return {"ok": True}


@app.api_route(f"/{API_VERSION}/{{path:path}}", methods=["GET", "POST", "DELETE"])
async def docker_api(path: str, request: Request) -> Response:
    body = await request.body()
    try:
        decoded = await request.json() if body else None
    except Exception:
        _refuse("invalid Docker request body")
    query = dict(request.query_params)
    if not _allowed(request.method, path, query, decoded if body else body):
        _refuse("Docker operation is outside the OPUS engine contract")
    async with _daemon() as client:
        # Docker requires a media type for the JSON body of container creation.
        # FastAPI has already validated that body at this boundary, but httpx
        # does not infer a Content-Type when forwarding raw bytes.
        headers = {"content-type": "application/json"} if body else None
        response = await client.request(request.method, f"/{path}", params=query,
                                        content=body, headers=headers)
        content = await response.aread()
    # Docker answers successful create/start operations with an explicitly empty
    # Content-Type header. Forwarding that value makes httpx reject the gateway
    # response before the provisioner can read its status.
    headers = {"content-type": response.headers.get("content-type") or "application/json"}
    return Response(content=content, status_code=response.status_code, headers=headers)
