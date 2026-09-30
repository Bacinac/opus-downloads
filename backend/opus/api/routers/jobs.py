"""The job surface — grab, track, cancel.

A grab_ref names its own engine, so this router never switches on protocol: it
hands the ref to opus.jobs and the engine the release came from does the work.
Status is read through on every request rather than served from the row, so what
a consumer sees is what the engine says right now."""

import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from opus import jobs as job_service
from opus.api.shared import Answered
from opus.settings_store import NAME, RuntimeConfig, current_runtime

router = APIRouter(route_class=Answered)

IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


class GrabRequest(BaseModel):
    grab_ref: dict
    # it becomes a category in the engine and a folder under the landing zone
    namespace: str = Field(pattern=f"^{NAME.pattern}$", max_length=128)
    app: str | None = None


@router.post("/grab")
async def grab(request: Request, req: GrabRequest, runtime: RuntimeConfig = Depends(current_runtime),
               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    if idempotency_key is not None and not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise HTTPException(422, "Idempotency-Key must be 16–128 safe characters")
    app = getattr(request.state, "module_app", None) or req.app
    job = await job_service.start(runtime, req.grab_ref, req.namespace, app, idempotency_key)
    return {"job_id": job.id}


@router.get("/jobs")
async def list_jobs():
    return [job_service.to_dict(job) for job in await job_service.listing()]


@router.get("/jobs/{job_id}")
async def job_status(request: Request, job_id: str, runtime: RuntimeConfig = Depends(current_runtime)):
    job, live = await job_service.refresh(runtime, job_id,
                                          app=getattr(request.state, "module_app", None))
    return job_service.to_dict(job, live)


@router.get("/jobs/{job_id}/history")
async def job_history(request: Request, job_id: str):
    events = await job_service.history(job_id, app=getattr(request.state, "module_app", None))
    return [job_service.event_to_dict(event) for event in events]


@router.delete("/jobs/{job_id}", status_code=204)
async def cancel(request: Request, job_id: str, runtime: RuntimeConfig = Depends(current_runtime)):
    await job_service.cancel(runtime, job_id, app=getattr(request.state, "module_app", None))
