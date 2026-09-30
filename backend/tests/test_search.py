import pytest

from opus.api.shared import release_to_dict
from opus.engines import prowlarr, slskd
from opus.engines.base import EngineConfig, Protocol, Release
from opus.engines.catalog import SPEC_BY_NAME
from opus.settings_store import store_credentials


@pytest.mark.parametrize(("engines", "said"), [
    (["sabnzbd"], "cannot search"),
    (["ytdlp"], "cannot search"),
    (["prowlarr"], "not configured"),
    (["nonesuch"], "unknown engine: nonesuch"),
])
async def test_a_search_naming_an_engine_that_cannot_answer_is_refused(api, engines, said):
    resp = await api.post("/api/search", json={"query": "x", "engines": engines})
    assert resp.status_code == 400 and said in resp.json()["detail"]


async def test_a_chosen_engine_that_breaks_is_named_beside_the_other_answers(api, monkeypatch):
    await store_credentials({"prowlarr_mode": "external", "prowlarr_api_key": "a",
                             "slskd_mode": "external", "slskd_api_key": "b"})

    async def broken(self, query, type):
        raise KeyError("results")

    async def answering(self, query, type):
        return [Release(title="Available", protocol=Protocol.SOULSEEK, source="slskd",
                        grab_ref={"engine": "slskd", "id": "a"})]
    monkeypatch.setattr(prowlarr.ProwlarrEngine, "search", broken)
    monkeypatch.setattr(slskd.SlskdEngine, "search", answering)
    resp = await api.post("/api/search", json={"query": "x", "type": "music",
                                               "engines": ["slskd", "prowlarr"]})
    assert resp.status_code == 200
    assert [release["title"] for release in resp.json()["releases"]] == ["Available"]
    assert resp.json()["errors"] == [{"engine": "prowlarr", "detail": "search failed: 'results'"}]
    resp = await api.post("/api/search", json={"query": "x", "type": "music", "engines": ["slskd"]})
    assert resp.status_code == 200
    assert [release["title"] for release in resp.json()["releases"]] == ["Available"]
    assert resp.json()["errors"] == []


async def test_an_install_with_no_searcher_answers_an_empty_list(api):
    resp = await api.post("/api/search", json={"query": "x"})
    assert resp.status_code == 200 and resp.json() == {"releases": [], "errors": []}


@pytest.mark.parametrize(("grab_ref", "said"), [
    ({"engine": "nonesuch"}, "unknown engine"),
    ({}, "does not name an engine"),
    ({"engine": "prowlarr"}, "not configured"),
])
async def test_an_inspect_that_cannot_be_asked_is_refused(api, grab_ref, said):
    resp = await api.post("/api/inspect", json={"grab_ref": grab_ref})
    assert resp.status_code == 400 and said in resp.json()["detail"]


async def test_an_engine_that_cannot_look_inside_says_so(api):
    resp = await api.post("/api/inspect", json={"grab_ref": {"engine": "ytdlp", "url": "https://x"}})
    assert resp.json() == {"available": False, "detail": "ytdlp cannot inspect a release",
                           "files": []}


@pytest.mark.parametrize("protocol", ["usenet", "torrent"])
def test_an_indexer_result_names_its_post(protocol):
    engine = prowlarr.ProwlarrEngine(SPEC_BY_NAME["prowlarr"], EngineConfig(
        name="prowlarr", mode="external", enabled_flag=True, url=""), None)
    said = release_to_dict(engine._to_release({
        "title": "Film.2020.1080p", "protocol": protocol, "indexer": "geek",
        "downloadUrl": "http://prowlarr/7/download?link=changes-every-search",
        "guid": "https://nzbgeek.info/geekseek.php?guid=abc123"}))
    assert said["guid"] == "https://nzbgeek.info/geekseek.php?guid=abc123"
