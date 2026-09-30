"""What an engine is: its kind, the fields it is configured by, and the spec
the catalog holds for it. Kept apart from the catalog so that a plugin can say
what its engines are while the catalog is still being put together."""

import enum
from dataclasses import dataclass


class EngineKind(enum.StrEnum):
    INDEXER = "indexer"    # search-side aggregator (Prowlarr); feeds usenet + torrent
    USENET = "usenet"
    TORRENT = "torrent"
    SOULSEEK = "soulseek"
    STREAM = "stream"
    WEB = "web"


@dataclass(frozen=True)
class CredField:
    """One configurable field of an engine, stored as `<engine>_<key>`.
    A `secret` field is write-only: never echoed back, and its presence is what
    gates the engine's `enabled` state. A `hidden` field is still the user's —
    it just does not arrive by typing, the way an OAuth link hands over a token
    rather than a password."""

    key: str
    secret: bool = False
    default: str = ""
    hidden: bool = False
    kind: str = "text"


@dataclass(frozen=True)
class EngineSpec:
    name: str
    kind: EngineKind
    family: str            # indexer_mediated | direct_source
    swappable: bool        # True → bundled|external mode; False → in_process
    # service-engine connection (url + auth). Gates `enabled` in external mode.
    connection: tuple[CredField, ...] = ()
    # third-party account credentials needed only in bundled mode (a usenet
    # provider, a soulseek login) or by a direct-source engine (an account
    # token). Gates `enabled` in bundled / direct-source mode.
    account: tuple[CredField, ...] = ()
    # values OPUS works out for itself and then keeps — a store's app secret
    # read out of its web player, the display name behind a linked token. Stored
    # and validated like any setting, never shown, never typed, and never part
    # of what gates `enabled`: an engine is not unconfigured because OPUS has
    # not yet gone and looked something up.
    learned: tuple[CredField, ...] = ()
    # whether this engine's VPN toggle starts on. Any service engine can be run
    # inside a VPN netns; the ones that expose the user to a peer swarm just
    # default to it. The VPN itself is one install-wide account (VPN_FIELDS) —
    # one subscription, one tunnel per engine that asks for it.
    vpn_default: bool = False
    # where OPUS sees this engine's finished downloads. The engine reports its
    # completed paths in its own filesystem view; both views point at one shared
    # directory, and this is OPUS's side of it. Grab-capable engines only.
    paths: tuple[CredField, ...] = ()
    default_enabled: bool = False  # direct-source engines only
    # how many of an in-process engine's downloads fetch at once; the rest wait
    parallel: int = 1
    # what OPUS starts when this engine runs bundled. The service keeps its
    # native port inside the container and nothing is published to the host:
    # OPUS reaches it on the network they share, and so does nobody else.
    image: str = ""
    service_port: int = 0
    # what the engine offers back to its network, as against what it downloads.
    # Soulseek is two-way and slskd shares a library on it; the path is the
    # install's business, the mount point is what the engine's config names.
    share_mount: str = ""
    # where the image keeps its configuration. Most use /config; slskd keeps
    # everything under /app and silently falls back to a copy inside the
    # container when that is not mounted.
    config_mount: str = "/config"
    # whether the engine can be told the path it is served at (Servarr's
    # urlBase, SABnzbd's url_base, slskd's web.url_base). qBittorrent has no
    # such setting and answers 404 to anything carrying a prefix, so what is
    # served under one has to be cut back to its root before it arrives.
    serves_at_base_path: bool = True
    # whether a browser runs beside it when bundled, one that answers a site's
    # Cloudflare challenge so the indexers behind one can be searched at all
    solver: bool = False
