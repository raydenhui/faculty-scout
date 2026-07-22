"""In-memory pipeline: reads Excel → scrapes → exports incrementally. No database.

Replaces the old orchestrator.py which required SQLite for job queueing,
resume, and faculty record storage. The Excel file is now the sole source of
truth for both input (targets) and output (records).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pandas as pd
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .cache import CacheManager
from .config import AppConfig
from .exporter import export_records
from .llm_factory import get_llm
from .logging_config import get_logger
from .schema import Schema
from .scraper_graph import build_agent_graph

log = get_logger("pipeline")

_UNI_COL = "university_name"
_DEPT_COL = "department_name"
_LINK_COL = "link"
_STATUS_COL = "status"


def read_targets(excel_path: str | Path) -> list[dict[str, Any]]:
    """Read scrape targets from the input Excel file. Returns list of target dicts."""
    path = Path(excel_path)
    if not path.exists():
        return []

    df = pd.read_excel(path, sheet_name=0)
    df = df.where(pd.notna(df), None)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    if _UNI_COL not in df.columns:
        raise ValueError(f"Missing required column: {_UNI_COL}")

    targets: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        uni = row.get(_UNI_COL)
        if uni is None or not str(uni).strip():
            continue
        uni = str(uni).strip()

        dept = None
        if _DEPT_COL in df.columns and row[_DEPT_COL] is not None:
            dept_val = str(row[_DEPT_COL]).strip()
            if dept_val.lower() not in ("", "none", "null", "nan"):
                dept = dept_val

        link = None
        if _LINK_COL in df.columns and row.get(_LINK_COL) is not None:
            link_val = str(row[_LINK_COL]).strip()
            if link_val.lower() not in ("", "none", "null", "nan") and link_val.startswith("http"):
                link = link_val

        status = None
        if _STATUS_COL in df.columns:
            raw = row.get(_STATUS_COL)
            if raw is not None and str(raw).strip().lower() not in ("", "none", "null", "nan"):
                status = str(raw).strip()

        targets.append({
            "university": uni,
            "department": dept,
            "link": link,
            "status": status,
            "_row_index": _,
        })

    return targets


def _set_excel_status(
    excel_path: str | Path,
    university: str,
    department: str | None,
    status: str,
    link: str | None = None,
) -> None:
    """Write status and link back to the input Excel file."""
    path = Path(excel_path)
    if not path.exists():
        return

    df = pd.read_excel(path, sheet_name=0)
    df = df.where(pd.notna(df), None)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    if _STATUS_COL not in df.columns:
        df[_STATUS_COL] = None
    df[_STATUS_COL] = df[_STATUS_COL].astype(object)

    if _LINK_COL not in df.columns:
        df[_LINK_COL] = None
    df[_LINK_COL] = df[_LINK_COL].astype(object)

    for idx, row in df.iterrows():
        row_uni = str(row[_UNI_COL]).strip()
        row_dept = None
        if _DEPT_COL in df.columns and row.get(_DEPT_COL) is not None:
            row_dept = str(row[_DEPT_COL]).strip()
            if row_dept.lower() in ("", "none", "null", "nan"):
                row_dept = None
        if row_uni == university and ((row_dept or "") == (department or "")):
            df.at[idx, _STATUS_COL] = status
            if link:
                df.at[idx, _LINK_COL] = link

    df.columns = [c.replace("_", " ").title() for c in df.columns]
    df.to_excel(path, index=False)


def _append_departments_to_excel(
    excel_path: str | Path,
    university: str,
    departments: list[str],
) -> None:
    """Add discovered departments as new rows in the Excel input file."""
    path = Path(excel_path)
    if not path.exists():
        return

    df = pd.read_excel(path, sheet_name=0)
    df = df.where(pd.notna(df), None)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    for d in departments:
        new_row = pd.DataFrame([{_UNI_COL: university, _DEPT_COL: d}])
        df = pd.concat([df, new_row], ignore_index=True)

    df.columns = [c.replace("_", " ").title() for c in df.columns]
    df.to_excel(path, index=False)


async def run_pipeline(
    config: AppConfig,
    schema: Schema,
    cache: CacheManager,
    skip_unchanged: bool = False,
) -> dict[str, Any]:
    """Run all pending targets and export results incrementally. Returns summary dict."""
    llm = get_llm(config.llm)
    console = Console()
    log.info("pipeline start  provider=%s model=%s skip_unchanged=%s",
             config.llm.provider, config.llm.model, skip_unchanged)

    console.print("[bold blue]Pipeline[/] Loading input from Excel...")
    targets = read_targets(config.files.input_excel)
    console.print(f"  Loaded {len(targets)} targets from {config.files.input_excel}.")

    if not targets:
        console.print("[yellow]No targets to process.[/]")
        return {"total": 0, "successful": 0, "failed": 0, "skipped": 0}

    agent = build_agent_graph(config, schema, llm, cache, checkpointer=None,
                              skip_unchanged=skip_unchanged)

    # ---- Discovery phase ----
    discovery_count = 0
    for t in targets:
        if t["status"] and str(t["status"]).lower() not in ("skipped",):
            continue
        if t["department"] is None and config.department.discovery_enabled:
            console.print(f"[cyan]Discovery[/] {t['university']} — searching for departments...")
            try:
                state: dict[str, Any] = {
                    "university": t["university"],
                    "department": None,
                    "need_discovery": True,
                    "page_url": t["link"] or "",
                }
                result = await agent.ainvoke(state)
                departments = result.get("discovered_departments", [])
                console.print(f"  Found {len(departments)} departments")
                _set_excel_status(config.files.input_excel, t["university"], None,
                                  "completed", link=result.get("page_url", ""))
                if departments:
                    _append_departments_to_excel(config.files.input_excel, t["university"], departments)
                    discovery_count += 1
            except Exception as e:
                console.print(f"[red]Discovery failed[/] {t['university']}: {e}")

    if discovery_count:
        console.print(f"  Discovered departments for {discovery_count} universities. Reloading targets...")
        targets = read_targets(config.files.input_excel)

    # ---- Scrape phase ----
    scrape_targets = [t for t in targets if t["department"] is not None]
    pending = [t for t in scrape_targets
               if not t["status"] or str(t["status"]).lower() in ("", "none", "null", "nan", "skipped")]

    if not pending:
        console.print("[yellow]No pending scrape targets.[/]")
        return {"total": 0, "successful": 0, "failed": 0, "skipped": 0}

    semaphore = asyncio.Semaphore(config.scraping.max_concurrent_jobs)
    all_records: dict[str, list[dict[str, Any]]] = {}
    successful = 0
    failed = 0
    skipped = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[blue]Scraping...", total=len(pending))

        async def _process_target(t: dict[str, Any]) -> None:
            nonlocal successful, failed, skipped
            async with semaphore:
                try:
                    from . import scraper_graph
                    scraper_graph._progress_callback = (
                        lambda pct, msg, _t=task, _p=progress: (
                            _p.update(_t, completed=pct, description=f"[blue]{msg}")
                        )
                    )

                    state: dict[str, Any] = {
                        "university": t["university"],
                        "department": t["department"],
                        "need_discovery": False,
                        "listing_url": t["link"],
                        "skip_unchanged": skip_unchanged,
                    }
                    result = await agent.ainvoke(state)
                    scraper_graph._progress_callback = None

                    if result.get("skipped"):
                        progress.console.print(
                            f"[dim]Skipped[/] {t['university']}/{t['department']}: page unchanged"
                        )
                        _set_excel_status(config.files.input_excel, t["university"],
                                          t["department"], "Skipped")
                        skipped += 1
                        progress.update(task, advance=1)
                        return

                    records = result.get("extracted_records", [])
                    for rec in records:
                        _fill_static_fields(rec, t, schema)

                    graph_error = result.get("error")
                    log.info("target done  uni=%s dept=%s records=%d url=%s error=%s",
                             t["university"], t["department"], len(records),
                             result.get("listing_url", "?"), graph_error or "-")

                    if graph_error and not records:
                        progress.console.print(
                            f"[red]Failed[/] {t['university']}/{t['department']}: {graph_error}"
                        )
                        _set_excel_status(config.files.input_excel, t["university"],
                                          t["department"], f"failed: {graph_error}",
                                          link=result.get("listing_url") or "")
                        failed += 1
                        progress.update(task, advance=1)
                        return

                    key = f"{t['department']}/{t['university']}"
                    all_records[key] = records
                    successful += 1

                    progress.console.print(
                        f"[green]Scraped[/] {t['university']}/{t['department']}: {len(records)} faculty"
                    )

                    _set_excel_status(config.files.input_excel, t["university"],
                                      t["department"], "completed",
                                      link=result.get("listing_url") or "")

                except Exception as e:
                    progress.console.print(
                        f"[red]Scrape failed[/] {t['university']}/{t['department']}: {e}"
                    )
                    _set_excel_status(config.files.input_excel, t["university"],
                                      t["department"], f"failed: {str(e)[:100]}")
                    failed += 1
                finally:
                    progress.update(task, advance=1)

        scrape_tasks = [asyncio.create_task(_process_target(t)) for t in pending]
        await asyncio.gather(*scrape_tasks)

    # ---- Write all results incrementally to Excel ----
    if all_records:
        console.print("[bold]Writing results to Excel...[/]")
        for key, records in all_records.items():
            dept, uni = key.split("/", 1)
            export_records(
                records, schema, Path(config.files.output_excel),
                source_university=uni,
                source_department=dept,
            )
        console.print(f"  Wrote results for {len(all_records)} departments.")

    total = len(pending)
    return {"total": total, "successful": successful, "failed": failed, "skipped": skipped}


def _fill_static_fields(rec: dict[str, Any], target: dict[str, Any], schema: Any) -> None:
    meta = {
        "university_name": target["university"],
        "department": target.get("department", ""),
        "listing_url": target.get("link", ""),
    }
    for col in schema.static_columns():
        if col.value_from and col.value_from in meta:
            rec[col.name] = meta[col.value_from]
    for col in schema.fallback_columns():
        current = rec.get(col.name)
        if current is None or (isinstance(current, str) and current.strip() == ""):
            if col.value_from and col.value_from in meta:
                rec[col.name] = meta[col.value_from]
