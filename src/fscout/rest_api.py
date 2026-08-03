"""RESTful API server for fscout.

Exposes the same operations as ``agent_api`` over HTTP so that n8n or any
client can trigger and query scraping without the MCP protocol.

Run with:
    fscout-api                          # http://0.0.0.0:8000
    fscout-api --host 127.0.0.1 --port 9000
    # or
    python -m fscout.rest_api --port 9000

Requires the optional ``api`` dependency:
    pip install "faculty-scout[api]"
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import agent_api

app = FastAPI(
    title="Faculty Scout API",
    description="Scrape university faculty information from listing pages.",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# Background job store
# ---------------------------------------------------------------------------
# Keyed by job_id. Run/clear-and-run launch a background task and return
# immediately so n8n can poll /api/jobs/{id} instead of blocking on a
# long HTTP request.
JOBS: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in job.items() if k != "_task"}


def _launch_run_job(job_type: str, force: bool, config_path: str) -> str:
    """Create a job and start it in the background. Returns the job_id."""
    job_id = uuid.uuid4().hex[:12]
    job: dict[str, Any] = {
        "id": job_id,
        "type": job_type,
        "status": "running",
        "force": force,
        "config_path": config_path,
        "created_at": _now(),
        "started_at": _now(),
        "finished_at": None,
        "done": 0,
        "total": 0,
        "message": "Starting...",
        "result": None,
        "error": None,
    }
    JOBS[job_id] = job

    async def _runner() -> None:
        try:
            cfg = agent_api_load_config(config_path)
            cache = _new_cache(cfg)
            schema = _new_schema(cfg)
            try:
                from .pipeline import run_pipeline

                def _cb(done: int, total: int, msg: str) -> None:
                    job["done"] = done
                    job["total"] = total
                    job["message"] = msg

                result = await run_pipeline(
                    cfg, schema, cache,
                    force=force,
                    clear_status=(job_type == "clear-and-run"),
                    progress_callback=_cb,
                )
                job["result"] = result
                job["status"] = "completed"
                job["message"] = "Completed"
            finally:
                cache.close()
        except asyncio.CancelledError:
            job["status"] = "stopped"
            job["message"] = "Stopped by user"
            job["finished_at"] = _now()
            raise
        except Exception as e:
            job["status"] = "failed"
            job["error"] = str(e)
            job["message"] = f"Failed: {e}"
        finally:
            job["finished_at"] = _now()

    task = asyncio.get_event_loop().create_task(_runner())
    job["_task"] = task
    return job_id


def agent_api_load_config(config_path: str) -> Any:
    from .config import load_config
    return load_config(config_path)


def _new_cache(cfg: Any) -> Any:
    from .cache import CacheManager
    return CacheManager(cfg.files.cache_dir)


def _new_schema(cfg: Any) -> Any:
    from .schema import load_schema
    return load_schema(cfg.files.schema_file)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    force: bool = False
    config_path: str = "config.yaml"


class DiscoverRequest(BaseModel):
    university: str
    link: str | None = None
    config_path: str = "config.yaml"


class AddTargetRequest(BaseModel):
    university: str
    department: str | None = None
    link: str | None = None
    config_path: str = "config.yaml"


class ExportRequest(BaseModel):
    fmt: str = "excel"
    output_path: str | None = None
    config_path: str = "config.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _respond(result: dict[str, Any], status: int = 200) -> JSONResponse:
    """Convert an agent_api envelope into an HTTP response."""
    if result.get("success", False):
        return JSONResponse(result, status_code=status)
    return JSONResponse(
        {"success": False, "error": result.get("error", {"message": "unknown error"})},
        status_code=status,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/targets")
async def list_targets(config_path: str = "config.yaml"):
    """List all configured scrape targets and their status."""
    return _respond(await agent_api.list_targets(config_path))


@app.post("/api/targets")
async def add_target(body: AddTargetRequest):
    """Add a scrape target (university + optional department + link)."""
    return _respond(await agent_api.add_target(
        body.university, body.department, body.link, body.config_path
    ))


@app.get("/api/status")
async def status(config_path: str = "config.yaml"):
    """Get job statuses and summary counts."""
    return _respond(await agent_api.get_status(config_path))


@app.get("/api/results")
async def results(
    university: str | None = Query(default=None),
    department: str | None = Query(default=None),
    config_path: str = "config.yaml",
):
    """Get extracted faculty records, optionally filtered by university/department."""
    return _respond(await agent_api.get_results(config_path, university, department))


@app.post("/api/discover")
async def discover(body: DiscoverRequest):
    """Discover academic departments for a university via LLM."""
    return _respond(await agent_api.discover_departments(
        body.university, body.link, body.config_path
    ))


@app.post("/api/run")
async def run(body: RunRequest):
    """Start a background scrape job. Returns a job_id immediately.

    Poll GET /api/jobs/{id} for status/progress.
    """
    job_id = _launch_run_job("run", force=body.force, config_path=body.config_path)
    return JSONResponse({
        "success": True,
        "data": {"job_id": job_id, "status": "running", "message": "Job started"},
    })


@app.post("/api/clear-and-run")
async def clear_and_run(body: RunRequest):
    """Clear all target statuses, then start a background scrape job.

    Returns a job_id immediately. Poll GET /api/jobs/{id} for status.
    """
    job_id = _launch_run_job("clear-and-run", force=body.force, config_path=body.config_path)
    return JSONResponse({
        "success": True,
        "data": {"job_id": job_id, "status": "running", "message": "Job started"},
    })


@app.get("/api/jobs")
async def list_jobs():
    """List all background scrape jobs (most recent first)."""
    jobs = [_job_summary(j) for j in sorted(JOBS.values(), key=lambda j: j["created_at"], reverse=True)]
    return JSONResponse({"success": True, "data": {"jobs": jobs, "total": len(jobs)}})


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    """Get status/progress for a background scrape job."""
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse(
            {"success": False, "error": {"code": "NOT_FOUND", "message": f"Job {job_id} not found"}},
            status_code=404,
        )
    return JSONResponse({"success": True, "data": _job_summary(job)})


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    """Stop a running background scrape job (cancels its task)."""
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse(
            {"success": False, "error": {"code": "NOT_FOUND", "message": f"Job {job_id} not found"}},
            status_code=404,
        )

    if job["status"] != "running":
        return JSONResponse({
            "success": True,
            "data": {"job_id": job_id, "status": job["status"], "message": f"Job already {job['status']}"},
        })

    task = job.get("_task")
    if task is not None:
        task.cancel()
        job["status"] = "stopping"
        job["message"] = "Stop requested"
        return JSONResponse({
            "success": True,
            "data": {"job_id": job_id, "status": "stopping", "message": "Stop requested"},
        })

    job["status"] = "stopped"
    job["finished_at"] = _now()
    return JSONResponse({
        "success": True,
        "data": {"job_id": job_id, "status": "stopped", "message": "Stopped"},
    })


@app.post("/api/export")
async def export(body: ExportRequest):
    """Export active faculty records to a file. fmt = 'json' or 'excel'."""
    return _respond(await agent_api.export(
        body.config_path, fmt=body.fmt, output_path=body.output_path
    ))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the fscout REST API server.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
