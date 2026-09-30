"""yt-dlp — direct-source web video: no indexer, no catalog, no account. A grab
takes a URL and the download runs inside this backend.

It is the only engine with nothing to search. That is not a gap: a URL is
already the answer to the question a search would ask, so it is not a Searcher
at all, and `/grab` takes `{"engine": "ytdlp", "url": ...}` directly.

yt-dlp is a blocking library, so the download runs in a worker thread and its
progress hook is what reports back — and what notices a cancellation, since a
thread cannot be interrupted from outside."""

import asyncio
import logging
from pathlib import Path

import yt_dlp

from opus.engines.base import EngineError, EngineHealth
from opus.engines.inprocess import InProcessEngine
from opus.runner import Download, Progress

log = logging.getLogger(__name__)


class YtdlpEngine(InProcessEngine):
    async def probe(self) -> EngineHealth:
        return EngineHealth(True, f"yt-dlp {yt_dlp.version.__version__}")

    async def _download(self, grab_ref: dict) -> tuple[str, Download]:
        url = grab_ref.get("url")
        if not url:
            raise EngineError(f"{self.name}: grab_ref carries no url")
        if not url.startswith(("http://", "https://")):
            raise EngineError(f"{self.name}: {url!r} is not an http(s) url")
        wanted = _options(grab_ref)

        async def download(progress: Progress, workdir: Path) -> str:
            return await asyncio.to_thread(_fetch, url, workdir, progress, wanted)

        return grab_ref.get("title") or _folder_for(url), download


def _folder_for(url: str) -> str:
    """A grab without a title still needs somewhere to land; the URL's own tail
    is more use than a random id when looking at the landing zone."""
    return url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "download"


def _options(grab_ref: dict) -> dict:
    """The choices the caller is allowed to make about the fetch.

    Named one by one rather than passed through: a consumer that could hand
    yt-dlp arbitrary options would be reaching around OPUS instead of through
    it, and the surface would be whatever yt-dlp happened to accept that week.
    Each of these is a policy the caller owns and OPUS cannot invent — which
    quality to take, which subtitle languages are wanted, whether a machine
    translation counts, whether sponsor segments are cut.

    A caller that names none of them gets yt-dlp's own best-effort choice."""
    options: dict = {}
    if fmt := grab_ref.get("format"):
        options["format"] = fmt

    languages = grab_ref.get("subtitles") or []
    if languages:
        options["writesubtitles"] = True
        options["subtitleslangs"] = list(languages)
        if grab_ref.get("auto_subtitles"):
            # a machine transcript where no published subtitle exists; the
            # caller decides whether that is acceptable evidence
            options["writeautomaticsub"] = True
        options["postprocessors"] = [
            {"key": "FFmpegSubtitlesConvertor", "format": "srt"}
        ]

    if grab_ref.get("sponsorblock"):
        options["postprocessors"] = [
            {"key": "SponsorBlock", "categories": ["sponsor"]},
            {"key": "ModifyChapters", "remove_sponsor_segments": ["sponsor"]},
            *options.get("postprocessors", []),
        ]
    return options


def _fetch(url: str, workdir: Path, progress: Progress, wanted: dict) -> str:
    """Run yt-dlp to completion and return the folder it filled.

    The folder, not the file, even for a single video: every other engine hands
    back the directory its download landed in, and a caller that has to branch
    on which engine fetched something has lost the point of one API."""
    options = {
        # the source's own id rides in the name: a title can repeat or be
        # rewritten upstream, and the id is what identifies the video later
        "outtmpl": str(workdir / "%(title)s [%(id)s].%(ext)s"),
        # let yt-dlp pick the best it can merge; the merge is why ffmpeg is in
        # the image, and mkv accepts every codec combination it might choose
        "merge_output_format": "mkv",
        "progress_hooks": [_hook(progress)],
        "postprocessor_hooks": [_post_hook(progress)],
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        # a failure inside a playlist must not be swallowed into a partial
        # result that looks complete
        "ignoreerrors": False,
        **wanted,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    if not _landed_files(info):
        raise EngineError("yt-dlp reported no downloaded file")
    return str(workdir)


def _landed_files(info: dict) -> list[str]:
    entries = info.get("entries") if isinstance(info, dict) else None
    items = list(entries) if entries else [info]
    paths = []
    for item in items:
        if not item:
            continue
        for download in item.get("requested_downloads") or []:
            if path := download.get("filepath"):
                paths.append(path)
    return paths


def _hook(progress: Progress):
    def hook(status: dict) -> None:
        # raises out of the download when cancelled — the one moment yt-dlp is
        # inside our code and can be stopped
        if status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            done = status.get("downloaded_bytes") or 0
            progress.report(
                fraction=done / total if total else None,
                speed_bps=status.get("speed"),
                eta_seconds=status.get("eta"),
                detail=Path(status.get("filename", "")).name,
            )
        else:
            progress.report(detail=status.get("status", ""))

    return hook


def _post_hook(progress: Progress):
    def hook(status: dict) -> None:
        # merging a large video takes long enough that silence here reads as a
        # stall; it is also a cancellation checkpoint
        progress.report(detail=f"{status.get('postprocessor', 'processing')}…")

    return hook
