import pytest

from opus import containers, docker_controller
from opus.config import settings
from opus.containers import OWNER_LABEL


def _engine_spec(name="sabnzbd"):
    spec = docker_controller.SPEC_BY_NAME[name]
    return {
        "Image": spec.image,
        "Env": [
            f"PUID={settings.engine_uid}",
            f"PGID={settings.engine_gid}",
            f"TZ={settings.timezone}",
        ],
        "Labels": {OWNER_LABEL: "engine", f"{OWNER_LABEL}.name": name},
        "HostConfig": {
            "Binds": [
                f"{settings.engines_host_dir}/{name}:{spec.config_mount}",
                f"{settings.landing_host_dir}:/landing",
            ],
            "RestartPolicy": {"Name": "unless-stopped"},
            "Memory": settings.engine_memory_limit_bytes,
            "PidsLimit": settings.engine_pids_limit,
        },
        "NetworkingConfig": {"EndpointsConfig": {settings.engine_network: {}}},
    }


def test_controller_allows_only_the_catalogued_engine_contract():
    body = _engine_spec()
    assert docker_controller._allowed("POST", "containers/create", {"name": "opus_sabnzbd"}, body)
    assert docker_controller._allowed("POST", "images/create",
                                      {"fromImage": body["Image"]}, None)
    assert docker_controller._allowed("GET", "images/lscr.io/linuxserver/sabnzbd:latest/json", {}, None)
    assert docker_controller._allowed("POST", "containers/opus_sabnzbd/start", {}, None)


def test_controller_refuses_host_escape_fields_and_unknown_containers():
    body = _engine_spec()
    body["HostConfig"]["Privileged"] = True
    assert not docker_controller._allowed("POST", "containers/create", {"name": "opus_sabnzbd"}, body)
    assert not docker_controller._allowed("POST", "containers/create", {"name": "arbitrary"}, _engine_spec())
    assert not docker_controller._allowed("DELETE", "containers/opus_library", {}, None)


def test_controller_refuses_a_bundled_engine_without_its_resource_limits():
    body = _engine_spec()
    body["HostConfig"]["Memory"] = settings.engine_memory_limit_bytes + 1
    assert not docker_controller._allowed("POST", "containers/create", {"name": "opus_sabnzbd"}, body)


def test_controller_accepts_only_the_vpn_tunnel_resource_profile():
    spec = docker_controller.SPEC_BY_NAME["qbittorrent"]
    body = containers.vpn_spec(spec, {})
    assert docker_controller._allowed("POST", "containers/create", {"name": "opus_vpn_qbittorrent"}, body)
    body["HostConfig"]["PidsLimit"] = settings.vpn_pids_limit + 1
    assert not docker_controller._allowed("POST", "containers/create", {"name": "opus_vpn_qbittorrent"}, body)


@pytest.mark.parametrize("use_vpn", [False, True])
def test_controller_accepts_the_solver_only_beside_the_engine_that_has_one(use_vpn):
    body = containers.solver_spec(docker_controller.SPEC_BY_NAME["prowlarr"], use_vpn)
    create = ("POST", "containers/create")
    assert docker_controller._allowed(*create, {"name": "opus_solver_prowlarr"}, body)
    assert docker_controller._allowed("POST", "images/create", {"fromImage": body["Image"]}, None)
    assert not docker_controller._allowed(*create, {"name": "opus_solver_sabnzbd"}, body)
    assert not docker_controller._allowed(*create, {"name": "opus_prowlarr"}, body)
    wider = {**body, "HostConfig": {**body["HostConfig"], "Tmpfs": {"/config": "", "/etc": ""}}}
    assert not docker_controller._allowed(*create, {"name": "opus_solver_prowlarr"}, wider)
    body["HostConfig"]["Binds"] = ["/:/host"]
    assert not docker_controller._allowed(*create, {"name": "opus_solver_prowlarr"}, body)


def test_controller_rejects_a_share_bind_that_escapes_media_with_traversal(monkeypatch):
    monkeypatch.setattr(settings, "media_host_dir", "/mnt/media")
    body = _engine_spec("slskd")
    body["HostConfig"]["Binds"].append(f"{settings.media_host_dir}/../../etc:/music:ro")
    assert not docker_controller._allowed("POST", "containers/create", {"name": "opus_slskd"}, body)
    body["HostConfig"]["Binds"][-1] = f"{settings.media_host_dir}/music:/music:ro"
    assert docker_controller._allowed("POST", "containers/create", {"name": "opus_slskd"}, body)
