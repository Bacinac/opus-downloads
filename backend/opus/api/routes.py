"""The /api aggregator: one router per concern."""

from fastapi import APIRouter
from opus_core import plugins, revision

from opus.api.routers import auth, engines, jobs, search, system
from opus.plugins import PLUGINS

router = APIRouter(prefix="/api")

router.include_router(auth.router)
router.include_router(system.router)
router.include_router(revision.router("opus-downloads"))
router.include_router(plugins.router(PLUGINS))
router.include_router(engines.router)
router.include_router(search.router)
router.include_router(jobs.router)
