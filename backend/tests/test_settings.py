import pytest

from opus import provision
from opus.config import settings
from opus.engines.base import EngineMode
from opus.engines.registry import build_engine
from opus.settings_store import (
    SettingsValidationError,
    current_runtime,
    get_for_ui,
    store_credentials,
    update_settings,
)


@pytest.fixture
def applied(monkeypatch):
    saves = []
    monkeypatch.setattr(provision, "apply_saved", saves.append)
    return saves


@pytest.mark.parametrize(("key", "value", "code"), [
    ("ytdlp_landing_dir", "web", "bad_path"),
    ("ytdlp_landing_dir", "/etc", "outside_landing"),
    ("ytdlp_landing_dir", "{root}/../etc", "bad_path"),
    ("sabnzbd_landing_dir", "/", "outside_landing"),
    ("slskd_share_dir", "/mnt/media/../../etc", "bad_path"),
    ("slskd_share_dir", "/", "outside_media"),
    ("slskd_share_dir", "/srv/music", "outside_media"),
    ("content_folders", "movies,Series", "bad_name"),
    ("content_folders", "movies,../x", "bad_name"),
    ("prowlarr_url", "ftp://x", "bad_url"),
    ("prowlarr_mode", "sideways", "bad_value"),
    ("nonesuch", "x", "unknown_key"),
    ("access_library_token", "", "not_editable"),
    ("sabnzbd_bundled_api_key", "x", "not_editable"),
    ("sabnzbd_usenet_port", "0", "bad_port"),
    ("sabnzbd_usenet_port", "65536", "bad_port"),
    ("sabnzbd_usenet_port", "５６３", "bad_port"),
    ("sabnzbd_usenet_connections", "", "bad_connections"),
    ("sabnzbd_usenet_connections", "501", "bad_connections"),
    ("sabnzbd_usenet_ssl", "yes", "bad_value"),
])
async def test_what_a_save_refuses(monkeypatch, key, value, code):
    monkeypatch.setattr(settings, "media_host_dir", "/mnt/media")
    await store_credentials({"access_library_token": "kept"})
    with pytest.raises(SettingsValidationError) as refused:
        await update_settings({"content_folders": "music",
                               key: value.format(root=settings.landing_root)})
    assert (refused.value.key, refused.value.code) == (key, code)
    runtime = await current_runtime()
    assert runtime.get("access_library_token") == "kept"
    assert runtime.get("content_folders") != "music"


@pytest.mark.parametrize(("key", "value"), [
    ("ytdlp_landing_dir", "{root}/web"),
    ("ytdlp_landing_dir", "{root}"),
    ("slskd_share_dir", "/mnt/media/music"),
    ("slskd_share_dir", ""),
    ("content_folders", "movies, music,web-video"),
    ("sabnzbd_usenet_port", "119"),
    ("sabnzbd_usenet_connections", "500"),
    ("sabnzbd_usenet_ssl", "false"),
])
async def test_what_a_save_accepts(monkeypatch, key, value):
    monkeypatch.setattr(settings, "media_host_dir", "/mnt/media")
    value = value.format(root=settings.landing_root)
    await update_settings({key: value})
    assert (await current_runtime()).get(key) == value


async def test_no_engine_runs_bundled_without_an_origin_for_its_interface(monkeypatch):
    monkeypatch.setattr(settings, "engines_url", "")
    with pytest.raises(SettingsValidationError) as refused:
        await update_settings({"slskd_mode": "bundled"})
    assert refused.value.code == "no_engines_url"
    await update_settings({"slskd_mode": "external"})


async def test_the_engines_own_configuration_is_never_shared(monkeypatch):
    monkeypatch.setattr(settings, "media_host_dir", "/mnt/docker")
    monkeypatch.setattr(settings, "engines_host_dir", "/mnt/docker/opus-downloads/volumes/engines")
    with pytest.raises(SettingsValidationError):
        await update_settings({"slskd_share_dir": "/mnt/docker/opus-downloads"})


async def test_a_save_answers_what_each_changed_key_held_before():
    await update_settings({"slskd_soulseek_username": "filip", "slskd_soulseek_password": "one"})
    previous = await update_settings({"slskd_soulseek_username": "filip",
                                      "slskd_soulseek_password": "two",
                                      "sabnzbd_usenet_password": ""})
    assert previous == {"slskd_soulseek_password": "one"}
    assert (await current_runtime()).get("slskd_soulseek_password") == "two"


async def test_the_settings_route_applies_what_changed(api, applied):
    resp = await api.put("/api/settings", json={"slskd_soulseek_password": "s3cret"})
    assert resp.status_code == 200
    assert applied == [{"slskd_soulseek_password": ""}]
    shown = {s["key"]: s for s in resp.json()}
    assert shown["slskd_soulseek_password"] == {**shown["slskd_soulseek_password"],
                                               "value": "", "is_set": True}
    assert "access_library_token" not in shown


async def test_the_settings_route_refuses_a_machine_key(api, applied, token):
    resp = await api.put("/api/settings", json={"access_library_token": ""})
    assert resp.status_code == 400
    assert resp.json()["detail"] == {"key": "access_library_token", "code": "not_editable"}
    assert applied == []
    assert (await current_runtime()).get("access_library_token") == token


async def test_a_bundled_engine_is_enabled_by_its_own_credentials_alone():
    await update_settings({"sabnzbd_mode": "bundled", "sabnzbd_use_vpn": "false",
                           "sabnzbd_usenet_password": "news"})
    await store_credentials({"sabnzbd_bundled_api_key": "learned"})
    sabnzbd = build_engine(await current_runtime(), "sabnzbd")
    assert sabnzbd.mode is EngineMode.BUNDLED and sabnzbd.enabled
    assert not [s for s in await get_for_ui() if s["group"] == "access"]

