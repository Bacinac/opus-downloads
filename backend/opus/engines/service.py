"""The engines OPUS reaches over HTTP — a sidecar it runs or an instance it
adopted. Each adapter speaks its own dialect; how the connection is opened, how
a failure comes back out and how a path in the engine's filesystem becomes one
in OPUS's are the same for all of them, and live here."""

import json
import posixpath
from contextlib import asynccontextmanager
from pathlib import PurePosixPath

import httpx

from opus.engines.base import Engine, EngineError, EngineMode


@asynccontextmanager
async def service_client(engine: str, base_url: str, *, headers: dict | None = None,
                         timeout: float = 30):
    """Every transport, status or parse failure comes back out as a single
    EngineError carrying the request that failed, so a broken engine is
    reported as broken instead of quietly returning nothing."""
    if not base_url:
        raise EngineError(f"{engine}: url is not configured")
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), headers=headers or {}, timeout=timeout
    ) as client:
        try:
            yield client
        except EngineError:
            raise
        except httpx.HTTPStatusError as exc:
            raise EngineError(
                f"{engine}: {exc.request.method} {exc.request.url.path} "
                f"→ HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise EngineError(f"{engine}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise EngineError(f"{engine}: answered with something that is not JSON ({exc})") from exc


class ServiceEngine(Engine):
    # where the engine's API answers, under its address
    api_path = ""

    def auth_headers(self) -> dict[str, str]:
        return {}

    def http(self, timeout: float):
        return service_client(
            self.name, f"{self.config.url.rstrip('/')}{self.api_path}" if self.config.url else "",
            headers=self.auth_headers(), timeout=timeout,
        )

    def landing_path(self, remote_path: str, remote_root: str) -> str:
        """Translate a path the worker reports in its own filesystem view into
        OPUS's view of the same file.

        An adopted engine sees a shared directory under a mount point of its
        own: SABnzbd calls it /downloads/complete, OPUS sees it as landing_dir,
        and the worker's own root — read from its API, never guessed — is what
        the rebase is relative to. A bundled engine has no mount point of its
        own to rebase from: OPUS started it on the identical landing bind, so
        its reported path already IS the OPUS-side path, verbatim, subfolder
        and all — rebasing onto landing_dir (the tree's top) would instead
        throw that subfolder away for any bundled engine that, like slskd,
        organizes its own downloads under one. Either way, a completed path
        outside the worker's declared root is an error, not a silently wrong
        move later on."""
        path = PurePosixPath(posixpath.normpath(remote_path))
        root = PurePosixPath(posixpath.normpath(remote_root))
        if not remote_path.startswith("/") or path == root or not path.is_relative_to(root):
            raise EngineError(
                f"{self.name}: completed path {remote_path!r} is not inside the engine's "
                f"own download root {remote_root!r}; cannot map it into the landing zone"
            )
        if self.mode is EngineMode.BUNDLED:
            return self.landed(str(path))
        return self.landed(str(PurePosixPath(self.landing_dir()) / path.relative_to(root)))
