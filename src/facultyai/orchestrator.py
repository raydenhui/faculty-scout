"""Async orchestrator that manages the end-to-end pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .cache import CacheManager
from .config import AppConfig
from .database import Database, _job_id
from .exporter import export_to_excel
from .input_manager import sync_input_excel
from .llm_factory import get_llm
from .logging_config import get_logger
from .schema import Schema
from .scraper_graph import build_agent_graph

log = get_logger("orch")


async def run_pipeline(
    config: AppConfig,
    schema: Schema,
    db: Database,
    cache: CacheManager,
    retry_failed: bool = False,
) -> dict[str, Any]:
    """Run all pending jobs and export results. Returns run summary dict."""
    llm = get_llm(config.llm)
    console = Console()
    log.info("pipeline start  provider=%s model=%s", config.llm.provider, config.llm.model)

    console.print("[bold blue]Orchestrator[/] Loading input from Excel...")
    inserted, deleted = await sync_input_excel(db, config.files.input_excel)
    console.print(f"  Input sync: {inserted} rows kept, {deleted} removed.")

    uni_rows = await db.get_input_universities()
    console.print(f"  Loaded {len(uni_rows)} university entries from DB.")

    # Debug: show all rows with their status
    log.debug("input rows after sync:")
    for r in uni_rows:
        log.debug("  uni=%s dept=%s status=%s link=%s",
                   r["university"], r.get("department"), repr(r.get("status")),
                   (r.get("link") or "")[:50])

    if not uni_rows:
        console.print("[yellow]No university entries to process.[/]")
        return {"total": 0, "successful": 0, "failed": 0}

    discovery_jobs = 0
    scrape_jobs = 0

    async with AsyncSqliteSaver.from_conn_string(str(db.db_path)) as checkpointer:
        agent = build_agent_graph(config, schema, llm, cache, checkpointer=checkpointer)

        for r in uni_rows:
            uni = r["university"]
            dept = r.get("department")
            # Normalize empty-like values to None for discovery triggering
            if dept is not None and str(dept).strip().lower() in ("", "none", "null", "nan"):
                dept = None

            # Skip rows already marked complete in Excel
            excel_status = r.get("status")
            log.debug("status check uni=%s dept=%s raw_status=%s", uni, dept, repr(excel_status))
            if excel_status and str(excel_status).strip().lower() not in ("", "none", "null", "nan"):
                console.print(
                    f"  [dim]Skipping[/] {uni}/{dept or 'All'}: "
                    "already completed (clear status to re-run)"
                )
                continue

            if dept is None and config.department.discovery_enabled:
                await db.upsert_job(uni, None, job_type="discovery", status="pending")
                discovery_jobs += 1
            else:
                link = r.get("link")
                raw_link = str(link).strip() if link else ""
                if raw_link.lower() in ("", "none", "null", "nan"):
                    raw_link = ""
                url = raw_link if raw_link.startswith("http") else None
                await db.upsert_job(uni, dept, job_type="scrape", status="pending",
                                    listing_url=url)
                scrape_jobs += 1

        console.print(f"  Queued {discovery_jobs} discovery jobs, {scrape_jobs} scrape jobs.")

        run_id = await db.start_run()

        discovery_pending = await db.get_jobs_by_status("pending")
        discovery_pending = [j for j in discovery_pending if j["job_type"] == "discovery"]

        for job in discovery_pending:
            await db.update_job_status(job["job_id"], "running")
            try:
                state = {
                    "university": job["university"],
                    "department": None,
                    "need_discovery": True,
                }
                result = await agent.ainvoke(
                    state,
                    {"configurable": {"thread_id": job["job_id"]}},
                )

                departments = result.get("discovered_departments", [])
                console.print(
                    f"[cyan]Discovery[/] {job['university']}: found {len(departments)} departments"
                )

                existing_job_ids = {j["job_id"] for j in await db.list_jobs()}

                for d in departments:
                    d_jid = _job_id(job["university"], d)
                    if d_jid not in existing_job_ids:
                        await db.upsert_job(
                            job["university"], d, job_type="scrape", status="pending"
                        )
                        scrape_jobs += 1

                await db.update_job_status(job["job_id"], "completed")

                # Mark the uni-only row as completed (won't re-discover next time)
                _set_excel_status(
                    config.files.input_excel,
                    job["university"],
                    None,
                    "completed",
                    link=result.get("page_url", ""),
                )

                # Append discovered departments as new rows in the Excel
                _append_departments_to_excel(
                    config.files.input_excel,
                    job["university"],
                    departments,
                )

                # Sync the new rows into DB so they're available for future runs
                await sync_input_excel(db, config.files.input_excel)
            except Exception as e:
                console.print(f"[red]Discovery failed[/] {job['university']}: {e}")
                await db.update_job_status(job["job_id"], "failed", str(e))

        scrape_pending = await db.get_jobs_by_status("pending")
        scrape_pending = [j for j in scrape_pending if j["job_type"] == "scrape"]

        semaphore = asyncio.Semaphore(config.scraping.max_concurrent_jobs)
        successful = 0
        failed = 0

        if scrape_pending:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("[blue]Scraping...", total=len(scrape_pending))

                async def _process_job(job: dict[str, Any]) -> None:
                    nonlocal successful, failed
                    async with semaphore:
                        jid = job["job_id"]
                        await db.update_job_status(jid, "running")
                        result: dict[str, Any] | None = None
                        try:
                            state = {
                                "university": job["university"],
                                "department": job.get("department"),
                                "need_discovery": False,
                                "listing_url": job.get("listing_url"),
                            }

                            # Set progress callback via module-level variable (not serializable)
                            from . import scraper_graph
                            scraper_graph._progress_callback = (
                                lambda pct, msg, _t=task, _p=progress: (
                                    _p.update(_t, completed=pct, description=f"[blue]{msg}")
                                )
                            )

                            result = await agent.ainvoke(
                                state,
                                {"configurable": {"thread_id": jid}},
                            )

                            scraper_graph._progress_callback = None

                            listing_url = result.get("listing_url")
                            if listing_url:
                                await db.upsert_job(
                                    job["university"],
                                    job.get("department"),
                                    listing_url=listing_url,
                                    status="running",
                                )

                            records = result.get("extracted_records", [])
                            for rec in records:
                                _fill_static_fields(rec, job, schema)
                            graph_error = result.get("error")
                            log.info(
                                "job done  uni=%s dept=%s records=%d url=%s error=%s",
                                job["university"],
                                job.get("department"),
                                len(records),
                                result.get("listing_url", "?"),
                                graph_error or "-",
                            )

                            if graph_error:
                                progress.console.print(
                                    f"[yellow]Warning[/] {job['university']}/"
                                    f"{job.get('department', 'All')}: {graph_error}"
                                )
                                if not records:
                                    await db.update_job_status(jid, "failed", graph_error)
                                    failed += 1
                                    _set_excel_status(
                                        config.files.input_excel,
                                        job["university"],
                                        job.get("department"),
                                        f"failed: {graph_error}",
                                        link=result.get("listing_url") or "",
                                    )
                                    progress.update(task, advance=1)
                                    return

                            seen_ids: list[str] = []
                            for rec in records:
                                uv: dict[str, Any] = {}
                                for key in config.output.unique_keys:
                                    val = rec.get(key)
                                    if val:
                                        uv[key] = val
                                await db.upsert_faculty(
                                    job["university"],
                                    job.get("department"),
                                    uv,
                                    rec,
                                    profile_url=rec.get("profile_url") or None,
                                )
                                rid = _build_record_id(
                                    job["university"], job.get("department"), uv
                                )
                                seen_ids.append(rid)

                            await db.mark_not_seen(
                                job["university"], job.get("department"), seen_ids
                            )

                            await db.update_job_status(jid, "completed")
                            successful += 1
                            progress.console.print(
                                f"[green]Scrape[/] {job['university']}/"
                                f"{job.get('department', 'All')}: {len(records)} faculty"
                            )

                            export_count = await export_to_excel(
                                db, schema, config.files.output_excel,
                                upsert_university=job["university"],
                                upsert_department=job.get("department"),
                            )
                            log.info("incremental export: %d rows written for %s/%s",
                                     export_count, job["university"], job.get("department"))

                            # Write completed status to Excel
                            # Write completed status + link to Excel
                            _set_excel_status(
                                config.files.input_excel,
                                job["university"],
                                job.get("department"),
                                "completed",
                                link=result.get("listing_url") or "",
                            )
                        except Exception as e:
                            console.print(
                                f"[red]Scrape failed[/] {job['university']}/"
                                f"{job.get('department', 'All')}: {e}"
                            )
                            await db.update_job_status(jid, "failed", str(e))
                            failed += 1
                            _set_excel_status(
                                config.files.input_excel,
                                job["university"],
                                job.get("department"),
                                f"failed: {str(e)[:100]}",
                                link=result.get("listing_url", "") if result else "",
                            )
                        finally:
                            progress.update(task, advance=1)

                tasks_list = [asyncio.create_task(_process_job(j)) for j in scrape_pending]
                await asyncio.gather(*tasks_list)

        archived = await db.archive_old_not_found(config.output.archive_after_not_found_runs)
        if archived:
            console.print(f"  Archived {len(archived)} stale records.")

        total = discovery_jobs + scrape_jobs
        await db.finish_run(run_id, total, successful, failed)

        return {"total": total, "successful": successful, "failed": failed}


def _build_record_id(university: str, department: str | None, unique_vals: dict[str, Any]) -> str:
    payload = json.dumps(
        {"university": university, "department": department or "", "keys": unique_vals},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _fill_static_fields(rec: dict[str, Any], job: dict[str, Any], schema: Any) -> None:
    meta = {
        "university_name": job["university"],
    }
    for col in schema.static_columns():
        if col.value_from and col.value_from in meta:
            rec[col.name] = meta[col.value_from]


def _set_excel_status(
    excel_path: str,
    university: str,
    department: str | None,
    status: str,
    link: str | None = None,
) -> None:
    """Write status and link back to the input Excel file."""
    from pathlib import Path

    import pandas as pd

    path = Path(excel_path)
    if not path.exists():
        return

    df = pd.read_excel(path, sheet_name=0)
    df = df.where(pd.notna(df), None)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    if "status" not in df.columns:
        df["status"] = None
    df["status"] = df["status"].astype(object)

    if "link" not in df.columns:
        df["link"] = None
    df["link"] = df["link"].astype(object)

    for idx, row in df.iterrows():
        row_uni = str(row["university_name"]).strip()
        row_dept = str(row.get("department_name", "")).strip() if row.get("department_name") else ""
        if row_uni == university and (row_dept == (department or "") or (not row_dept and not department)):
            df.at[idx, "status"] = status
            if link:
                df.at[idx, "link"] = link

    # Restore original column name casing for output
    df.columns = [c.replace("_", " ").title() for c in df.columns]
    df.to_excel(path, index=False)


def _append_departments_to_excel(
    excel_path: str,
    university: str,
    departments: list[str],
) -> None:
    """Add discovered departments as new rows in the Excel input file."""
    from pathlib import Path

    import pandas as pd

    path = Path(excel_path)
    if not path.exists():
        return

    df = pd.read_excel(path, sheet_name=0)
    df = df.where(pd.notna(df), None)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    for d in departments:
        new_row = pd.DataFrame([{
            "university_name": university,
            "department_name": d,
            "status": None,
        }])
        df = pd.concat([df, new_row], ignore_index=True)

    df.columns = [c.replace("_", " ").title() for c in df.columns]
    df.to_excel(path, index=False)
