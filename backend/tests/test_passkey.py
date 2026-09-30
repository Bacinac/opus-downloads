import httpx
import opus_auth
import pytest
from opus_auth import ADMIN, USER
from opus_auth.authority import AuthorityUnavailable, Person

from opus import auth
from opus.config import settings
from opus.main import app

HERE = {"x-forwarded-host": "downloads.example.com"}
LAN = {"x-forwarded-host": "192.168.1.103:5290"}
SIGNED = {"id": "a2V5", "rawId": "a2V5", "type": "public-key", "response": {}}


class Library:
    def __init__(self, person):
        self.person = person
        self.asked = []

    async def passkey_options(self):
        return {"challenge": "c2lnbg", "rpId": "example.com"}

    async def passkey(self, credential, origin, address):
        self.asked.append((credential, origin))
        return self.person


@pytest.fixture
def library(monkeypatch):
    monkeypatch.setattr(settings, "cookie_domain", "example.com")

    def answering(person):
        said = Library(person)
        monkeypatch.setattr(auth, "authority", said)
        return said
    return answering


@pytest.fixture
async def browser():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://opus") as client:
        yield client


async def test_the_owner_signs_in_with_a_passkey_signed_on_this_door(browser, library):
    said = library(Person("filip", 3, ADMIN, "Filip"))
    options = await browser.post("/api/auth/passkey/options", headers=HERE)
    entered = await browser.post("/api/auth/passkey/login", headers=HERE,
                                 json={"credential": SIGNED})
    assert options.json()["rpId"] == "example.com"
    assert entered.status_code == 200
    assert entered.headers["set-cookie"].startswith(f"{opus_auth.SESSION_COOKIE}=")
    assert said.asked == [(SIGNED, "https://downloads.example.com")]


@pytest.mark.parametrize(("person", "status"), [(Person("ana", 1, USER, "Ana"), 403), (None, 401)])
async def test_a_passkey_opens_this_door_only_for_its_owner(browser, library, person, status):
    library(person)
    entered = await browser.post("/api/auth/passkey/login", headers=HERE,
                                 json={"credential": SIGNED})
    assert entered.status_code == status
    assert "set-cookie" not in entered.headers


async def test_off_the_shared_domain_there_is_no_passkey(browser, library):
    said = library(Person("filip", 3, ADMIN, "Filip"))
    assert (await browser.get("/api/auth/session", headers=HERE)).json()["passkey"] is True
    assert (await browser.get("/api/auth/session", headers=LAN)).json()["passkey"] is False
    assert (await browser.post("/api/auth/passkey/options", headers=LAN)).status_code == 404
    assert (await browser.post("/api/auth/passkey/login", headers=LAN,
                               json={"credential": SIGNED})).status_code == 404
    assert said.asked == []


async def test_library_away_is_said(browser, library, monkeypatch):
    said = library(None)

    async def away():
        raise AuthorityUnavailable("no answer")
    monkeypatch.setattr(said, "passkey_options", away)
    assert (await browser.post("/api/auth/passkey/options", headers=HERE)).status_code == 502
