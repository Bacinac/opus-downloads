import json

import httpx
import pytest

from opus import containers
from opus.containers import OWNER_LABEL, SPEC_LABEL, ContainerError, Docker, ensure, stamped

SPEC = {"Image": "lscr.io/linuxserver/sabnzbd:latest", "Labels": {OWNER_LABEL: "engine"}}


class Recorded:
    """A daemon holding one container of OPUS's, built to an older definition."""

    def __init__(self, pull_fails=False):
        self.calls = []
        self.pull_fails = pull_fails

    async def inspect(self, name):
        return {"Config": {"Labels": {OWNER_LABEL: "engine", SPEC_LABEL: "old"}}}

    async def pull(self, image):
        self.calls.append("pull")
        if self.pull_fails:
            raise ContainerError(f"could not pull {image}: registry unreachable")

    def __getattr__(self, action):
        async def record(*args):
            self.calls.append(action)
        return record


async def test_a_changed_definition_pulls_before_it_touches_the_running_engine():
    docker = Recorded()
    assert await ensure(docker, "opus_sabnzbd", SPEC) is True
    assert docker.calls == ["pull", "stop", "remove", "create", "start"]


async def test_a_registry_that_fails_leaves_the_running_engine_alone():
    docker = Recorded(pull_fails=True)
    with pytest.raises(ContainerError, match="registry unreachable"):
        await ensure(docker, "opus_sabnzbd", SPEC)
    assert docker.calls == ["pull"]


async def test_a_matching_definition_is_only_started():
    docker = Recorded()

    async def inspect(name):
        return {"Config": {"Labels": {OWNER_LABEL: "engine",
                                      SPEC_LABEL: stamped(SPEC)["Labels"][SPEC_LABEL]}}}
    docker.inspect = inspect
    assert await ensure(docker, "opus_sabnzbd", SPEC) is False
    assert docker.calls == ["start"]


@pytest.fixture
def daemon(monkeypatch):
    answers = {}

    def respond(request):
        path = request.url.path
        if path.endswith("/images/create"):
            return httpx.Response(200, content=answers["pull"])
        if path.endswith("/json"):
            return httpx.Response(answers.get("image", 200), json={})
        return httpx.Response(404)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(respond),
                                 base_url=f"{containers.BASE_URL}/{containers.API_VERSION}")
    monkeypatch.setattr(Docker, "_client", client)
    return answers


def lines(*said):
    return "\n".join(json.dumps(s) for s in said).encode()


async def test_a_pull_the_daemon_reports_failed_inside_its_stream_is_a_failure(daemon):
    daemon["pull"] = lines({"status": "Pulling from linuxserver/sabnzbd"},
                           {"errorDetail": {"message": "toomanyrequests"}, "error": "toomanyrequests"})
    with pytest.raises(ContainerError, match="toomanyrequests"):
        await Docker().pull("lscr.io/linuxserver/sabnzbd:latest")


async def test_a_pull_is_believed_only_once_the_image_is_there(daemon):
    daemon["pull"] = lines({"status": "Status: Image is up to date"})
    await Docker().pull("lscr.io/linuxserver/sabnzbd:latest")
    daemon["image"] = 404
    with pytest.raises(ContainerError, match="not there after pulling"):
        await Docker().pull("lscr.io/linuxserver/sabnzbd:latest")
