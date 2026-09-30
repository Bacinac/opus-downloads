import json

from opus_core import revision


async def test_the_admin_is_told_which_build_runs(api, monkeypatch, tmp_path):
    stamp = tmp_path / "revision.json"
    stamp.write_text(json.dumps({"version": "0.1.221", "sha": "4082d30a"}))
    monkeypatch.setattr(revision, "STAMP", stamp)
    answer = await api.get("/api/version")
    assert answer.status_code == 200
    assert answer.json() == {"product": "opus-downloads", "version": "0.1.221", "sha": "4082d30a"}
