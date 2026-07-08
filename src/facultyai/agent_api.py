"""Programmatic, agent-friendly API for FacultyAI.

Every function returns a plain JSON-serializable dict with a consistent
envelope so AI agents (and scripts) can consume results without parsing
Rich console output.

Envelope shape:
    {"success": bool, "data": {...}} on success
    {"success": false, "error": {"code": str, "message": str}} on failure

All functions are async and manage their own DB/cache lifecycle so they
can be called standalone from an MCP server, a script, or the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cache import CacheManager
from .config import load_config
from .database import Database
from .lock_manager import LockManager
from .logging_config import get_logger
from .schema import load_schema

log = get_logger("agent_api")


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": {"code": code, "message": message, **extra}}


def _clean(val: Any) -> Any:
    """Normalize empty-like strings ('nan', 'none', '') to None."""
    if val is None:
        return None
    if isinstance(val, str) and val.strip().lower() in ("", "nan", "none", "null"):
        return None
    return val


# ---------------------------------------------------------------------------
# Read operations (no lock needed)
# ---------------------------------------------------------------------------


async def get_status(config_path: str = "config.yaml") -> dict[str, Any]:
    """Return all jobs, status counts, and recent run history."""
    cfg = load_config(config_path)
    db = Database(cfg.files.database)
    async with db:
        jobs = await db.list_jobs()
        counts: dict[str, int] = {}
        for j in jobs:
            counts[j["status"]] = counts.get(j["status"], 0) + 1

        history: list[dict[str, Any]] = []
        if db._connection is not None:
            async with db._connection.execute(
                "SELECT * FROM run_history ORDER BY id DESC LIMIT 5"
            ) as cursor:
                history = [dict(r) for r in await cursor.fetchall()]

        return ok({
            "jobs": [_job_view(j) for j in jobs],
            "summary": {
                "completed": counts.get("completed", 0),
                "running": counts.get("running", 0),
                "pending": counts.get("pending", 0),
                "failed": counts.get("failed", 0),
                "total": len(jobs),
            },
            "history": history,
        })


async def get_results(
    config_path: str = "config.yaml",
    university: str | None = None,
    department: str | None = None,
) -> dict[str, Any]:
    """Return extracted faculty records, optionally filtered by uni/dept."""
    cfg = load_config(config_path)
    db = Database(cfg.files.database)
    async with db:
        if university is not None:
            rows = await db.get_faculty_by_university(university, department)
        else:
            rows = await db.get_active_faculty()

        records = []
        for r in rows:
            parsed = json.loads(r["data_json"] or "{}")
            parsed["_university"] = r["university"]
            parsed["_department"] = r["department"]
            records.append(parsed)

        return ok({"count": len(records), "records": records})


async def list_targets(config_path: str = "config.yaml") -> dict[str, Any]:
    """Return the input universities/departments queue."""
    cfg = load_config(config_path)
    db = Database(cfg.files.database)
    async with db:
        rows = await db.get_input_universities()
        targets = [
            {
                "university": r["university"],
                "department": _clean(r.get("department")),
                "link": _clean(r.get("link")),
                "status": _clean(r.get("status")),
            }
            for r in rows
        ]
        return ok({"count": len(targets), "targets": targets})


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


async def add_target(
    university: str,
    department: str | None = None,
    link: str | None = None,
    config_path: str = "config.yaml",
) -> dict[str, Any]:
    """Add or update a scrape target in the DB input queue."""
    if not university or not university.strip():
        return err("INVALID_INPUT", "university is required")

    cfg = load_config(config_path)
    db = Database(cfg.files.database)
    async with db:
        await db.upsert_input_university(
            university.strip(),
            department.strip() if department else None,
            link=link.strip() if link else None,
            status=None,
        )
        return ok({
            "university": university.strip(),
            "department": department.strip() if department else None,
            "link": link.strip() if link else None,
        })


async def run(
    config_path: str = "config.yaml",
    retry_failed: bool = False,
) -> dict[str, Any]:
    """Run the full scrape pipeline. Returns per-job results + summary."""
    cfg = load_config(config_path)
    lock = LockManager()
    if not lock.acquire():
        return err("LOCKED", "Another facultyai process is already running.")

    try:
        db = Database(cfg.files.database)
        cache = CacheManager(cfg.files.cache_dir)
        schema = load_schema(cfg.files.schema_file)

        async with db:
            try:
                from .orchestrator import run_pipeline

                summary = await run_pipeline(
                    cfg, schema, db, cache, retry_failed=retry_failed
                )
                jobs = await db.list_jobs()
                return ok({
                    "summary": summary,
                    "jobs": [_job_view(j) for j in jobs],
                })
            except Exception as e:  # noqa: BLE001
                log.error("run failed: %s", e)
                return err("PIPELINE_ERROR", str(e))
            finally:
                cache.close()
    finally:
        lock.release()


async def discover_departments(
    university: str,
    link: str | None = None,
    config_path: str = "config.yaml",
) -> dict[str, Any]:
    """Discover academic departments for a university via LLM."""
    if not university or not university.strip():
        return err("INVALID_INPUT", "university is required")

    cfg = load_config(config_path)
    lock = LockManager()
    if not lock.acquire():
        return err("LOCKED", "Another facultyai process is already running.")

    try:
        db = Database(cfg.files.database)
        async with db:
            from .llm_factory import get_llm
            from .scraper_graph import _discover_departments_impl

            llm = get_llm(cfg.llm)
            state: dict[str, Any] = {
                "university": university.strip(),
                "page_url": link.strip() if link and link.strip().startswith("http") else "",
            }
            result = await _discover_departments_impl(state, cfg, llm, None)
            departments = result.get("discovered_departments", [])
            return ok({
                "university": university.strip(),
                "departments": departments,
                "count": len(departments),
            })
    finally:
        lock.release()


async def export(
    config_path: str = "config.yaml",
    fmt: str = "excel",
    output_path: str | None = None,
) -> dict[str, Any]:
    """Export active faculty records to Excel or JSON."""
    cfg = load_config(config_path)
    db = Database(cfg.files.database)
    schema = load_schema(cfg.files.schema_file)

    async with db:
        if fmt == "json":
            rows = await db.get_active_faculty()
            records = []
            for r in rows:
                parsed = json.loads(r["data_json"] or "{}")
                parsed["_university"] = r["university"]
                parsed["_department"] = r["department"]
                records.append(parsed)
            out = Path(output_path or "faculty_data.json")
            out.write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return ok({"format": "json", "path": str(out), "count": len(records)})

        from .exporter import export_to_excel

        out_path = output_path or cfg.files.output_excel
        count = await export_to_excel(db, schema, out_path)
        return ok({"format": "excel", "path": str(out_path), "count": count})


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _job_view(j: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": j.get("job_id"),
        "university": j.get("university"),
        "department": _clean(j.get("department")),
        "type": j.get("job_type"),
        "status": j.get("status"),
        "listing_url": _clean(j.get("listing_url")),
        "error": j.get("error"),
    }
