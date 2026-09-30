"""slskd — Soulseek search and grab in one engine. Unlike the indexer-mediated
pair it is its own index: a search asks the network and every response is a
candidate folder held by one peer, which is also the unit that gets grabbed.

OPUS hands back whole folders and nothing else. Picking which of a peer's files
belong to the release is domain logic (which tracks, which edition) and stays in
the consuming app; here a folder is a release and its file list rides along in
the grab_ref.

slskd has no notion of categories and never reports where it wrote: it recreates
the peer's folder, by name, directly under its one downloads directory. So the
namespace isolates nothing here, and two folders that share a name share a
place on disk — which is why a grab is refused while another folder of that
name is still arriving or still lying there."""

import asyncio
import posixpath
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


def _holds_files(folder: Path) -> bool:
    return folder.is_dir() and any(folder.iterdir())


def _remove(folder: Path, names: list[str]) -> None:
    for name in names:
        (folder / name).unlink(missing_ok=True)
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()


class SlskdEngine(ServiceEngine, Searcher, Grabber):
    api_path = "/api/v0"

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
        username = grab_ref["username"]
        files = grab_ref["files"]
        if not files:
            raise EngineError(f"{self.name}: grab_ref carries no files")
        # only what slskd's API takes: the grab_ref also carries per-file bitrate
        # for the caller's benefit, and the peer has no use for it
        wanted = [{"filename": f["filename"], "size": f.get("size", 0)} for f in files]
        async with self.http(30) as client:
            await self._refuse_shared_folder(client, _folder(grab_ref["directory"]))
            resp = await client.post(f"/transfers/downloads/{username}", json=wanted)
            resp.raise_for_status()
        return {
            "username": username,
            "directory": grab_ref["directory"],
            "files": [f["filename"] for f in files],
        }

    async def _refuse_shared_folder(self, client, folder: str) -> None:
        resp = await client.get("/transfers/downloads")
        resp.raise_for_status()
        for user in resp.json():
            for directory in user.get("directories", []):
                if _folder(directory.get("directory", "")) != folder:
                    continue
                if any(not f.get("state", "").startswith("Completed")
                       for f in directory.get("files", [])):
                    raise EngineError(
                        f"{self.name}: a folder named {folder!r} is already arriving from "
                        f"{user.get('username')}, and this one would land in the same place"
                    )
        landed = Path(self._landed(await self._downloads_root(client), folder))
        if await asyncio.to_thread(_holds_files, landed):
            raise EngineError(
                f"{self.name}: {landed} still holds another download's files, and this "
                "one would land among them"
            )

    async def _transfers(self, client, username: str, wanted: set[str]) -> list[dict]:
        resp = await client.get(f"/transfers/downloads/{username}")
        resp.raise_for_status()
        return [
            file
            for directory in resp.json().get("directories", [])
            for file in directory.get("files", [])
            if file.get("filename") in wanted
        ]

    async def status(self, job_ref: dict) -> JobStatus:
        wanted = set(job_ref["files"])
        async with self.http(15) as client:
            transfers = await self._transfers(client, job_ref["username"], wanted)
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
        async with self.http(15) as client:
            root = await self._downloads_root(client)
        return self._landed(root, _folder(job_ref["directory"]))

    async def cancel(self, job_ref: dict) -> None:
        """slskd forgets a transfer when told to remove it and leaves its file
        where it landed — exactly where the next folder of that name would
        write. So the files this job names are deleted from the landing zone
        too, and the folder with them once nothing else is in it."""
        wanted = set(job_ref["files"])
        username = job_ref["username"]
        async with self.http(30) as client:
            for transfer in await self._transfers(client, username, wanted):
                if not transfer.get("id"):
                    continue
                resp = await client.delete(
                    f"/transfers/downloads/{username}/{transfer['id']}",
                    params={"remove": "true"},
                )
                resp.raise_for_status()
            root = await self._downloads_root(client)
        await asyncio.to_thread(
            _remove, Path(self._landed(root, _folder(job_ref["directory"]))),
            [posixpath.basename(_normalize(f)) for f in wanted],
        )
