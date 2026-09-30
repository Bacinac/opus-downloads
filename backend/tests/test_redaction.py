import pytest

from opus.containers import ContainerError
from opus.engines.base import EngineError
from opus.redaction import redacted


@pytest.mark.parametrize(("said", "hidden"), [
    ("Client error '400 Bad Request' for url "
     "'https://store.example/api/user/login?email=filip%40x.hr&password=5f4dcc3b&app_id=1'",
     ["filip%40x.hr", "5f4dcc3b", "app_id=1"]),
    ("fetching http://user:hunter2@indexer/dl failed", ["hunter2"]),
    ("rejected body username=filip&password=hunter2&apikey=abc123", ["hunter2", "abc123"]),
    ('[{"propertyName": "ApiKey", "apiKey": "abc123", "token": "t0k3n"}]', ["abc123", "t0k3n"]),
    ("Authorization: Bearer eyJhbGciOi.abc.def", ["eyJhbGciOi"]),
    ("X-Api-Key: 0123456789abcdef", ["0123456789abcdef"]),
    ("ws://slskd:5030/hub?access_token=secret-ws", ["secret-ws"]),
])
def test_what_an_error_may_not_carry(said, hidden):
    clean = redacted(said)
    assert not [h for h in hidden if h in clean]


@pytest.mark.parametrize("said", [
    "sabnzbd kept host_whitelist='opus_sabnzbd' instead of 'x'",
    "prowlarr: api_key is not configured",
    "the store offers 11 of the 12 tracks on 'Keys: Live'",
    "the stream stopped at 1024 of 2048 bytes",
])
def test_what_an_error_keeps(said):
    assert redacted(said) == said


def test_every_engine_and_container_error_is_redacted_where_it_is_raised():
    assert "hunter2" not in str(EngineError("GET http://sab/api?apikey=hunter2 failed"))
    assert "hunter2" not in str(ContainerError("pull https://registry/x?token=hunter2"))

