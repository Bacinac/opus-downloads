from contextlib import asynccontextmanager

import opus_auth
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opus_core.door import Door
from opus_core.encoding import CompressJSON

from opus import auth, logs, provision, runner
from opus.api.routes import router
from opus.config import settings
from opus.engines.base import EngineMode
from opus.engines.registry import build_engines
from opus.settings_store import current_runtime

logs.configure()


@asynccontextmanager
async def lifespan(app: FastAPI):
    bundled = [e.name for e in build_engines(await current_runtime())
               if e.mode is EngineMode.BUNDLED]
    if bundled and not settings.engines_url:
        raise RuntimeError(f"OPUS_ENGINES_URL is not set, and {', '.join(bundled)} run bundled: "
                           "their interfaces would have no origin to be opened on")
    # an in-process download lived in the memory of the process that just ended;
    # say so before answering anything about it
    await runner.recover()
    # a definition this release changed is applied like a saved setting is
    provision.apply_saved({})
    yield


class _Downloads(FastAPI):
    def build_middleware_stack(self):
        return CompressJSON(opus_auth.secured(super().build_middleware_stack()))


app = _Downloads(title="OPUS", lifespan=lifespan,
                 docs_url=None, redoc_url=None, openapi_url=None)


async def _module(token: str | None) -> bool:
    return bool(token) and opus_auth.same_token(token, auth.consumer_token(await current_runtime()))


_MODULE_CALLS = frozenset({
    ("POST", "/api/search"),
    ("POST", "/api/inspect"),
    ("POST", "/api/grab"),
})


def _module_route(request: Request) -> bool:
    """Whether a consuming module may make this request.

    A service token crosses a module boundary, so it is deliberately not an
    alternate administrator credential. Consumers need to choose, inspect and
    acquire releases, then follow or cancel an individual job they already
    know.
    They never need installation settings, engine control, token rotation or a
    global list of another consumer's jobs.
    """
    if (request.method, request.url.path) in _MODULE_CALLS:
        return True
    parts = request.url.path.split("/")
    return (request.method in {"GET", "DELETE"}
            and len(parts) == 4
            and parts[:3] == ["", "api", "jobs"]
            and bool(parts[3]))


async def refusal(request: Request) -> JSONResponse | None:
    # lists what it lets through rather than what it protects, so a new route
    # is behind the door without anyone remembering to put it there
    cookie = request.cookies.get(opus_auth.SESSION_COOKIE)
    token = request.headers.get(opus_auth.TOKEN_HEADER)
    module = await _module(token)
    # a cookie on the parent domain is written and cleared by the open paths too,
    # and a page elsewhere reaches those without carrying one
    if (cookie or request.url.path in auth.OPEN_PATHS) and not module and not opus_auth.same_origin(request):
        return JSONResponse({"detail": "not sent from this module's pages"}, status_code=403)
    if request.url.path in auth.OPEN_PATHS:
        return None
    if module:
        # This installation's service credential belongs to Library. Routes
        # read this scope rather than trusting the app field a caller supplied.
        request.state.module_app = auth.CONSUMER
        if _module_route(request):
            return None
        return JSONResponse({"detail": "service token may only acquire and track jobs"}, status_code=403)
    return await auth.refused(cookie)


app.add_middleware(Door, refusal=refusal)


app.include_router(router)
