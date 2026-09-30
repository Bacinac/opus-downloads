"""SABnzbd — usenet grab/track. It searches nothing (Prowlarr feeds it
releases); it adds the chosen NZB, tracks it by nzo_id and reveals the completed
folder in its history. Swappable: external adopts the existing instance, bundled
runs one.

The namespace becomes a SABnzbd category whose folder is the namespace itself,
so each consuming app's downloads stay in their own subtree of the landing zone.
The category is created if missing — a grab into a category SABnzbd does not
know would silently land in Default, i.e. in another app's folder."""

import asyncio
import posixpath
import re
from xml.etree import ElementTree

import httpx

from opus.engines.base import (
    ContentFile,
    Contents,
    EngineError,
    EngineHealth,
    Grabber,
    JobStatus,
    unseen,
)
from opus.engines.service import ServiceEngine
from opus.redaction import redacted


# SABnzbd writes its shipped categories on its first save, which can land after
# the first reconciliation pass — so the result is verified rather than assumed.
_RECONCILE_ATTEMPTS = 4

# An NZB names each file only inside its segments' subject line, in the shape
#   [002/106] - "Name.part01.rar" yEnc (1/220)
_QUOTED = re.compile(r'"([^"]+)"')
# posters who quote nothing — or forget the opening quote, which is common —
# put the name last, right before the yEnc marker, spaces and all
_TRAILING = re.compile(r"[\w\-+()'\[\]. ]+\.[A-Za-z0-9]{1,4}\s*$")

# a manifest is a few hundred KB; anything larger is not one, and a server that
# drips bytes forever would hold the request open — httpx's timeout is per
# operation, so the read needs its own overall budget as well
_NZB_BUDGET_SECONDS = 25
_NZB_MAX_BYTES = 8 * 1024 * 1024


def _filename(subject: str) -> str:
    """The filename out of a segment subject. Quoted when the poster quoted it,
    and otherwise the trailing name — both shapes occur in the wild."""
    named = next((q for q in _QUOTED.findall(subject) if posixpath.splitext(q)[1]), None)
    if named:
        return named.strip()
    head = subject.split(" yEnc", 1)[0].strip().rstrip('"')
    match = _TRAILING.search(head)
    return match.group(0).strip(" -[]") if match else ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _nzb_files(body: bytes) -> list[ContentFile]:
    """The files an NZB declares, with the byte count its segments add up to.

    A post that seals its tracks inside archive volumes declares those volumes
    and nothing else. That is a true answer about what the post contains, and
    what to make of it — packed, obfuscated, acceptable — belongs to the caller."""
    root = ElementTree.fromstring(body)
    files = []
    for element in root.iter():
        if _local(element.tag) != "file":
            continue
        name = _filename(element.get("subject", ""))
        if not name:
            continue
        size = sum(int(seg.get("bytes") or 0) for seg in element.iter()
                   if _local(seg.tag) == "segment")
        files.append(ContentFile(name=name, size=size))
    return files


async def _fetch_nzb(url: str) -> bytes:
    limits = httpx.Timeout(connect=5, read=10, write=10, pool=5)
    async with httpx.AsyncClient(timeout=limits, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > _NZB_MAX_BYTES:
                    raise EngineError("the manifest is larger than a manifest should be")
            return bytes(body)


def _eta_seconds(timeleft: str) -> int | None:
    """SABnzbd reports the remaining time as H:MM:SS."""
    parts = timeleft.split(":")
    if len(parts) != 3 or not all(p.strip().isdigit() for p in parts):
        return None
    hours, minutes, seconds = (int(p) for p in parts)
    return hours * 3600 + minutes * 60 + seconds


class SabnzbdEngine(ServiceEngine, Grabber):
    async def _call(self, client, **params) -> dict:
        resp = await client.get("/api", params={
            "apikey": self.secret("api_key"), "output": "json", **params,
        })
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") is False:
            raise EngineError(f"{self.name}: {params.get('mode')} rejected: "
                              f"{data.get('error', 'unknown error')}")
        return data

    async def probe(self) -> EngineHealth:
        async with self.http(15) as client:
            version = (await self._call(client, mode="version")).get("version", "?")
        return EngineHealth(True, f"SABnzbd {version}")

    async def _ensure_category(self, client, namespace: str) -> None:
        """Make the category exist AND sort into its own folder.

        Existing by name is not enough: SABnzbd ships categories of its own
        (movies, tv, audio…) with no folder set, so a name that merely exists
        can still drop its downloads straight into the completed root, mixed in
        with everything else."""
        if (await self._categories(client)).get(namespace) == namespace:
            return
        await self._call(client, mode="set_config", section="categories",
                         keyword=namespace, dir=namespace)

    async def _categories(self, client) -> dict[str, str]:
        config = await self._call(client, mode="get_config", section="categories")
        # a SABnzbd that has never had a category omits the key entirely rather
        # than returning an empty list
        return {c.get("name"): c.get("dir", "")
                for c in config.get("config", {}).get("categories", [])}

    async def reconcile_namespaces(self, folders: list[str]) -> None:
        """Make the categories exactly the declared folders.

        SABnzbd ships its own — movies, tv, audio, software — and on an instance
        OPUS runs they are noise. It also writes them the first time it saves
        its configuration, which is *after* the first pass here, so its defaults
        can land on top of what was just set. The result is therefore read back
        and the pass repeated until it holds, rather than run twice and hoped
        for. `*` is left alone: it is not a folder but SABnzbd's entry for a
        download that arrived without a category."""
        wanted = {f: f for f in folders}
        async with self.http(30) as client:
            for _ in range(_RECONCILE_ATTEMPTS):
                current = await self._categories(client)
                stale = [n for n in current if n not in wanted and n != "*"]
                wrong = [f for f in folders if current.get(f) != f]
                if not stale and not wrong:
                    return
                for folder in wrong:
                    await self._call(client, mode="set_config", section="categories",
                                     keyword=folder, dir=folder)
                for name in stale:
                    await self._call(client, mode="del_config", section="categories",
                                     keyword=name)
        raise EngineError(
            f"{self.name}: categories would not settle to {folders} after "
            f"{_RECONCILE_ATTEMPTS} passes"
        )

    async def inspect(self, grab_ref: dict) -> Contents:
        """Read the NZB the release points at and report the filenames it
        declares. The NZB is a manifest and it is small, so this costs one
        fetch and tells the caller what the title only claims."""
        url = grab_ref.get("nzb_url")
        if not url:
            return Contents(available=False, detail="the release carries no NZB url")
        try:
            body = await asyncio.wait_for(_fetch_nzb(url), _NZB_BUDGET_SECONDS)
        except Exception as exc:
            # not being able to look is not the same as looking and seeing
            # nothing; the caller decides whether to grab blind
            return Contents(available=False,
                            detail=redacted(f"the NZB at {url} could not be read: {exc}"))
        try:
            files = _nzb_files(body)
        except ElementTree.ParseError as exc:
            return Contents(available=False, detail=f"the NZB is not valid XML: {exc}")
        return Contents(available=True, files=files,
                        detail=f"{len(files)} files declared")

    async def grab(self, grab_ref: dict, namespace: str) -> dict:
        title = grab_ref.get("title", "")
        async with self.http(60) as client:
            await self._ensure_category(client, namespace)
            data = await self._call(client, mode="addurl", name=grab_ref["nzb_url"],
                                    nzbname=title, cat=namespace)
            nzo_ids = data.get("nzo_ids") or []
            if not nzo_ids:
                raise EngineError(f"{self.name}: NZB accepted but no job id returned")
        return {"nzo_id": nzo_ids[0], "title": title, "category": namespace}

    async def _history_slot(self, client, nzo_id: str) -> dict | None:
        """The one history entry for this job, wherever SABnzbd is keeping it.

        A finished job does not stay in the history view: SABnzbd moves it to the
        archive, which that view does not include. Measured against the live
        instance: 85 entries in the history and 2717 in the archive, and the
        history's newest are failures. So a scan of the first N entries finds a
        completed job only in the moments before it is archived, and reports it
        as still queued for ever after — a job that silently stops progressing,
        which is worse than one that fails.

        Both lookups ask for the single id rather than paging, so neither the
        archive's size nor a limit can hide the answer."""
        for archive in (0, 1):
            history = (await self._call(
                client, mode="history", nzo_ids=nzo_id, archive=archive
            )).get("history", {})
            for slot in history.get("slots", []):
                if slot.get("nzo_id") == nzo_id:
                    return slot
        return None

    async def status(self, job_ref: dict) -> JobStatus:
        nzo_id = job_ref["nzo_id"]
        async with self.http(15) as client:
            queue = (await self._call(client, mode="queue")).get("queue", {})
            for slot in queue.get("slots", []):
                if slot.get("nzo_id") != nzo_id:
                    continue
                progress = float(slot.get("percentage", 0)) / 100
                return JobStatus(
                    state="downloading", progress=progress,
                    speed_bps=int(float(queue.get("kbpersec", 0)) * 1024),
                    eta_seconds=_eta_seconds(slot.get("timeleft", "")),
                    detail=f"{slot.get('status', '')} — {slot.get('mbleft', '?')} MB left "
                           f"of {slot.get('mb', '?')} MB",
                )
            slot = await self._history_slot(client, nzo_id)
        if slot is None:
            return unseen("not in SABnzbd's queue or history")

        status = slot.get("status", "")
        if status == "Completed":
            return JobStatus(state="complete", progress=1.0, detail=slot.get("storage", ""))
        if status == "Failed":
            return JobStatus(state="failed",
                             detail=slot.get("fail_message") or "download failed")
        # in history but still post-processing (Repairing, Verifying,
        # Extracting, Moving, …) — not done yet, and not a failure
        return JobStatus(state="downloading", progress=0.99,
                         detail=status or "post-processing")

    async def _history_storage(self, client, nzo_id: str) -> str:
        slot = await self._history_slot(client, nzo_id)
        if slot is None:
            raise EngineError(f"{self.name}: job {nzo_id} is not in SABnzbd's history")
        storage = slot.get("storage", "")
        if not storage:
            raise EngineError(f"{self.name}: job {nzo_id} has no completed folder")
        return storage

    async def completed_path(self, job_ref: dict) -> str:
        async with self.http(15) as client:
            storage = await self._history_storage(client, job_ref["nzo_id"])
            misc = (await self._call(client, mode="get_config", section="misc"))["config"]["misc"]
            complete_dir = misc.get("complete_dir", "")
        if not complete_dir.startswith("/"):
            raise EngineError(
                f"{self.name}: complete_dir {complete_dir!r} is relative to SABnzbd's own "
                "working directory; OPUS cannot map it into the landing zone"
            )
        return self.landing_path(storage, complete_dir)

    async def cancel(self, job_ref: dict) -> None:
        """A job in flight lives in the queue and a finished one in the history,
        and SABnzbd rejects a delete aimed at the side the job is not on. So
        both are tried and one success is enough; failing on both is the real
        error, because then nothing was removed."""
        nzo_id = job_ref["nzo_id"]
        async with self.http(30) as client:
            reasons = []
            for mode in ("queue", "history"):
                try:
                    await self._call(client, mode=mode, name="delete", value=nzo_id,
                                     del_files=1)
                    return
                except EngineError as exc:
                    reasons.append(str(exc))
        raise EngineError(f"{self.name}: {nzo_id} could not be removed: {'; '.join(reasons)}")
