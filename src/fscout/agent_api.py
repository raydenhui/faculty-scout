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


async def clear_and_run(
    config_path: str = "config.yaml",
    force: bool = False,
) -> dict[str, Any]:
    """Clear all target statuses in the input Excel, then run the full pipeline."""
    cfg = load_config(config_path)
    cache = CacheManager(cfg.files.cache_dir)
    schema = load_schema(cfg.files.schema_file)

    try:
        from .pipeline import clear_all_status, run_pipeline

        cleared = clear_all_status(cfg.files.input_excel)
        summary = await run_pipeline(cfg, schema, cache, force=force)
        return ok({"cleared": cleared, "summary": summary})
    except Exception as e:
        log.error("clear_and_run failed: %s", e)
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

    try:
        llm = get_llm(cfg.llm)
    except Exception as e:
        return err("LLM_SETUP_ERROR", str(e))

    try:
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
    except Exception as e:
        return err("DISCOVERY_ERROR", str(e))


async def add_target(
    university: str,
    department: str | None = None,
    link: str | None = None,
    config_path: str = "config.yaml",
) -> dict[str, Any]:
    """Add a scrape target (university + optional department + link) to the input Excel."""
    if not university or not university.strip():
        return err("INVALID_INPUT", "university is required")

    cfg = load_config(config_path)
    path = Path(cfg.files.input_excel)

    try:
        import pandas as pd

        university = university.strip()
        dept = department.strip() if department and department.strip() else None

        if path.exists():
            df = pd.read_excel(path, sheet_name=0)
            df = df.where(pd.notna(df), None)
            df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

            new_row: dict[str, Any] = {"university_name": university}
            if "department_name" in df.columns:
                new_row["department_name"] = dept
            if "link" in df.columns:
                new_row["link"] = link.strip() if link else None
            if "status" in df.columns:
                new_row["status"] = None

            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            df.columns = [c.replace("_", " ").title() for c in df.columns]
        else:
            cols = ["University Name"]
            vals: list[Any] = [university]
            if dept:
                cols.append("Department Name")
                vals.append(dept)
            if link:
                cols.append("Link")
                vals.append(link.strip())
            df = pd.DataFrame([vals], columns=cols)

        df.to_excel(path, index=False)
        return ok({"university": university, "department": dept, "link": link})
    except Exception as e:
        return err("ADD_TARGET_ERROR", str(e))


async def list_targets(config_path: str = "config.yaml") -> dict[str, Any]:
    """List all configured scrape targets and their status from the input Excel."""
    cfg = load_config(config_path)
    try:
        from .pipeline import read_targets

        targets = read_targets(cfg.files.input_excel)
        status_counts: dict[str, int] = {}
        for t in targets:
            st = t.get("status") or "pending"
            status_counts[st] = status_counts.get(st, 0) + 1

        return ok({
            "targets": [{k: v for k, v in t.items() if k != "_row_index"} for t in targets],
            "total": len(targets),
            "status_counts": status_counts,
        })
    except Exception as e:
        return err("LIST_TARGETS_ERROR", str(e))


async def get_status(config_path: str = "config.yaml") -> dict[str, Any]:
    """Get current job statuses, summary counts, and recent run history."""
    cfg = load_config(config_path)
    try:
        from .pipeline import read_targets

        targets = read_targets(cfg.files.input_excel)
        stats = {"completed": 0, "failed": 0, "skipped": 0, "pending": 0, "total": len(targets)}
        failed_details: list[dict[str, Any]] = []

        for t in targets:
            st = str(t.get("status", "")).lower() if t.get("status") else ""
            if "failed" in st or "error" in st:
                stats["failed"] += 1
                failed_details.append({
                    "university": t["university"],
                    "department": t.get("department"),
                    "error": st,
                })
            elif st == "skipped":
                stats["skipped"] += 1
            elif st == "completed":
                stats["completed"] += 1
            else:
                stats["pending"] += 1

        return ok({"stats": stats, "failed_details": failed_details})
    except Exception as e:
        return err("STATUS_ERROR", str(e))


async def get_results(
    config_path: str = "config.yaml",
    university: str | None = None,
    department: str | None = None,
) -> dict[str, Any]:
    """Get extracted faculty records, optionally filtered by university/department."""
    cfg = load_config(config_path)
    try:
        import pandas as pd

        path = Path(cfg.files.output_excel)
        if not path.exists():
            return ok({"records": [], "total": 0})

        df = pd.read_excel(path, sheet_name=0)
        df = df.where(pd.notna(df), None)
        records: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            rec = {}
            for col_name, val in row.items():
                if col_name.startswith("_") and col_name.endswith("_source_key"):
                    continue
                if val is not None and str(val) not in ("", "nan", "None"):
                    rec[str(col_name)] = val
            if not rec:
                continue
            if university and university.strip():
                inst = str(rec.get("Institution", rec.get("institution", "")))
                if inst.lower() != university.strip().lower():
                    continue
            if department and department.strip():
                dept = str(rec.get("Department", rec.get("department", "")))
                if dept.lower() != department.strip().lower():
                    continue
            records.append(rec)

        return ok({"records": records, "total": len(records)})
    except Exception as e:
        return err("RESULTS_ERROR", str(e))


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
