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
    """Run the full scrape pipeline over all pending targets.

    Long-running: fetches pages and calls the LLM for every target.
    """
    return _respond(await agent_api.run(body.config_path, force=body.force))


@app.post("/api/clear-and-run")
async def clear_and_run(body: RunRequest):
    """Clear all target statuses in the input Excel, then run the full pipeline."""
    return _respond(await agent_api.clear_and_run(body.config_path, force=body.force))


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
