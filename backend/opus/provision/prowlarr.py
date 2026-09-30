"""Prowlarr: its own key learned, its login handed to the proxy, its solver
registered, and the grab clients OPUS runs registered with it."""

import re

import httpx

from opus.containers import (
    ContainerError,
    Docker,
    bundled_url,
    container_name,
    solver_url,
    state,
)
from opus.engines.base import EngineMode
from opus.engines.catalog import content_folders, ui_base_path
from opus.engines.spec import EngineSpec
from opus.engines.registry import build_engines
from opus.provision import common
from opus.settings_store import RuntimeConfig

# Prowlarr's own names for the two clients OPUS bundles
_CLIENT_IMPLEMENTATIONS = {
    "sabnzbd": ("Sabnzbd", "usenet"),
    "qbittorrent": ("QBittorrent", "torrent"),
}

# Prowlarr sends an indexer through a proxy only when they share a tag, so this
# is the word an indexer behind Cloudflare is given
SOLVER_TAG = "flaresolverr"


def _refusal(resp: httpx.Response) -> str:
    """What Servarr says is wrong, without the values it echoes back with it —
    a rejected client definition carries the key it was sent."""
    try:
        said = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    if isinstance(said, list):
        messages = [e.get("errorMessage") for e in said if isinstance(e, dict)]
        if any(messages):
            return "; ".join(m for m in messages if m)
    return f"HTTP {resp.status_code}"


async def configure(spec: EngineSpec, runtime: RuntimeConfig, docker: Docker,
                    previous: dict[str, str]) -> dict[str, str]:
    """Prowlarr mints its own API key on first boot and writes it to config.xml.
    OPUS reads it rather than forcing one in — the key the engine generated is
    the one it will accept.

    Its own login is then turned off. Nothing can reach this engine except OPUS,
    which publishes no port for it and has already asked who you are. Servarr
    calls this `external`: the proxy in front is the authority."""
    text = await common.await_file(common.config_path(spec, "config.xml"))
    found = re.search(r"<ApiKey>([^<]+)</ApiKey>", text)
    if not found:
        raise ContainerError("prowlarr wrote a config without an API key")
    api_key = found.group(1)

    base = ui_base_path(spec.name)
    served = bundled_url(spec, common.uses_vpn(spec, runtime))
    # Where its API answers now is wherever it was last told: at the root on a
    # first boot, under the base path once it has been moved there. The file
    # says which, and asking the other address is answered with a 404.
    said = re.search(r"<UrlBase>([^<]*)</UrlBase>", text)
    answering = served.removesuffix(base) + (said.group(1).rstrip("/") if said else "")
    key = {"X-Api-Key": api_key}

    await common.await_http(answering)
    async with httpx.AsyncClient(base_url=answering, headers=key, timeout=30) as client:
        resp = await client.get("/api/v1/config/host")
        resp.raise_for_status()
        host = resp.json()
        # Servarr takes the whole resource back, so the fetched one is edited
        # rather than a fresh object posted
        host_changed = host.get("urlBase") != base
        host.update(
            urlBase=base,
            authenticationMethod="external",
            authenticationRequired="enabled",
        )
        resp = await client.put(f"/api/v1/config/host/{host['id']}", json=host)
        if resp.status_code >= 400:
            raise ContainerError(
                f"prowlarr would not hand its login to the proxy: {_refusal(resp)}")
    if host_changed:
        # Servarr builds the links to its own assets at startup, so a base path
        # set while it runs applies to nothing it has already rendered
        await docker.restart(container_name(spec.name))
        await common.await_http(f"{served}/api/v1/config/host", headers=key)
    async with httpx.AsyncClient(base_url=served, headers=key, timeout=90) as client:
        await _link_solver(client, solver_url(spec, common.uses_vpn(spec, runtime)))
    return {"prowlarr_bundled_api_key": api_key}


async def _link_solver(client: httpx.AsyncClient, host: str) -> None:
    """Register the solver OPUS runs beside Prowlarr as its FlareSolverr proxy.

    Saving one makes Prowlarr fetch a page through it, and a browser that has
    only just started refuses that for a few seconds — so the definition is
    tested until it passes, and saved only then."""
    resp = await client.get("/api/v1/tag")
    resp.raise_for_status()
    tag = next((t["id"] for t in resp.json() if t["label"] == SOLVER_TAG), None)
    if tag is None:
        resp = await client.post("/api/v1/tag", json={"label": SOLVER_TAG})
        resp.raise_for_status()
        tag = resp.json()["id"]
    resp = await client.get("/api/v1/indexerproxy/schema")
    resp.raise_for_status()
    body = next(s for s in resp.json() if s["implementation"] == "FlareSolverr")
    body.update(
        name="FlareSolverr",
        tags=[tag],
        fields=[{**f, "value": host} if f["name"] == "host" else f for f in body["fields"]],
    )
    resp = await common.await_accepted(client, "/api/v1/indexerproxy/test", body)
    if resp.status_code >= 400:
        raise ContainerError(f"prowlarr's solver never passed its test: {_refusal(resp)}")
    resp = await client.get("/api/v1/indexerproxy")
    resp.raise_for_status()
    current = next((p for p in resp.json() if p["implementation"] == "FlareSolverr"), None)
    if current is None:
        resp = await client.post("/api/v1/indexerproxy", json=body)
    else:
        body["id"] = current["id"]
        resp = await client.put(f"/api/v1/indexerproxy/{current['id']}", json=body)
    if resp.status_code >= 400:
        raise ContainerError(f"prowlarr would not take its solver: {_refusal(resp)}")


def _client_overrides(engine, use_vpn: bool, folders: list[str]) -> dict:
    """How Prowlarr should reach the grab client. It talks to it directly on
    OPUS's network, so the address is the container's name — or its tunnel's,
    when the engine lives inside one and has no address of its own."""
    over = {
        "host": common.reached_as(engine.spec, use_vpn),
        "port": engine.spec.service_port,
        "useSsl": False,
    }
    if engine.name == "sabnzbd":
        over["apiKey"] = engine.secret("api_key")
    else:
        over["username"] = engine.cred("user") or ""
        over["password"] = engine.secret("password")
    # OPUS names the category on every grab, so this one is never used by it —
    # but Prowlarr refuses a client whose default category does not exist in the
    # service, so it has to be one of the declared folders
    if folders:
        over["category"] = folders[0]
    return over


async def link_clients(docker: Docker, runtime: RuntimeConfig) -> list[str]:
    """Register OPUS's grab clients with its Prowlarr, so it hands a release
    straight to the client that fetches it with nobody typing two addresses and
    two keys.

    The definition is built from the schema Prowlarr itself publishes and only
    the values OPUS knows are overridden: posting just the interesting fields
    fails its connectivity test on a null reference.

    Only engines OPUS runs and can call are registered, and only into a
    Prowlarr it runs that is up: an adopted one's client list is its owner's,
    and a stopped one is told when it next starts."""
    engines = build_engines(runtime)
    prowlarr = next(e for e in engines if e.name == "prowlarr")
    if prowlarr.mode is not EngineMode.BUNDLED or not prowlarr.enabled:
        return []
    if not (await state(docker, container_name(prowlarr.name)))["running"]:
        return []
    folders = content_folders(runtime)
    linked = []
    try:
        async with httpx.AsyncClient(base_url=prowlarr.config.url, timeout=30,
                                     headers={"X-Api-Key": prowlarr.secret("api_key")}) as client:
            resp = await client.get("/api/v1/downloadclient/schema")
            resp.raise_for_status()
            schemas = {s["implementation"]: s for s in resp.json()}
            resp = await client.get("/api/v1/downloadclient")
            resp.raise_for_status()
            existing = {c["name"]: c for c in resp.json()}
            for engine in engines:
                entry = _CLIENT_IMPLEMENTATIONS.get(engine.name)
                if entry is None or engine.mode is not EngineMode.BUNDLED or not engine.enabled:
                    continue
                implementation, protocol = entry
                body = dict(schemas[implementation])
                over = _client_overrides(engine, engine.config.use_vpn, folders)
                body["fields"] = [
                    {**f, "value": over.get(f["name"], f.get("value", ""))}
                    for f in body["fields"]
                ]
                body.update(name=engine.name, enable=True, priority=1, protocol=protocol)
                current = existing.get(engine.name)
                if current is None:
                    resp = await client.post("/api/v1/downloadclient", json=body)
                else:
                    body["id"] = current["id"]
                    resp = await client.put(f"/api/v1/downloadclient/{current['id']}", json=body)
                if resp.status_code >= 400:
                    raise ContainerError(
                        f"prowlarr rejected the {engine.name} client: {_refusal(resp)}")
                linked.append(engine.name)
    except httpx.HTTPError as exc:
        raise ContainerError(f"prowlarr could not be told about the grab clients: {exc}") from exc
    return linked
