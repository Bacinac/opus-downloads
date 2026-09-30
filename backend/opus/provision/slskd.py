"""slskd: one YAML file it reads at start, and no API to be configured through
before it is authenticated — so what OPUS owns in that file is written there,
and the rest of it is left as whoever edited it last left it."""

import copy
import logging
import secrets

import yaml

from opus.config import settings
from opus.containers import ContainerError, Docker
from opus.engines.catalog import ui_base_path
from opus.engines.spec import EngineSpec
from opus.provision import common
from opus.settings_store import RuntimeConfig

log = logging.getLogger(__name__)

USERNAME, PASSWORD = "slskd_soulseek_username", "slskd_soulseek_password"
OWNED = (USERNAME, PASSWORD)


def _section(parent: dict, key: str) -> dict:
    if not isinstance(parent.get(key), dict):
        parent[key] = {}
    return parent[key]


def _read(path) -> dict | None:
    if not path.exists():
        return None
    try:
        held = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ContainerError(f"slskd's {path.name} is not YAML OPUS can read: {exc}") from exc
    return held if isinstance(held, dict) else {}


def seed(spec: EngineSpec, runtime: RuntimeConfig, subnet: str) -> bool:
    """Its own web login is off, like every other engine OPUS runs, and the API
    key stands: that is what identifies OPUS to it, which is a different question
    from who is looking.

    The Soulseek account is the settings' whenever both halves are set; a login
    only half given is not one, and the file keeps what it had. Shares are left
    alone: what to offer the network back is the user's decision. Returns
    whether the file changed, which a running engine has to be restarted for."""
    path = common.config_path(spec, "slskd.yml")
    held = _read(path)
    config = copy.deepcopy(held) if held else {}

    web = _section(config, "web")
    web["port"] = spec.service_port
    web["url_base"] = ui_base_path(spec.name)
    auth = _section(web, "authentication")
    auth.setdefault("disabled", True)
    jwt = _section(auth, "jwt")
    jwt.setdefault("key", secrets.token_hex(32))
    jwt.setdefault("ttl", 604800000)
    opus = _section(_section(auth, "api_keys"), "opus")
    opus.setdefault("key", secrets.token_hex(16))
    opus["role"] = "readwrite"
    opus.setdefault("cidr", "0.0.0.0/0,::/0")

    soulseek = _section(config, "soulseek")
    soulseek.setdefault("address", "vps.slsknet.org")
    soulseek.setdefault("port", 2271)
    if runtime.get(USERNAME) and runtime.get(PASSWORD):
        soulseek["username"] = runtime.get(USERNAME)
        soulseek["password"] = runtime.get(PASSWORD)

    # its own subtree, not the root of the landing zone: slskd has no categories,
    # and pointed at the root it put each finished album beside the per-engine
    # folders rather than inside one
    directories = _section(config, "directories")
    if directories.get("downloads") in (None, settings.landing_root):
        directories["downloads"] = f"{settings.landing_root}/soulseek"
    directories.setdefault("incomplete", f"{settings.landing_root}/.incomplete")

    if config == held:
        return False
    common.write_whole(path, yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    log.info("slskd: wrote what OPUS owns in its configuration")
    return True


async def configure(spec: EngineSpec, runtime: RuntimeConfig, docker: Docker,
                    previous: dict[str, str]) -> dict[str, str]:
    """The key OPUS calls it with is read back out of its file on every start
    rather than remembered from the seeding: a start that failed in between
    would otherwise lose it for good."""
    config = yaml.safe_load(await common.await_file(common.config_path(spec, "slskd.yml"))) or {}
    keys = ((config.get("web") or {}).get("authentication") or {}).get("api_keys") or {}
    key = (keys.get("opus") or {}).get("key")
    if not key:
        raise ContainerError("slskd's configuration holds no API key for OPUS")
    return {"slskd_bundled_api_key": str(key)}
