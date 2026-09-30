"""SABnzbd: its own key learned from disk, and everything after that set through
its API, so it rewrites its own file and OPUS never races it."""

import logging
import re

import httpx

from opus.config import settings
from opus.containers import ContainerError, Docker, address
from opus.engines.catalog import ui_base_path
from opus.engines.spec import EngineSpec
from opus.provision import common
from opus.settings_store import RuntimeConfig

log = logging.getLogger(__name__)

HOST, PORT, SSL, CONNECTIONS, USERNAME, PASSWORD = (
    "sabnzbd_usenet_host", "sabnzbd_usenet_port", "sabnzbd_usenet_ssl",
    "sabnzbd_usenet_connections", "sabnzbd_usenet_username", "sabnzbd_usenet_password")
OWNED = (HOST, PORT, SSL, CONNECTIONS, USERNAME, PASSWORD)


def _listed(value) -> list[str]:
    """A comma-separated setting and the list SABnzbd answers with are the same
    setting, so a value is compared as what it holds rather than how it is
    spelled — otherwise its own normalising reads as a refusal."""
    items = value if isinstance(value, list) else str(value).split(",")
    return [str(v).strip() for v in items if str(v).strip()]


async def _sab(client, api_key: str, **params) -> dict:
    """One call to SABnzbd, and proof that it did what was asked.

    Checking the status flag is not enough: told to move its folders somewhere
    it cannot write, SABnzbd answers 200 with the value it kept — no error, no
    failed status, just the old setting echoed back as though it were the new
    one. So a set is read back out of the answer and disagreement is raised."""
    resp = await client.get("/api", params={"apikey": api_key, "output": "json", **params})
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") is False:
        raise ContainerError(f"sabnzbd rejected {params.get('mode')}: {data.get('error')}")
    # only a plain keyword=value set can be read back: a server is defined by a
    # whole block of parameters and comes back as a list of them
    if params.get("mode") == "set_config" and "value" in params:
        keyword, value = params.get("keyword"), params.get("value")
        section = data.get("config", {}).get(params.get("section"))
        got = section.get(keyword) if isinstance(section, dict) else None
        if got is not None and _listed(got) != _listed(value):
            raise ContainerError(
                f"sabnzbd kept {keyword}={got!r} instead of {value!r} — it did not "
                f"say why, and the usual reason is that it cannot write there"
            )
    return data


async def _servers(client, api_key: str) -> set[str]:
    data = await _sab(client, api_key, mode="get_config", section="servers")
    return {s.get("name") for s in data.get("config", {}).get("servers") or []}


async def configure(spec: EngineSpec, runtime: RuntimeConfig, docker: Docker,
                    previous: dict[str, str]) -> dict[str, str]:
    """The usenet account is the user's and cannot be invented: without it the
    engine comes up configured but unable to fetch anything, which is what the
    card will say. The server is named after its host, so a changed host
    replaces the one the old host named rather than adding a second."""
    text = await common.await_file(common.config_path(spec, "sabnzbd.ini"))
    found = re.search(r"^api_key\s*=\s*(\S+)", text, re.M)
    if not found:
        raise ContainerError("sabnzbd wrote a config without an API key")
    api_key = found.group(1)

    # SABnzbd verifies the hostname it was reached by and starts out trusting
    # only its own container id, so the first call has to arrive at an address —
    # which that check does not apply to — and its job is to make the name work
    reached_as = common.reached_as(spec, common.uses_vpn(spec, runtime))
    ip = await address(docker, reached_as)
    url = f"http://{ip}:{spec.service_port}"
    await common.await_http(url)
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        await _sab(client, api_key, mode="set_config", section="misc",
                   keyword="host_whitelist", value=reached_as)
        await _sab(client, api_key, mode="set_config", section="misc",
                   keyword="url_base", value=ui_base_path(spec.name))
        # nothing reaches this engine but OPUS, which already asked
        await _sab(client, api_key, mode="set_config", section="misc",
                   keyword="username", value="")
        await _sab(client, api_key, mode="set_config", section="misc",
                   keyword="password", value="")
        # the engine sees the landing tree at the same path OPUS does, so the
        # completed folder needs no translation on the way back
        await _sab(client, api_key, mode="set_config", section="misc",
                   keyword="complete_dir", value=settings.landing_root)
        await _sab(client, api_key, mode="set_config", section="misc",
                   keyword="download_dir", value=f"{settings.landing_root}/.incomplete")

        host = runtime.get(HOST)
        old_host = previous.get(HOST)
        if old_host and old_host != host and old_host in await _servers(client, api_key):
            await _sab(client, api_key, mode="del_config", section="servers", keyword=old_host)
        news_pass = runtime.get(PASSWORD)
        if host and news_pass:
            await _sab(client, api_key, mode="set_config", section="servers",
                       keyword=host, host=host, port=runtime.get(PORT),
                       ssl=int(runtime.bool(SSL)), enable=1,
                       connections=runtime.get(CONNECTIONS), username=runtime.get(USERNAME),
                       password=news_pass)
        else:
            log.info("sabnzbd: no usenet account set, leaving its servers alone")
    return {"sabnzbd_bundled_api_key": api_key}
