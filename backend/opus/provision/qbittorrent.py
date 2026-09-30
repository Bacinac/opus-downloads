"""qBittorrent: a configuration written before it has ever run, because it
cannot be told its password afterwards without first logging in with a
temporary one it prints to its own log."""

import base64
import configparser
import hashlib
import io
import json
import logging
import re
import secrets

import httpx

from opus.config import settings
from opus.containers import ContainerError, Docker, bundled_url
from opus.engines.spec import EngineSpec
from opus.provision import common
from opus.settings_store import RuntimeConfig

log = logging.getLogger(__name__)

USER, PASSWORD = "qbittorrent_bundled_user", "qbittorrent_bundled_password"

_PLAIN = re.compile(r"[A-Za-z0-9._/*:+-]+")


def credentials(runtime: RuntimeConfig) -> dict[str, str]:
    """The machine login OPUS keeps for it, made once. qBittorrent lets its own
    network in without asking, so nobody types it; it exists because the engine
    has to hold one and because an engine is configured only once its
    credentials are recorded."""
    made = {}
    if not runtime.get(USER):
        made[USER] = "opus"
    if not runtime.get(PASSWORD):
        made[PASSWORD] = secrets.token_urlsafe(24)
    return made


def password_hash(password: str) -> str:
    """qBittorrent stores its web password as PBKDF2-HMAC-SHA512 over a random
    salt, both base64, in one @ByteArray value."""
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100_000, dklen=64)
    return f"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(key).decode()})"


def conf_value(value: str) -> str:
    """A value as Qt's settings file reads it back: anything but a plain token
    is quoted and escaped, because an unquoted comma makes a list and a leading
    @ makes a type."""
    if _PLAIN.fullmatch(value):
        return value
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    if escaped.startswith("@") and not escaped.startswith("@ByteArray("):
        escaped = f"@{escaped}"
    return f'"{escaped}"'


def seed(spec: EngineSpec, runtime: RuntimeConfig, subnet: str) -> bool:
    """Two protections are off deliberately. This instance is OPUS's, reached
    only through OPUS, and both of them exist to stop a *different* origin
    driving the web UI — which is exactly what OPUS does, and what showing the
    engine inside its own tab will do.

    Its login is off for callers on the network it shares with OPUS, which is
    the only network it is on. qBittorrent has no setting for handing
    authentication to a proxy, and this is the nearest thing it has.

    Written once: the running engine rewrites this file itself on every exit,
    so nothing here is changed under it afterwards."""
    path = common.config_path(spec, "qBittorrent", "qBittorrent.conf")
    if path.exists():
        return False
    sections = {
        "BitTorrent": {
            "Session\\DefaultSavePath": settings.landing_root,
            "Session\\TempPath": f"{settings.landing_root}/.incomplete",
        },
        "Preferences": {
            "WebUI\\Address": "*",
            "WebUI\\ServerDomains": "*",
            "WebUI\\Port": str(spec.service_port),
            "WebUI\\Username": runtime.get(USER),
            "WebUI\\Password_PBKDF2": password_hash(runtime.get(PASSWORD)),
            "WebUI\\AuthSubnetWhitelistEnabled": "true",
            "WebUI\\AuthSubnetWhitelist": subnet,
            "WebUI\\ClickjackingProtection": "false",
            "WebUI\\CSRFProtection": "false",
            "WebUI\\HostHeaderValidation": "false",
        },
    }
    conf = configparser.RawConfigParser()
    conf.optionxform = str
    conf.read_dict({name: {key: conf_value(value) for key, value in values.items()}
                    for name, values in sections.items()})
    out = io.StringIO()
    conf.write(out, space_around_delimiters=False)
    common.write_whole(path, out.getvalue())
    log.info("qbittorrent: seeded its configuration")
    return True


async def configure(spec: EngineSpec, runtime: RuntimeConfig, docker: Docker,
                    previous: dict[str, str]) -> dict[str, str]:
    """The network it lets in without a login is set on every start, not only
    seeded: the file is the engine's own once it runs, and the engines' network
    is not the one an older install seeded it with."""
    url = bundled_url(spec, common.uses_vpn(spec, runtime))
    subnet = await docker.network_subnet(settings.engine_network)
    preferences = {"json": json.dumps({"bypass_auth_subnet_whitelist_enabled": True,
                                       "bypass_auth_subnet_whitelist": subnet})}
    await common.await_http(url)
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        resp = await client.post("/api/v2/app/setPreferences", data=preferences)
        if resp.status_code == 403:
            login = await client.post("/api/v2/auth/login", data={
                "username": runtime.get(USER), "password": runtime.get(PASSWORD)})
            if login.status_code not in (200, 204) or login.text.strip() == "Fails.":
                raise ContainerError("qbittorrent refused the login OPUS keeps for it")
            resp = await client.post("/api/v2/app/setPreferences", data=preferences)
        if resp.status_code >= 400:
            raise ContainerError(f"qbittorrent would not let {subnet} in: HTTP {resp.status_code}")
    return {}
