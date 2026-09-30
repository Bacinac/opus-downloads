"""Who may talk to OPUS.

Two kinds of caller, because they are not alike. A **person** arrives in a
browser, proves themselves once and carries a session cookie. A **consuming
module** has no browser and no session: it carries a token on every call.

The roster is not here. It lives in Library; this module holds no password and
no name, and asks. Downloads is the owner's tool from end to end — engines,
credentials, a landing zone — so a member has no business past this door at
all, reading included."""

import opus_auth
from fastapi.responses import JSONResponse
from opus_auth import ADMIN
from opus_auth.authority import Authority, Person

from opus.config import settings
from opus.engines.catalog import ACCESS_GROUP

# what answers before anyone has proved anything: liveness, and the exchange
# that proves it. Letting go of a session must not be able to fail because the
# session is already gone.
OPEN_PATHS = ("/api/ping", "/api/ready", "/api/auth/login", "/api/auth/session", "/api/auth/logout",
              "/api/auth/passkey/options", "/api/auth/passkey/login")

authority = Authority(settings.auth_url, settings.auth_token)


CONSUMER = "library"


def token_key(consumer: str) -> str:
    return f"{ACCESS_GROUP}_{consumer}_token"


def consumer_token(runtime) -> str:
    return runtime.get(token_key(CONSUMER))


async def person(cookie: str | None) -> Person | None:
    said = opus_auth.session_user(settings.session_key, cookie)
    return None if said is None else await authority.current(*said)


async def owns(cookie: str | None) -> bool:
    """An unrecognised standing is a no: a word this module has never heard of
    is an authority that moved on without it, and guessing generously at that
    is how a door opens by accident."""
    found = await person(cookie)
    return found is not None and found.role == ADMIN


async def refused(cookie: str | None) -> JSONResponse | None:
    """Why this cookie is not let in, or None when its owner is. Signed in as
    somebody else is a different answer from not signed in at all: the first
    must not send them back to a login screen they already passed."""
    found = await person(cookie)
    if found is None:
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
    if found.role != ADMIN:
        return JSONResponse({"detail": "this is the library's tool, not yours"}, status_code=403)
    return None


async def allowed(runtime, cookie: str | None, token: str | None = None) -> bool:
    if opus_auth.same_token(token, consumer_token(runtime)):
        return True
    return await owns(cookie)
