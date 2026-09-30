"""Prowlarr — indexer aggregator. It searches every configured indexer and
returns usenet + torrent releases; it never grabs. Each release it yields
carries the grab_ref of the engine that *can* grab it — SABnzbd for usenet,
qBittorrent for torrent — which is what lets /grab stay protocol-agnostic.
Swappable: adopt an existing instance (external) or run a managed one
(bundled)."""

from opus.engines.base import EngineHealth, Protocol, Release, Searcher
from opus.engines.service import ServiceEngine

# Newznab/Torznab category roots. "any" searches all three at once rather than
# dropping the filter — an unfiltered indexer query also returns software, books
# and XXX, which no consumer of OPUS has asked for.
CATEGORIES = {
    "movie": (2000,),
    "tv": (5000,),
    "music": (3000,),
    "any": (2000, 5000, 3000),
}

SEARCH_TIMEOUT = 60


class ProwlarrEngine(ServiceEngine, Searcher):
    def auth_headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.secret("api_key")}

    async def probe(self) -> EngineHealth:
        async with self.http(15) as client:
            resp = await client.get("/api/v1/health")
            resp.raise_for_status()
            # Prowlarr's health endpoint lists active warnings; an empty list is
            # a clean instance, entries are the instance's own complaints
            issues = resp.json()
        if issues:
            first = issues[0].get("message", "")
            return EngineHealth(True, f"reachable, {len(issues)} indexer warning(s): {first}")
        return EngineHealth(True, "reachable")

    async def search(self, query: str, type: str) -> list[Release]:
        async with self.http(SEARCH_TIMEOUT) as client:
            resp = await client.get("/api/v1/search", params={
                "query": query,
                "categories": list(CATEGORIES.get(type, CATEGORIES["any"])),
                "type": "search",
            })
            resp.raise_for_status()
            results = resp.json()
        return [r for r in (self._to_release(item) for item in results) if r]

    def _to_release(self, item: dict) -> Release | None:
        title = item.get("title", "")
        protocol = item.get("protocol")
        indexer = item.get("indexer", "")
        size = item.get("size") or None
        age = item.get("age")
        categories = [c["id"] for c in item.get("categories") or [] if c.get("id")]
        if protocol == "usenet":
            url = item.get("downloadUrl") or item.get("guid")
            if not url:
                return None
            return Release(
                title=title, protocol=Protocol.USENET, source="sabnzbd",
                grab_ref={"engine": "sabnzbd", "nzb_url": url, "title": title},
                size=size, age_days=age, indexer=indexer, categories=categories,
                guid=item.get("guid") or "",
            )
        if protocol == "torrent":
            magnet = item.get("magnetUrl")
            url = item.get("downloadUrl") or item.get("guid")
            if not magnet and not url:
                return None
            return Release(
                title=title, protocol=Protocol.TORRENT, source="qbittorrent",
                grab_ref={"engine": "qbittorrent", "magnet": magnet,
                          "torrent_url": url, "title": title},
                size=size, seeders=item.get("seeders"), age_days=age, indexer=indexer,
                categories=categories, guid=item.get("guid") or "",
            )
        return None
