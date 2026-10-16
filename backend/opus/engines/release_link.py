"""What a release's link leads to, fetched from where the search went out.

Prowlarr hands out its own address for every release it returns. On it, it
serves a .torrent itself and answers a magnet with a redirect to `magnet:`; a
usenet release — always — and a torrent from an indexer set to redirect it
sends on to the indexer's own address. That hop reaches the indexer from
whoever follows it, so with the searcher in a tunnel it is followed through the
tunnel's proxy, never from this host's own address."""

from contextlib import nullcontext
from urllib.parse import urljoin, urlparse

import httpx

from opus.engines.base import EngineError

_HOPS = 5


async def fetch(link: str, proxy: str, *, timeout: httpx.Timeout | float,
                limit: int | None = None) -> str | bytes:
    """The magnet or the file `link` leads to. A hop to the tunnel itself — the
    searcher answering at its own address — is made directly; any other goes
    through `proxy` when there is one. A proxy that does not answer fails the
    fetch rather than sending it out the other way."""
    inside = urlparse(proxy).hostname if proxy else None
    async with httpx.AsyncClient(timeout=timeout) as direct:
        async with (httpx.AsyncClient(timeout=timeout, proxy=proxy) if proxy
                    else nullcontext(direct)) as out:
            for _ in range(_HOPS):
                client = out if proxy and urlparse(link).hostname != inside else direct
                async with client.stream("GET", link) as resp:
                    if not resp.is_redirect:
                        resp.raise_for_status()
                        return await _read(resp, limit)
                link = urljoin(link, resp.headers["location"])
                if link.startswith("magnet:"):
                    return link
    raise EngineError("the release link kept redirecting")


async def _read(resp: httpx.Response, limit: int | None) -> bytes:
    body = bytearray()
    async for chunk in resp.aiter_bytes():
        body += chunk
        if limit is not None and len(body) > limit:
            raise EngineError("the release file is larger than one should be")
    return bytes(body)
