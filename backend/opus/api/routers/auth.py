"""Proving who you are, and finding out whether you have to.

The session endpoint is answerable by anyone, on purpose: a browser has to be
able to ask "am I in, and is there even a door here" before it can decide
whether to draw a login form. It says only that, never the credential.

The roster is not here and neither is the screen for it. This module is the
owner's tool from end to end, so the only people it has questions about are the
ones trying to get in — and it asks Library, which keeps the answer."""

from contextlib import contextmanager

import httpx
import opus_auth
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from opus_auth import ADMIN
from opus_auth.authority import AuthorityUnavailable, TooManyAttempts
from pydantic import BaseModel
from sqlalchemy import text

from opus import auth
from opus.config import settings
from opus.db import SessionLocal
from opus.settings_store import current_runtime, store_credentials

router = APIRouter()


class Login(BaseModel):
    username: str
    password: str


class PasskeyAnswer(BaseModel):
    credential: dict


class NewPassword(BaseModel):
    current: str
    password: str


@router.get("/ping")
async def ping():
    """Liveness for a monitor. Says nothing a stranger could use."""
    return {"ok": True}


@router.get("/ready")
async def ready():
    """Readiness: the process can answer only when its state store can too."""
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
    return {"ok": True}


@router.get("/auth/session")
async def session_state(request: Request):
    runtime = await current_runtime()
    cookie = request.cookies.get(opus_auth.SESSION_COOKIE)
    inside = await auth.allowed(runtime, cookie, request.headers.get(opus_auth.TOKEN_HEADER))
    found = await auth.person(cookie) if inside else None
    return {
        "required": True,
        "authenticated": inside,
        # only to whoever is already inside: who is in is not a stranger's
        # question, and somebody the door turned away is a stranger here even
        # though the roster next door knows their name
        "username": found.name if found else "",
        # everyone this door lets in is an admin — it opens for nobody else —
        # so the standing is said rather than worked out, in the same word the
        # authority and the other two modules use
        "role": ADMIN if inside else "",
        "passkey": opus_auth.passkey_door(settings.cookie_domain, request) is not None,
    }


def _sign_in(response: Response, request: Request, who: str, version: int):
    opus_auth.set_cookie(response, request, settings.cookie_domain, opus_auth.SESSION_COOKIE,
                         opus_auth.issue(settings.session_key, who, version),
                         opus_auth.SESSION_MAX_AGE)


@contextmanager
def _asking():
    try:
        yield
    except TooManyAttempts as refused:
        raise HTTPException(429, "too many failed attempts",
                            headers={"Retry-After": refused.retry_after})
    except AuthorityUnavailable as why:
        raise HTTPException(502, str(why))


def _admitted(said, request: Request, response: Response):
    """This module keeps no credential to compare against — one store, asked,
    rather than three kept in step — and it lets nobody in who does not own the
    place, because everything past this door is the library's plumbing."""
    if said is None:
        # one message for a wrong name and a wrong password: which of the two
        # was right is not the asker's business
        return Response(status_code=401)
    if said.role != ADMIN:
        return JSONResponse({"detail": "this is the library's tool, not yours"},
                            status_code=403)
    _sign_in(response, request, said.name, said.version)
    return {"authenticated": True}


@router.post("/auth/login")
async def login(body: Login, request: Request, response: Response):
    with _asking():
        said = await auth.authority.verify(body.username, body.password,
                                           opus_auth.client_address(request))
    return _admitted(said, request, response)


def _door(request: Request) -> str:
    door = opus_auth.passkey_door(settings.cookie_domain, request)
    if door is None:
        raise HTTPException(404, "this door takes no passkey")
    return door[1]


@router.post("/auth/passkey/options")
async def passkey_options(request: Request):
    _door(request)
    with _asking():
        return await auth.authority.passkey_options()


@router.post("/auth/passkey/login")
async def passkey_login(body: PasskeyAnswer, request: Request, response: Response):
    """Library checks the signature against the key it keeps and this door's
    own address, so a signature collected on a sibling site opens nothing."""
    with _asking():
        said = await auth.authority.passkey(body.credential, _door(request),
                                            opus_auth.client_address(request))
    return _admitted(said, request, response)


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    opus_auth.delete_cookie(response, request, settings.cookie_domain, opus_auth.SESSION_COOKIE)
    return {"authenticated": False}


@router.post("/auth/password")
async def change_password(body: NewPassword, request: Request, response: Response):
    """Changing your own password from whichever module you happen to be in.

    Carried next door rather than kept: the roster has one home. The session
    goes with it, so the authority does its own asking for the current one and
    this module never sees a secret it has no business holding."""
    cookie = request.cookies.get(opus_auth.SESSION_COOKIE)
    who = await auth.person(cookie)
    if who is None:
        return Response(status_code=401)
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(
                f"{auth.authority.url}/api/auth/password",
                json={"current": body.current, "password": body.password},
                cookies={opus_auth.SESSION_COOKIE: cookie},
                headers={"X-Forwarded-For": opus_auth.client_address(request)})
    except httpx.HTTPError as why:
        raise HTTPException(502, f"the library could not be reached ({type(why).__name__})")
    if resp.status_code == 401:
        return Response(status_code=401)
    if resp.status_code == 429:
        raise HTTPException(429, "too many failed attempts",
                            headers={"Retry-After": resp.headers.get("retry-after", "60")})
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, "the roster would not take it")
    # the version just rose, so every session issued under the old secret ended
    # — including this caller's, who has to keep theirs
    try:
        version = int(resp.json()["version"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(502, "the library changed the password and did not say its version")
    auth.authority.forget()
    _sign_in(response, request, who.name, version)
    return {"changed": True}


@router.get("/auth/token")
async def token():
    """The token Library calls with, for the admin to copy into Library by hand.
    Empty until one is made."""
    return {"tokens": [{"consumer": auth.CONSUMER,
                        "token": auth.consumer_token(await current_runtime())}]}


class TokenFor(BaseModel):
    consumer: str


@router.post("/auth/token")
async def new_token(body: TokenFor, request: Request):
    """A new token, which ends the old one: Library is turned away until it is
    given this one. A person's act, never a module's."""
    if not await auth.owns(request.cookies.get(opus_auth.SESSION_COOKIE)):
        raise HTTPException(403, "only an admin may do this")
    if body.consumer != auth.CONSUMER:
        raise HTTPException(404, "no such consumer")
    value = opus_auth.new_token()
    await store_credentials({auth.token_key(auth.CONSUMER): value})
    return {"consumer": auth.CONSUMER, "token": value}
