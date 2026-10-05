"""The engine catalog: the single source of truth for what engines exist, how
they run and what they need. Both the settings spec (opus.settings_store) and
the /engines endpoint are derived from it, so a new engine is added in exactly
one place: here, or in a plugin (opus.plugins), whose engines follow the ones
built in.

Two families:
  * indexer_mediated — swappable service engines (bundled|external mode).
    Prowlarr aggregates indexer search; SABnzbd/qBittorrent/slskd grab.
  * direct_source — in-process libraries (enabled toggle, no mode). They query
    a service's own catalog and grab by catalog id with account credentials.
"""

from opus.engines.spec import CredField, EngineKind, EngineSpec
from opus.plugins import PLUGINS

# OPUS ships external-first (D3): adopt what runs, migrate nothing. An adopted
# engine has no address until its owner gives it one.
BUILT_IN: tuple[EngineSpec, ...] = (
    EngineSpec(
        name="prowlarr",
        kind=EngineKind.INDEXER,
        family="indexer_mediated",
        swappable=True,
        connection=(CredField("api_key", secret=True),),
        # indexer accounts are configured inside Prowlarr itself, not at OPUS
        account=(),
        image="lscr.io/linuxserver/prowlarr:latest",
        service_port=9696,
        solver=True,
    ),
    EngineSpec(
        name="sabnzbd",
        kind=EngineKind.USENET,
        family="indexer_mediated",
        swappable=True,
        connection=(CredField("api_key", secret=True),),
        account=(
            CredField("usenet_host"),
            CredField("usenet_port", default="563"),
            CredField("usenet_ssl", default="true", kind="bool"),
            CredField("usenet_connections", default="20"),
            CredField("usenet_username"),
            CredField("usenet_password", secret=True),
        ),
        paths=(CredField("landing_dir", default="/landing/usenet"),),
        image="lscr.io/linuxserver/sabnzbd:latest",
        service_port=8080,
    ),
    EngineSpec(
        name="qbittorrent",
        kind=EngineKind.TORRENT,
        family="indexer_mediated",
        swappable=True,
        connection=(CredField("user"), CredField("password", secret=True)),
        account=(),
        vpn_default=True,
        paths=(CredField("landing_dir", default="/landing/torrent"),),
        image="lscr.io/linuxserver/qbittorrent:latest",
        service_port=8080,
        serves_at_base_path=False,
        ui_embeddable=False,
    ),
    EngineSpec(
        name="slskd",
        kind=EngineKind.SOULSEEK,
        family="indexer_mediated",
        swappable=True,
        connection=(CredField("api_key", secret=True),),
        account=(
            CredField("soulseek_username"),
            CredField("soulseek_password", secret=True),
        ),
        vpn_default=True,
        paths=(CredField("landing_dir", default="/landing/soulseek"),),
        image="slskd/slskd:latest",
        service_port=5030,
        config_mount="/app",
        share_mount="/music",
    ),
    EngineSpec(
        name="ytdlp",
        kind=EngineKind.WEB,
        family="direct_source",
        swappable=False,
        account=(),  # no account needed
        default_enabled=True,
        parallel=2,
        paths=(CredField("landing_dir", default="/landing/web"),),
    ),
)

CATALOG: tuple[EngineSpec, ...] = BUILT_IN + tuple(
    spec for plugin in PLUGINS for spec, _ in plugin.engines)

SPEC_BY_NAME: dict[str, EngineSpec] = {s.name: s for s in CATALOG}
if len(SPEC_BY_NAME) != len(CATALOG):
    raise RuntimeError("a plugin names an engine the catalog already has")

# Where OPUS serves a bundled engine's own interface. An engine that can be told
# its base path is told this one, which then also becomes the path its API
# answers on — so this is not only the proxy's business and cannot live there.
UI_PREFIX = "/api/engines/{name}/ui"


def ui_base_path(name: str) -> str:
    return UI_PREFIX.format(name=name)

# One VPN account for the whole install, stored under the `vpn` group rather
# than on any engine: a subscription is not a property of a downloader, and the
# same one covers every engine that opts in (the existing stack proves the
# shape — one provider account, a separate tunnel per engine).
VPN_GROUP = "vpn"
VPN_FIELDS: tuple[CredField, ...] = (
    CredField("provider"),
    CredField("type", default="openvpn"),
    CredField("username"),
    CredField("password", secret=True),
    # where the tunnel comes out. Left empty the provider picks for you, which
    # is rarely what someone who chose a provider wants — latency and what a
    # peer network makes of the exit both follow from it.
    CredField("region"),
)

# gluetun's own names for the account, verified against a running instance
VPN_ENV = {
    "provider": "VPN_SERVICE_PROVIDER",
    "type": "VPN_TYPE",
    "username": "OPENVPN_USER",
    "password": "OPENVPN_PASSWORD",
    "region": "SERVER_REGIONS",
}

# The content folders every engine sorts into. One set defined once, created in
# each service and under the landing root, so a download of a given kind lands
# in the same place whichever engine fetched it. Callers may still name their
# own namespace at grab time; these are the ones that always exist.
CONTENT_GROUP = "content"
CONTENT_FIELDS: tuple[CredField, ...] = (
    CredField("folders", default="movies,series,music,videos"),
)


def content_folders(runtime) -> list[str]:
    raw = runtime.get(f"{CONTENT_GROUP}_folders")
    return [f.strip() for f in raw.split(",") if f.strip()]


# The token Library calls this one with. Generated, never typed by
# anyone, and readable back by an admin — unlike a password, it has to be copied
# somewhere else to be of any use.
ACCESS_GROUP = "access"
ACCESS_FIELDS: tuple[CredField, ...] = (
    CredField("library_token", hidden=True),
)


def credential_key(mode: str, key: str) -> str:
    """Connection credentials are per instance, not per engine name."""
    return f"bundled_{key}" if mode == "bundled" else key


def _required_secrets(fields: tuple[CredField, ...]) -> tuple[CredField, ...]:
    return tuple(f for f in fields if f.secret)


def vpn_configured(creds) -> bool:
    return all(creds.has(VPN_GROUP, f.key) for f in _required_secrets(VPN_FIELDS))


def is_enabled(spec: EngineSpec, config, creds) -> bool:
    """An engine is enabled once the credentials its active mode needs are all
    present. External service engines need their connection secret; bundled
    ones and direct-source engines need their account secrets. url/user and
    other non-secret fields carry defaults, so secrets are the gate.

    A bundled engine routed through the VPN also needs the install's VPN
    account: OPUS would be starting that tunnel itself, and a netns that cannot
    come up means the engine has no network at all."""
    if not spec.swappable:  # direct-source
        if not config.enabled_flag:
            return False
        return all(creds.has(spec.name, f.key) for f in _required_secrets(spec.account))
    if config.mode == "external":
        # the instance is already wherever its owner put it, VPN or not
        return all(creds.has(spec.name, f.key) for f in _required_secrets(spec.connection))
    if config.use_vpn and not vpn_configured(creds):
        return False
    if not all(creds.has(spec.name, credential_key(config.mode, f.key))
               for f in _required_secrets(spec.connection)):
        return False
    return all(creds.has(spec.name, f.key) for f in _required_secrets(spec.account))
