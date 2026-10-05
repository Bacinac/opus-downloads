"""The one contract the unified API knows. Every acquisition backend — a
managed sidecar (SABnzbd, qBittorrent, slskd), an indexer aggregator
(Prowlarr) or an in-process library (yt-dlp, a plugin's store) — is an
Engine. The API is engine- and protocol-agnostic: it calls search/inspect/grab/
status/cancel and routes a grab to the engine named by the release's grab_ref.

OPUS is mechanism only. An engine's job ends at "the file is in the landing
zone; here is the path"; scoring, the acceptance gate and naming stay in the
consuming apps."""

import abc
import enum
import posixpath
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from opus import paths
from opus.engines.catalog import credential_key, is_enabled
from opus.engines.spec import EngineSpec
from opus.redaction import redacted


class EngineError(Exception):
    """Raised on any engine failure. Never swallowed — fail loud."""

    def __init__(self, message: str):
        super().__init__(redacted(message))


class EngineMode(enum.StrEnum):
    BUNDLED = "bundled"        # OPUS runs the engine as a managed sidecar
    EXTERNAL = "external"      # point at an existing instance (url + key)
    IN_PROCESS = "in_process"  # direct-source library, runs inside the backend


class Protocol(enum.StrEnum):
    USENET = "usenet"
    TORRENT = "torrent"
    SOULSEEK = "soulseek"
    STREAM = "stream"
    WEB = "web"


def carries_music(type: str) -> bool:
    return type in ("music", "any")


@dataclass
class Release:
    """A normalized search candidate from any engine or family. `grab_ref` is
    stateless: it round-trips caller→server and carries everything grab() needs
    to fetch this release (an indexer release URL, or a service catalog id).
    Its `engine` key names the adapter that can grab it — that is what makes
    /grab protocol-agnostic. There is no server-side candidate cache (KINO's
    proven pattern)."""

    title: str
    protocol: Protocol
    source: str            # engine name that can grab it
    grab_ref: dict         # stateless grab handle, including its `engine`
    size: int | None = None
    seeders: int | None = None
    age_days: int | None = None
    # what a peer-to-peer offer says about itself. Soulseek has no swarm to
    # count, so these are its equivalent of seeders: a fast peer with nothing
    # queued finishes, a slow one behind forty others may never start.
    speed_bps: int | None = None
    queue_length: int | None = None
    bitrate: int | None = None
    subs_hint: bool | None = None
    indexer: str = ""
    # the indexer's lasting name for this one post. The download address changes
    # with every search and a re-post carries the same title, so this is the
    # only thing that says two results are the same upload.
    guid: str = ""
    # what a streaming store states about an album it sells: how many tracks it
    # holds and the master it streams from, the sample rate in Hz
    tracks: int | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None
    # the indexer's own category ids, passed through untouched. A Newznab
    # subcategory states the format the title often does not — plenty of FLAC
    # albums never say "FLAC" anywhere in their name — so dropping it would cost
    # a consumer a real signal and leave it parsing titles instead. What a
    # category means is the consumer's to decide; OPUS only carries it.
    categories: list[int] = field(default_factory=list)


@dataclass
class ContentFile:
    name: str
    size: int = 0


@dataclass
class Contents:
    """What a release declares it holds, read before anything is downloaded.

    A title is a promise; a manifest is a fact. An NZB lists its real filenames,
    which is how a post advertising a lossless deluxe edition can be caught
    holding MP3s before the bytes are fetched rather than after.

    `available` is False when the engine cannot look inside at all — which is
    not the same as a release that declares nothing. Both are honest answers and
    the caller must be able to tell them apart.

    What the file list *means* is the caller's: OPUS reports the names it found
    and never decides that a release is packed, obfuscated or unacceptable."""

    available: bool = False
    files: list["ContentFile"] = field(default_factory=list)
    detail: str = ""


@dataclass
class JobStatus:
    state: str  # queued | downloading | complete | failed
    progress: float = 0.0
    speed_bps: int | None = None
    eta_seconds: int | None = None
    detail: str = ""
    # False when the engine holds no record of the job at all. Right after a
    # grab that is a moment's lag; long after, the engine has lost it — and only
    # the job's own row knows how long it has been
    seen: bool = True


def unseen(detail: str) -> JobStatus:
    return JobStatus(state="queued", detail=detail, seen=False)


@dataclass
class EngineHealth:
    ok: bool
    detail: str = ""


@dataclass
class EngineConfig:
    """Non-secret, per-engine runtime config resolved from the settings store.
    Secrets never live here — engines read them through the CredentialProvider
    seam so a future multi-user layer can key them by user without touching any
    adapter."""

    name: str
    mode: str           # bundled | external (swappable) or in_process (direct-source)
    enabled_flag: bool  # direct-source on/off toggle; always True for swappable engines
    url: str
    # where a person opens the engine, when that is not where OPUS calls it.
    # Empty unless an adopted instance is published somewhere of its own.
    public_url: str = ""
    landing_dir: str = ""  # where OPUS sees this engine's finished downloads
    use_vpn: bool = False  # run a bundled instance inside the install's VPN netns


class Engine(abc.ABC):
    """One adapter per acquisition backend.

    `kind` is the protocol/role the engine serves; `mode` is how it runs. What
    it can do is what it is: an engine that searches is a `Searcher`, one that
    grabs is a `Grabber` — Prowlarr only searches, SABnzbd and qBittorrent only
    grab, slskd and the streaming catalogues do both."""

    def __init__(self, spec: EngineSpec, config: EngineConfig, creds):
        self.spec = spec
        self.config = config
        self.creds = creds

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def kind(self) -> str:
        return self.spec.kind

    @property
    def family(self) -> str:
        return self.spec.family

    @property
    def can_search(self) -> bool:
        return isinstance(self, Searcher)

    @property
    def can_grab(self) -> bool:
        return isinstance(self, Grabber)

    @property
    def can_link(self) -> bool:
        return isinstance(self, Linker)

    @property
    def mode(self) -> EngineMode:
        if not self.spec.swappable:
            return EngineMode.IN_PROCESS
        return EngineMode(self.config.mode)

    @property
    def enabled(self) -> bool:
        """Visible-but-disabled until its required credentials are present.
        Missing creds → False, never hidden (onboarding shows the whole
        catalog)."""
        return is_enabled(self.spec, self.config, self.creds)

    def cred(self, key: str) -> str | None:
        """A connection credential for the instance actually in use. A bundled
        engine is not the adopted one wearing a different hat — it is a separate
        service with its own login, so its credentials live under their own
        keys and provisioning cannot overwrite the adopted instance's."""
        return self.creds.get(self.name, credential_key(self.config.mode, key))

    def secret(self, key: str) -> str:
        value = self.cred(key)
        if not value:
            raise EngineError(f"{self.name}: {key} is not configured")
        return value

    def landing_dir(self) -> str:
        """OPUS's view of where this engine's downloads land — a folder it writes
        into and empties, so one outside the landing zone is refused."""
        where = self.config.landing_dir
        if not where:
            raise EngineError(f"{self.name}: no landing directory is configured")
        if paths.landing_refusal(where):
            raise EngineError(f"{self.name}: {where} is outside the landing zone")
        return where

    def landed(self, path: str) -> str:
        """A finished download's path, held to what it may be: something inside
        this engine's landing directory, never the directory itself and never a
        way out of it. Whatever the consumer does next — a move, a delete — it
        does to this."""
        root = PurePosixPath(posixpath.normpath(self.landing_dir()))
        found = PurePosixPath(posixpath.normpath(path))
        if not path.startswith("/") or found == root or not found.is_relative_to(root):
            raise EngineError(f"{self.name}: {path!r} is not inside its landing directory {root}")
        return str(found)

    async def health(self) -> EngineHealth:
        """The one channel that reports failure instead of raising it — an
        unreachable engine must show up as a red card, not as a 500 on the page
        that lists every engine."""
        if not self.enabled:
            return EngineHealth(False, "not configured")
        try:
            return await self.probe()
        except Exception as exc:
            return EngineHealth(False, redacted(str(exc)))

    @abc.abstractmethod
    async def probe(self) -> EngineHealth:
        """Live reachability + auth check against the running engine."""

    async def inspect(self, grab_ref: dict) -> Contents:
        """Look inside a release before committing to it.

        Most engines already know: slskd and the streaming catalogues ship the
        file list in the grab_ref itself, so they only have to read it back.
        Usenet is the case that earns the method — the NZB is a manifest, and it
        is small enough to fetch and parse before deciding.

        An engine that cannot look inside says so rather than returning an empty
        list, which would read as "this release holds nothing"."""
        return Contents(available=False, detail=f"{self.name} cannot inspect a release")

    async def ensure_namespace(self, namespace: str) -> None:
        """Make a content folder exist in the engine ahead of any download.
        Engines that sort into categories create one; the rest have nowhere to
        put it and say so by doing nothing."""
        return None

    async def reconcile_namespaces(self, folders: list[str]) -> None:
        """Make the engine's folders exactly the declared set.

        This runs only against an engine OPUS started, where the declared set
        is the whole truth — a fresh service ships categories of its own that
        nobody asked for, and leaving them turns "defined in one place" into
        "defined in one place plus whatever the image happened to contain"."""
        for folder in folders:
            await self.ensure_namespace(folder)


class Searcher(Engine):
    @abc.abstractmethod
    async def search(self, query: str, type: str) -> list[Release]:
        ...


class Grabber(Engine):
    """grab/status/cancel/completed_path speak in two dicts. `grab_ref` is the
    caller's stateless handle on a release; `job_ref` is what the engine hands
    back to address the running download in its own terms (an nzo_id, an
    infohash, a soulseek user plus file list). OPUS owns the job id and stores
    the job_ref against it — the engine stays stateless about OPUS."""

    claims_directory = False

    async def reference(self, grab_ref: dict, namespace: str, job_id: str) -> dict:
        return {}

    @abc.abstractmethod
    async def grab(self, grab_ref: dict, namespace: str) -> dict:
        """Start the grab tagged with the caller's namespace (per-app category
        isolation); returns the engine's own job_ref for later status polling."""

    @abc.abstractmethod
    async def status(self, job_ref: dict) -> JobStatus:
        ...

    @abc.abstractmethod
    async def cancel(self, job_ref: dict) -> None:
        """Stop the download and discard what it fetched."""

    @abc.abstractmethod
    async def completed_path(self, job_ref: dict) -> str:
        """Absolute path of the finished download inside the landing zone, in
        OPUS's own filesystem view."""


class Linker(Engine):
    """An engine whose account cannot be typed: the service hands a token to
    whoever approves a device-code link, so its card offers that conversation
    instead of a field. Each call answers the link's state — linked, the account
    it is, whether an approval is waiting and at which address, and the error
    of the last attempt."""

    @abc.abstractmethod
    async def link_status(self) -> dict:
        ...

    @abc.abstractmethod
    async def start_link(self) -> dict:
        """Begin the approval, or answer the one already waiting: the approval
        happens in the user's browser, so its outcome arrives on a later look."""

    @abc.abstractmethod
    async def unlink(self) -> dict:
        ...
