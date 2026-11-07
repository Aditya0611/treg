"""Local web app: upload a list or confirm a query, then download the workbook."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.export import workbook_bytes
from app.files import parse_upload
from app.filters import Filters, filters_from_dict, parse_query
from app.pipeline import Job, Options, failure_text, run_job
from app.treg_client import TregClient

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

app = FastAPI(title="Treg company list")
JOBS: dict[str, Job] = {}
UPLOADS: dict[str, list[dict]] = {}
TASKS: set = set()


class ParseBody(BaseModel):
    query: str = ""
    limit: int | None = None
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""


class JobBody(BaseModel):
    mode: str
    upload_id: str = ""
    filters: dict = Field(default_factory=dict)
    options: dict = Field(default_factory=dict)
    token: str = ""
    org: str = ""


def _spawn(coro) -> None:
    task = asyncio_create(coro)
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


def asyncio_create(coro):
    import asyncio

    return asyncio.create_task(coro)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "token_configured": bool(os.environ.get("TREG_TOKEN", "").strip()),
        "llm_configured": bool(os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")),
    }


@app.post("/api/parse")
async def parse_endpoint(body: ParseBody):
    filters = await parse_query(
        body.query,
        api_key=body.llm_api_key or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "",
        base_url=body.llm_base_url or os.environ.get("LLM_BASE_URL") or "",
        model=body.llm_model or os.environ.get("LLM_MODEL") or "",
        limit=body.limit,
    )
    return filters.to_dict()


@app.post("/api/uploads")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 15_000_000:
        raise HTTPException(413, "File is larger than 15 MB")
    try:
        rows, column, skipped = parse_upload(file.filename or "", data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    import uuid

    upload_id = uuid.uuid4().hex[:12]
    UPLOADS[upload_id] = rows
    return {
        "upload_id": upload_id,
        "column": column,
        "count": len(rows),
        "skipped": skipped,
        "sample": rows[:5],
    }


@app.post("/api/jobs")
async def create_job(body: JobBody):
    if body.mode not in {"file", "query"}:
        raise HTTPException(400, "mode must be file or query")
    token = (body.token or os.environ.get("TREG_TOKEN") or "").strip()
    if not token:
        raise HTTPException(400, "Add a Treg token in the form, or set TREG_TOKEN in .env")
    org = (body.org or os.environ.get("TREG_ORG") or "").strip()
    try:
        incoming = filters_from_dict(body.filters)
        options = Options.from_dict(body.options)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"Check the filters and options: {exc}") from exc
    filters = incoming if body.mode == "query" else Filters(title=incoming.title)
    if body.mode == "query" and not any((filters.q, filters.industry, filters.technology, filters.country)):
        raise HTTPException(400, "Add an industry, a query, or a country before searching")
    if body.mode == "file":
        seeds = UPLOADS.get(body.upload_id)
        if not seeds:
            raise HTTPException(400, "Upload the file again. That file is no longer on the server.")
    else:
        seeds = []
    job = Job(mode=body.mode, filters=filters, options=options)
    JOBS[job.id] = job
    client = TregClient(
        token,
        base_url=os.environ.get("TREG_BASE_URL") or "https://treg.to",
        org=org,
        max_cost_usd=options.max_cost_per_call_usd,
        waterfall=options.waterfall,
    )

    async def runner():
        try:
            async with client:
                await run_job(job, client, list(seeds))
        except Exception as exc:
            job.status = "error"
            job.phase = "error"
            job.error = failure_text(exc)[:500]
            job.log(job.error)

    _spawn(runner())
    return job.public()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "That run is not on this server anymore")
    return job.public()


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "That run is not on this server anymore")
    job.stop = True
    job.stop_reason = job.stop_reason or "Stopped"
    return job.public()


@app.get("/api/jobs/{job_id}/export")
def export_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "That run is not on this server anymore")
    if not job.companies and not job.removed:
        raise HTTPException(400, "Nothing to export yet")
    payload = workbook_bytes(job.companies, job.contacts, job.removed, job.accuracy, job.accuracy_history)
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="treg-{job.id}.xlsx"'},
    )
