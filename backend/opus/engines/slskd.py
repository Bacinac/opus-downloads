"""Soulseek acquisition through isolated, durably recorded download batches."""

import asyncio
import posixpath
from urllib.parse import quote
from pathlib import Path

from opus.config import settings
from opus.engines.base import (
    ContentFile,
    Contents,
    EngineError,
    EngineHealth,
    Grabber,
    JobStatus,
    Protocol,
    Release,
    Searcher,
    carries_music,
    unseen,
)
from opus.engines.service import ServiceEngine

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".ape", ".wv",
                    ".dsf", ".dff"}

_FAILED_STATES = ("Errored", "Rejected", "Cancelled", "TimedOut")
_SUCCEEDED = "Completed, Succeeded"


def _normalize(path: str) -> str:
    """Soulseek peers are mostly Windows: paths arrive backslash-separated."""
    return path.replace("\\", "/")


def _folder(directory: str) -> str:
    return posixpath.basename(_normalize(directory))


def _remove(folder: Path, names: list[str]) -> None:
    for name in names:
        (folder / name).unlink(missing_ok=True)
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()


class SlskdEngine(ServiceEngine, Searcher, Grabber):
    api_path = "/api/v0"
    claims_directory = True

    async def reference(self, grab_ref: dict, namespace: str, job_id: str) -> dict:
        from opus.settings_store import NAME

        if not NAME.fullmatch(namespace):
            raise EngineError(f"{self.name}: invalid batch namespace")
        async with self.http(15) as client:
            root = await self._downloads_root(client)
        destination = f"{namespace}/{job_id}"
        return {"username": grab_ref["username"], "batch": job_id,
                "destination": destination, "landing": self._landed(root, destination),
                "files": [posixpath.basename(_normalize(file["filename"]))
                          for file in grab_ref["files"]]}

    def auth_headers(self) -> dict[str, str]:
        return {"X-API-Key": self.secret("api_key")}

    async def probe(self) -> EngineHealth:
        async with self.http(15) as client:
            resp = await client.get("/application")
            resp.raise_for_status()
            server = resp.json().get("server", {})
        if server.get("isConnected") and server.get("isLoggedIn"):
            return EngineHealth(True, "connected to Soulseek")
        return EngineHealth(False, f"not connected to Soulseek (state: {server.get('state')})")

    async def search(self, query: str, type: str) -> list[Release]:
        if not carries_music(type):
            return []
        async with self.http(settings.search_timeout_seconds + 15) as client:
            resp = await client.post("/searches", json={"searchText": query})
            resp.raise_for_status()
            search_id = resp.json()["id"]

            # a Soulseek search has no completion event: peers answer for as long
            # as they answer, so it runs to its own deadline and reads whatever
            # arrived by then
            deadline = asyncio.get_running_loop().time() + settings.search_timeout_seconds
            while True:
                resp = await client.get(f"/searches/{search_id}")
                resp.raise_for_status()
                if "Completed" in resp.json().get("state", ""):
                    break
                if asyncio.get_running_loop().time() > deadline:
                    break
                await asyncio.sleep(2)

            resp = await client.get(f"/searches/{search_id}/responses")
            resp.raise_for_status()
            responses = resp.json()
        return self._to_releases(responses)

    def _to_releases(self, responses: list[dict]) -> list[Release]:
        releases: list[Release] = []
        for response in responses:
            username = response.get("username", "")
            by_directory: dict[str, list[dict]] = {}
            for file in response.get("files", []):
                filename = _normalize(file.get("filename", ""))
                if posixpath.splitext(filename)[1].lower() in AUDIO_EXTENSIONS:
                    by_directory.setdefault(posixpath.dirname(filename), []).append(file)
            for directory, files in by_directory.items():
                bitrates = [f["bitRate"] for f in files if f.get("bitRate")]
                releases.append(Release(
                    title=f"{username}: {posixpath.basename(directory)}",
                    protocol=Protocol.SOULSEEK,
                    source=self.name,
                    grab_ref={
                        "engine": self.name,
                        "username": username,
                        "directory": directory,
                        # the per-file bitrate rides along: an album is not one
                        # number, and a consumer judging an edition reads the
                        # files rather than their maximum
                        "files": [{"filename": f["filename"], "size": f.get("size", 0),
                                   "bitrate": f.get("bitRate")}
                                  for f in files],
                    },
                    size=sum(f.get("size", 0) for f in files),
                    bitrate=max(bitrates) if bitrates else None,
                    speed_bps=response.get("uploadSpeed"),
                    queue_length=response.get("queueLength"),
                    indexer=username,
                ))
        return releases

    async def inspect(self, grab_ref: dict) -> Contents:
        """Nothing to fetch: a Soulseek search already returned the peer's real
        file list, and it rode back here in the grab_ref."""
        files = grab_ref.get("files") or []
        if not files:
            return Contents(available=False, detail="the release carries no file list")
        return Contents(
            available=True,
            files=[
                ContentFile(name=posixpath.basename(_normalize(f["filename"])),
                            size=f.get("size", 0))
                for f in files
            ],
            detail=f"{len(files)} files offered by {grab_ref.get('username', 'the peer')}",
        )

    async def grab(self, grab_ref: dict, namespace: str) -> dict:
        reference = grab_ref["_prepared"]
        files = grab_ref["files"]
        if not files:
            raise EngineError(f"{self.name}: grab_ref carries no files")
        if len(set(reference["files"])) != len(files):
            raise EngineError(f"{self.name}: duplicate destination filenames")
        if Path(reference["landing"]).exists():
            raise EngineError(f"{self.name}: batch directory already exists")
        wanted = [{"filename": file["filename"], "size": file.get("size", 0)} for file in files]
        async with self.http(30) as client:
            response = await client.post("/transfers/downloads/batches", json={
                "id": reference["batch"], "username": reference["username"], "files": wanted,
                "options": {"destination": reference["destination"], "externalId": reference["batch"]},
            })
            response.raise_for_status()
            failures = response.json().get("failures", [])
            if failures:
                raise EngineError(f"{self.name}: batch could not enqueue {len(failures)} files")
        return reference

    async def _transfers(self, client, job_ref: dict) -> list[dict]:
        if job_ref["batch"] is not None:
            response = await client.get(f"/transfers/downloads/batches/{job_ref['batch']}")
            if response.status_code == 404:
                return []
            response.raise_for_status()
            return response.json().get("transfers", [])
        transfers = []
        username = quote(job_ref["username"], safe="")
        for transfer_id in job_ref["transfers"]:
            response = await client.get(f"/transfers/downloads/{username}/{quote(transfer_id, safe='')}")
            if response.status_code != 404:
                response.raise_for_status()
                transfers.append(response.json())
        return transfers

    async def status(self, job_ref: dict) -> JobStatus:
        wanted = set(job_ref["files"])
        async with self.http(15) as client:
            transfers = await self._transfers(client, job_ref)
        if not transfers:
            return unseen("slskd holds no transfer for these files")

        states = [t.get("state", "") for t in transfers]
        done = sum(t.get("bytesTransferred", 0) for t in transfers)
        total = sum(t.get("size", 0) for t in transfers)
        if any(any(bad in s for bad in _FAILED_STATES) for s in states):
            return JobStatus(state="failed", progress=done / total if total else 0.0,
                             detail=f"transfer states: {sorted(set(states))}")
        if all(_SUCCEEDED in s for s in states) and len(transfers) == len(wanted):
            return JobStatus(state="complete", progress=1.0,
                             detail=f"{len(transfers)} files")
        return JobStatus(
            state="downloading", progress=done / total if total else 0.0,
            speed_bps=sum(t.get("averageSpeed", 0) or 0 for t in transfers) or None,
            detail=f"{sum(1 for s in states if _SUCCEEDED in s)}/{len(wanted)} files",
        )

    async def _downloads_root(self, client) -> str:
        resp = await client.get("/options")
        resp.raise_for_status()
        root = resp.json().get("directories", {}).get("downloads", "")
        if not root:
            raise EngineError(f"{self.name}: instance reports no downloads directory")
        return root.rstrip("/")

    def _landed(self, root: str, folder: str) -> str:
        return self.landing_path(f"{root}/{folder}", root)

    async def completed_path(self, job_ref: dict) -> str:
        return self.landed(job_ref["landing"])

    async def cancel(self, job_ref: dict) -> None:
        username = quote(job_ref["username"], safe="")
        async with self.http(30) as client:
            for transfer in await self._transfers(client, job_ref):
                transfer_id = quote(transfer["id"], safe="")
                response = await client.delete(
                    f"/transfers/downloads/{username}/{transfer_id}", params={"remove": "true"})
                response.raise_for_status()
        await asyncio.to_thread(_remove, Path(self.landed(job_ref["landing"])), job_ref["files"])
