"""Runtime, user-editable configuration persisted in Postgres and edited via
the Settings page. Distinct from opus.config (infra bootstrap from env). The
whole spec is derived from the engine catalog: each engine contributes its mode
(swappable) or enable toggle (direct-source), its URL and its credential fields,
stored flat as `<engine>_<field>`, plus the install-wide `vpn` group that is not
owned by any single engine. Labels live in the frontend i18n catalog; this
module only defines keys, groups and validation."""

import re
from collections.abc import Callable
from dataclasses import dataclass

from opus_core.settings import RuntimeConfig as _RuntimeConfig
from opus_core.settings import SettingSpec as _SettingSpec
from opus_core.settings import SettingsValidationError, Store

from opus import paths
from opus.config import settings
from opus.db import SessionLocal
from opus.engines.catalog import (
    ACCESS_FIELDS,
    ACCESS_GROUP,
    CATALOG,
    CONTENT_FIELDS,
    CONTENT_GROUP,
    VPN_FIELDS,
    VPN_GROUP,
)
from opus.models import Setting

MODES = ("external", "bundled")

# a content folder is also the namespace an engine sorts into — a category, a
# folder under the landing zone — so it is held to what is safe as both
NAME = re.compile(r"[a-z0-9_-]+")


@dataclass(frozen=True)
class SettingSpec(_SettingSpec):
    # which mode the field belongs to, so the page asks only for what the chosen
    # mode actually needs: an adopted instance has its own URL and credentials,
    # a bundled one gets both from OPUS
    scope: str = "both"   # both | external | bundled


def _build_spec() -> list[SettingSpec]:
    specs: list[SettingSpec] = [
        SettingSpec(f"{ACCESS_GROUP}_{f.key}", ACCESS_GROUP, f.default, label=f.key,
                    secret=f.secret, hidden=f.hidden)
        for f in ACCESS_FIELDS
    ]
    specs += [
        # a set of folder names, stored on one comma-separated line because that
        # is a tidy way to keep a list, not because anyone should have to type
        # one
        SettingSpec(f"{CONTENT_GROUP}_{f.key}", CONTENT_GROUP, f.default, label=f.key,
                    secret=f.secret, kind="list")
        for f in CONTENT_FIELDS
    ]
    specs += [
        SettingSpec(f"{VPN_GROUP}_{f.key}", VPN_GROUP, f.default, label=f.key,
                    secret=f.secret, kind=f.kind)
        for f in VPN_FIELDS
    ]
    for e in CATALOG:
        if e.swappable:
            specs.append(SettingSpec(f"{e.name}_mode", e.name, "external", label="mode",
                                     kind="select", options=MODES))
            # a bundled engine's URL is whatever OPUS started it on
            specs.append(SettingSpec(f"{e.name}_url", e.name, "", label="url",
                                     scope="external"))
            # the address a person opens it at, when that is not the one OPUS
            # calls it on. An adopted instance published through a tunnel
            # answers OPUS over the LAN and its owner from anywhere, and the
            # LAN address is no use to a browser outside the network.
            specs.append(SettingSpec(f"{e.name}_public_url", e.name, "", label="public_url",
                                     scope="external"))
            # any service engine may be run inside the VPN netns; the ones that
            # expose the user to a peer swarm just start with it on
            specs.append(SettingSpec(f"{e.name}_use_vpn", e.name,
                                     "true" if e.vpn_default else "false", label="use_vpn",
                                     kind="bool"))
            # connecting to someone else's instance needs its credentials;
            # a bundled one is opened with the shared engine login instead
            for f in e.connection:
                specs.append(SettingSpec(f"{e.name}_{f.key}", e.name, f.default,
                                         label=f.key, secret=f.secret, kind=f.kind,
                                         scope="external"))
                # the bundled instance is a different service with different
                # credentials; sharing one key would have provisioning overwrite
                # the adopted instance's login
                specs.append(SettingSpec(f"{e.name}_bundled_{f.key}", e.name, "",
                                         label=f.key, secret=f.secret, scope="bundled",
                                         hidden=True))
            # third-party accounts are the user's either way, but only a bundled
            # engine has to be told them — an adopted one already knows
            for f in e.account:
                specs.append(SettingSpec(f"{e.name}_{f.key}", e.name, f.default,
                                         label=f.key, secret=f.secret, kind=f.kind,
                                         scope="bundled"))
            # an engine that shares something back needs to be given it; an
            # adopted instance was already given it by whoever started it
            if e.share_mount:
                specs.append(SettingSpec(f"{e.name}_share_dir", e.name, "",
                                         label="share_dir", scope="bundled"))
            # a bundled engine writes into the tree OPUS gave it; only an
            # adopted one has a landing folder of its own worth recording
            for f in e.paths:
                specs.append(SettingSpec(f"{e.name}_{f.key}", e.name, f.default,
                                         label=f.key, secret=f.secret, kind=f.kind,
                                         scope="external"))
        else:
            specs.append(SettingSpec(f"{e.name}_enabled", e.name,
                                     "true" if e.default_enabled else "false",
                                     label="enabled", kind="bool"))
            for f in (*e.account, *e.paths):
                specs.append(SettingSpec(f"{e.name}_{f.key}", e.name, f.default,
                                         label=f.key, secret=f.secret, kind=f.kind,
                                         hidden=f.hidden))
            # what OPUS looked up for itself is stored like anything else and
            # shown like nothing else
            for f in e.learned:
                specs.append(SettingSpec(f"{e.name}_{f.key}", e.name, f.default,
                                         label=f.key, secret=f.secret, hidden=True))
    return specs


SETTINGS_SPEC: tuple[SettingSpec, ...] = tuple(_build_spec())


def _whole(value: str, low: int, high: int) -> bool:
    return value.isascii() and value.isdigit() and low <= int(value) <= high


def _mode(spec: SettingSpec, value: str):
    if spec.label == "mode" and value == "bundled" and not settings.engines_url:
        raise SettingsValidationError(spec.key, "no_engines_url")


def _port(spec: SettingSpec, value: str):
    if not _whole(value, 1, 65535):
        raise SettingsValidationError(spec.key, "bad_port")


def _connections(spec: SettingSpec, value: str):
    # SABnzbd's own ceiling for one server
    if not _whole(value, 1, 500):
        raise SettingsValidationError(spec.key, "bad_connections")


def _names(spec: SettingSpec, value: str):
    if any(not NAME.fullmatch(f.strip()) for f in value.split(",") if f.strip()):
        raise SettingsValidationError(spec.key, "bad_name")


def _url(spec: SettingSpec, value: str):
    if value and not value.startswith(("http://", "https://")):
        raise SettingsValidationError(spec.key, "bad_url")


def _landing(spec: SettingSpec, value: str):
    if value and (code := paths.landing_refusal(value)):
        raise SettingsValidationError(spec.key, code)


def _share(spec: SettingSpec, value: str):
    if value and (code := paths.share_refusal(value)):
        raise SettingsValidationError(spec.key, code)


# the first rule whose setting matches is the one that judges the value
_RULES: tuple[tuple[Callable[[SettingSpec], bool], Callable[[SettingSpec, str], None]], ...] = (
    (lambda spec: spec.kind in ("bool", "select"), _mode),
    (lambda spec: spec.label == "usenet_port", _port),
    (lambda spec: spec.label == "usenet_connections", _connections),
    (lambda spec: spec.kind == "list", _names),
    (lambda spec: spec.label.endswith("url"), _url),
    (lambda spec: spec.label == "landing_dir", _landing),
    (lambda spec: spec.label == "share_dir", _share),
)


def _validate(spec: SettingSpec, value: str):
    rule = next((rule for applies, rule in _RULES if applies(spec)), None)
    if rule is not None:
        rule(spec, value)


class RuntimeConfig(_RuntimeConfig):
    spec = SETTINGS_SPEC


store = Store(RuntimeConfig, Setting, SessionLocal, _validate)
current_runtime = store.runtime
forget_runtime = store.forget
get_for_ui = store.for_ui
update_settings = store.update
store_credentials = store.store_credentials
