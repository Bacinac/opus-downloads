"""qBittorrent — torrent grab/track. It searches nothing (Prowlarr feeds it
releases); it adds a magnet or a .torrent and tracks the download by infohash.
Swappable: external adopts the existing instance, bundled runs one behind a VPN
netns.

The namespace becomes a qBittorrent category whose save path is a namespace
subfolder of qBittorrent's own download root, which is what keeps each consuming
app's downloads in their own subtree of the landing zone. OPUS reads that root
from the running instance rather than assuming it, so the landing mapping holds
whatever the instance is configured with."""

import hashlib
import re
from urllib.parse import parse_qs, urlparse

import httpx

from opus.engines.base import (
    EngineError,
    EngineHealth,
    Grabber,
    JobStatus,
    unseen,
)
from opus.engines.service import ServiceEngine

# The states in which the files are whole and where they will stay. A torrent
# at 100% can still be moving to its save path or being re-checked, and a
# consumer that takes the folder then takes it mid-move.
_COMPLETE = {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP"}
_FAILED = {"error", "missingFiles"}


def _bdecode(data: bytes, i: int = 0):
    if data[i:i + 1] == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if data[i:i + 1] == b"l":
        out, i = [], i + 1
        while data[i:i + 1] != b"e":
            value, i = _bdecode(data, i)
            out.append(value)
        return out, i + 1
    if data[i:i + 1] == b"d":
        out, i = {}, i + 1
        while data[i:i + 1] != b"e":
            key, i = _bdecode(data, i)
            value, i = _bdecode(data, i)
            out[key] = value
        return out, i + 1
    colon = data.index(b":", i)
    length = int(data[i:colon])
    start = colon + 1
    return data[start:start + length], start + length


def _bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(_bencode(k) + _bencode(v) for k, v in sorted(value.items())) + b"e"
    raise TypeError(f"cannot bencode {type(value)}")


def _infohash(content: bytes) -> str:
    """The id a .torrent will be tracked by, computed from its info dictionary.
    What an indexer serves is not always a torrent — a login page, an error, a
    truncated body — and that is the indexer failing, not OPUS. Its address is
    not repeated: the indexer's key rides in it."""
    try:
        meta, _ = _bdecode(content)
    except (IndexError, ValueError) as exc:
        raise EngineError(f"qbittorrent: the indexer served something that is not a "
                          f"torrent: {exc}") from exc
    if not isinstance(meta, dict) or b"info" not in meta:
        raise EngineError("qbittorrent: the indexer served something that is not a torrent")
    return hashlib.sha1(_bencode(meta[b"info"])).hexdigest()


def _magnet_hash(magnet: str) -> str | None:
    for xt in parse_qs(urlparse(magnet).query).get("xt", []):
        match = re.match(r"urn:btih:([0-9a-fA-F]{40})", xt)
        if match:
            return match.group(1).lower()
    return None


class QbittorrentEngine(ServiceEngine, Grabber):
    async def _login(self, client) -> None:
        resp = await client.post("/api/v2/auth/login", data={
            "username": self.cred("user") or "",
            "password": self.secret("password"),
        })
        # qBittorrent 5.x replies 204 (empty) + SID cookie on success; older
        # builds reply 200 "Ok." — bad credentials come back 200 "Fails."
        if not (resp.status_code == 204 or
                (resp.status_code == 200 and resp.text.strip() == "Ok.")):
            raise EngineError(f"{self.name}: login failed: "
                              f"{resp.status_code} {resp.text.strip()}")

    async def probe(self) -> EngineHealth:
        async with self.http(15) as client:
            await self._login(client)
            resp = await client.get("/api/v2/app/version")
            resp.raise_for_status()
        return EngineHealth(True, f"qBittorrent {resp.text.strip()}")

    async def _save_root(self, client) -> str:
        resp = await client.get("/api/v2/app/preferences")
        resp.raise_for_status()
        root = resp.json().get("save_path", "")
        if not root:
            raise EngineError(f"{self.name}: instance reports no default save path")
        return root.rstrip("/")

    async def _ensure_category(self, client, namespace: str, save_path: str) -> None:
        resp = await client.post("/api/v2/torrents/createCategory", data={
            "category": namespace, "savePath": save_path,
        })
        if resp.status_code == 409:  # already exists — hold it to the same path
            resp = await client.post("/api/v2/torrents/editCategory", data={
                "category": namespace, "savePath": save_path,
            })
        if resp.status_code not in (200, 204):
            raise EngineError(f"{self.name}: category {namespace!r} could not be set to "
                              f"{save_path!r}: {resp.status_code} {resp.text.strip()}")

    async def ensure_namespace(self, namespace: str) -> None:
        async with self.http(30) as client:
            await self._login(client)
            root = await self._save_root(client)
            await self._ensure_category(client, namespace, f"{root}/{namespace}")

    async def grab(self, grab_ref: dict, namespace: str) -> dict:
        title = grab_ref.get("title", "")
        magnet = grab_ref.get("magnet")
        async with self.http(60) as client:
            await self._login(client)
            await self._ensure_category(client, namespace, f"{await self._save_root(client)}/{namespace}")
            if magnet:
                infohash = _magnet_hash(magnet)
                if not infohash:
                    raise EngineError(f"{self.name}: the magnet carries no infohash")
                resp = await client.post("/api/v2/torrents/add", data={
                    "urls": magnet, "category": namespace,
                })
            else:
                # the indexer serves a .torrent file: fetch it here so the job is
                # tracked by the infohash we computed, not one qBittorrent picked
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as fetcher:
                    torrent = await fetcher.get(grab_ref["torrent_url"])
                    torrent.raise_for_status()
                infohash = _infohash(torrent.content)
                resp = await client.post(
                    "/api/v2/torrents/add",
                    files={"torrents": ("release.torrent", torrent.content,
                                        "application/x-bittorrent")},
                    data={"category": namespace},
                )
            infohash = self._added_hash(resp, infohash)
        return {"hash": infohash, "title": title, "category": namespace}

    def _added_hash(self, resp, computed: str) -> str:
        """Confirm the add and settle on the hash the job is tracked by.

        qBittorrent 5.2 answers with JSON counts and the ids it accepted; older
        builds answer with plain "Ok." or "Fails.". The id the server reports is
        the one it will answer status queries for, so it wins over the hash
        computed from the torrent file — they agree, but only one of them is
        authoritative."""
        if resp.status_code not in (200, 204):
            raise EngineError(f"{self.name}: add failed: "
                              f"{resp.status_code} {resp.text.strip()}")
        body = resp.text.strip()
        if body.startswith("{"):
            data = resp.json()
            if data.get("failure_count") or not data.get("success_count"):
                raise EngineError(f"{self.name}: add rejected: {body}")
            added = data.get("added_torrent_ids") or []
            return added[0].lower() if added else computed
        # the older plain-text reply says "Fails." on a rejected add
        if "fail" in body.lower():
            raise EngineError(f"{self.name}: add failed: {resp.status_code} {body}")
        return computed

    async def _torrent(self, client, infohash: str) -> dict | None:
        resp = await client.get("/api/v2/torrents/info", params={"hashes": infohash})
        resp.raise_for_status()
        torrents = resp.json()
        return torrents[0] if torrents else None

    async def status(self, job_ref: dict) -> JobStatus:
        async with self.http(15) as client:
            await self._login(client)
            torrent = await self._torrent(client, job_ref["hash"])
        if torrent is None:
            return unseen("not in qBittorrent")
        state = torrent.get("state", "")
        if state in _COMPLETE:
            return JobStatus(state="complete", progress=1.0, detail=torrent.get("name", ""))
        if state in _FAILED:
            return JobStatus(state="failed", detail=f"qBittorrent state: {state}")
        eta = torrent.get("eta")
        return JobStatus(
            state="downloading", progress=float(torrent.get("progress", 0)),
            speed_bps=torrent.get("dlspeed"),
            # qBittorrent parks an unknown ETA at 8640000 (100 days)
            eta_seconds=eta if isinstance(eta, int) and eta < 8640000 else None,
            detail=f"{state} — {torrent.get('num_seeds', 0)} seeds",
        )

    async def completed_path(self, job_ref: dict) -> str:
        async with self.http(15) as client:
            await self._login(client)
            torrent = await self._torrent(client, job_ref["hash"])
            if torrent is None:
                raise EngineError(f"{self.name}: torrent {job_ref['hash']} is gone")
            content_path = torrent.get("content_path", "")
            if not content_path:
                raise EngineError(f"{self.name}: torrent {job_ref['hash']} has no content path")
            root = await self._save_root(client)
        return self.landing_path(content_path, root)

    async def cancel(self, job_ref: dict) -> None:
        async with self.http(30) as client:
            await self._login(client)
            resp = await client.post("/api/v2/torrents/delete", data={
                "hashes": job_ref["hash"], "deleteFiles": "true",
            })
            resp.raise_for_status()
