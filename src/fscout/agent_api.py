"""Programmatic, agent-friendly API for fscout.

Every function returns a plain JSON-serializable dict with a consistent
envelope so AI agents (and scripts) can consume results without parsing
Rich console output.

Envelope shape:
    {"success": bool, "data": {...}} on success
    {"success": false, "error": {"code": str, "message": str}} on failure

All functions are async and manage their own cache lifecycle so they
can be called standalone from an MCP server, a script, or the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cache import CacheManager
from .config import load_config
from .logging_config import get_logger
from .schema import load_schema

log = get_logger("agent_api")


def ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": {"code": code, "message": message, **extra}}


async def run(
    config_path: str = "config.yaml",
    force: bool = False,
) -> dict[str, Any]:
    """Run the full scrape pipeline. Returns per-job results + summary."""
    cfg = load_config(config_path)
    cache = CacheManager(cfg.files.cache_dir)
    schema = load_schema(cfg.files.schema_file)

    try:
        from .pipeline import run_pipeline

        summary = await run_pipeline(cfg, schema, cache, force=force)
        return ok({"summary": summary})
    except Exception as e:
        log.error("run failed: %s", e)
        return err("PIPELINE_ERROR", str(e))
    finally:
        cache.close()


async def discover_departments(
    university: str,
    link: str | None = None,
    config_path: str = "config.yaml",
) -> dict[str, Any]:
    """Discover academic departments for a university via LLM."""
    if not university or not university.strip():
        return err("INVALID_INPUT", "university is required")

    cfg = load_config(config_path)
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


async def export(
    config_path: str = "config.yaml",
    fmt: str = "excel",
    output_path: str | None = None,
) -> dict[str, Any]:
    """Export faculty records to Excel or JSON from the output file."""
    cfg = load_config(config_path)

    if fmt == "json":
        out = Path(output_path or "faculty_data.json")
        try:
            import pandas as pd
            path = Path(cfg.files.output_excel)
            if path.exists():
                df = pd.read_excel(path, sheet_name=0)
                records = df.where(pd.notna(df), None).to_dict(orient="records")
                records = [{str(k): v for k, v in r.items() if v is not None and str(v) != "nan"} for r in records]
            else:
                records = []
            out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            return ok({"format": "json", "path": str(out), "count": len(records)})
        except Exception as e:
            return err("EXPORT_ERROR", str(e))

    schema = load_schema(cfg.files.schema_file)
    from .exporter import export_to_excel

    out_path = output_path or cfg.files.output_excel
    try:
        count = await export_to_excel(None, schema, out_path)
        return ok({"format": "excel", "path": str(out_path), "count": count})
    except Exception as e:
        return err("EXPORT_ERROR", str(e))
