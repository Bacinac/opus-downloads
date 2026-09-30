"""The engine registry: turns the static catalog plus the stored runtime config
into live Engine adapters. One place maps a catalog name to its adapter class;
`build_engines` wires each with its resolved config and the credential seam."""

from opus_core import plugins

from opus.config import settings
from opus.containers import bundled_url
from opus.credentials import GlobalCredentialProvider
from opus.engines.base import Engine, EngineConfig
from opus.engines.catalog import CATALOG, SPEC_BY_NAME
from opus.engines.spec import EngineSpec
from opus.engines.prowlarr import ProwlarrEngine
from opus.engines.qbittorrent import QbittorrentEngine
from opus.engines.sabnzbd import SabnzbdEngine
from opus.engines.slskd import SlskdEngine
from opus.engines.ytdlp import YtdlpEngine
from opus.plugins import PLUGINS
from opus.settings_store import RuntimeConfig


class UnknownEngine(Exception):
    """A grab_ref or a stored job names an engine the catalog does not have."""


class EngineRefused(Exception):
    """The engine a request names cannot do what it was asked: it does not
    search or grab, or it is not configured."""


REGISTRY: dict[str, type[Engine]] = {
    "prowlarr": ProwlarrEngine,
    "sabnzbd": SabnzbdEngine,
    "qbittorrent": QbittorrentEngine,
    "slskd": SlskdEngine,
    "ytdlp": YtdlpEngine,
}
# a plugin's adapters are named, and imported the first time one is built
PLUGGED: dict[str, str] = {spec.name: path for plugin in PLUGINS for spec, path in plugin.engines}


def adapter(name: str) -> type[Engine]:
    return REGISTRY[name] if name in REGISTRY else plugins.resolve(PLUGGED[name])


def _engine_config(runtime: RuntimeConfig, spec: EngineSpec) -> EngineConfig:
    landing_dir = runtime.get(f"{spec.name}_landing_dir") if spec.paths else ""
    if spec.swappable:
        mode = runtime.get(f"{spec.name}_mode")
        if mode == "bundled" and spec.paths:
            # OPUS mounted the same tree at the same path in the engine, so
            # there is nothing to translate
            landing_dir = settings.landing_root
        use_vpn = runtime.bool(f"{spec.name}_use_vpn")
        # a bundled engine is wherever OPUS started it; only an adopted one has
        # an address worth storing
        url = (bundled_url(spec, use_vpn) if mode == "bundled"
               else runtime.get(f"{spec.name}_url"))
        return EngineConfig(
            name=spec.name,
            mode=mode,
            enabled_flag=True,
            url=url,
            # a bundled engine is opened through OPUS itself, so it needs no
            # address of its own; only an adopted one can be published elsewhere
            public_url="" if mode == "bundled" else runtime.get(f"{spec.name}_public_url"),
            landing_dir=landing_dir,
            use_vpn=use_vpn,
        )
    return EngineConfig(
        name=spec.name,
        mode="in_process",
        enabled_flag=runtime.bool(f"{spec.name}_enabled"),
        url="",
        landing_dir=landing_dir,
    )


def build_engines(runtime: RuntimeConfig) -> list[Engine]:
    creds = GlobalCredentialProvider(runtime)
    return [
        adapter(spec.name)(spec, _engine_config(runtime, spec), creds)
        for spec in CATALOG
    ]


def build_engine(runtime: RuntimeConfig, name: str) -> Engine:
    """The single engine a grab_ref or a running job names."""
    spec = SPEC_BY_NAME.get(name)
    if spec is None:
        raise UnknownEngine(name)
    return adapter(name)(spec, _engine_config(runtime, spec), GlobalCredentialProvider(runtime))
