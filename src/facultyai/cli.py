"""Click-based CLI entry point for FacultyAI."""

from __future__ import annotations

import asyncio
import json
import sys
import warnings

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .cache import CacheManager
from .config import load_config, mask_secrets
from .database import Database
from .lock_manager import LockManager
from .logging_config import configure as configure_logging
from .schema import load_schema

console = Console()


def _run_async(coro):
    """Run an async coroutine with proper event-loop cleanup on Windows."""
    warnings.filterwarnings("ignore", category=ResourceWarning)
    try:
        with asyncio.Runner() as runner:
            return runner.run(coro)
    except KeyboardInterrupt:
        pass


def _emit_json(result: dict) -> None:
    """Print a structured result as JSON to stdout and exit accordingly."""
    click.echo(json.dumps(result, ensure_ascii=False, default=str))
    if not result.get("success", False):
        sys.exit(1)


@click.group()
@click.version_option(__version__, prog_name="facultyai")
def cli() -> None:
    """FacultyAI – AI-driven faculty information scraper."""


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
@click.option("--retry-failed", is_flag=True, default=False, help="Retry previously failed jobs.")
@click.option("--skip-unchanged", is_flag=True, default=False,
              help="Skip jobs where listing page HTML is unchanged from last run.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show info-level logs.")
@click.option("--debug", is_flag=True, default=False, help="Show debug-level logs (implies -v).")
@click.option("--json", "json_out", is_flag=True, default=False, help="Emit machine-readable JSON to stdout.")
def run(config_path: str, retry_failed: bool, skip_unchanged: bool, verbose: bool, debug: bool, json_out: bool) -> None:
    """Start/run all pending jobs."""
    configure_logging(verbose=verbose, debug=debug)
    if json_out:
        from .agent_api import run as api_run

        result = _run_async(api_run(config_path, retry_failed=retry_failed, skip_unchanged=skip_unchanged))
        _emit_json(result)
        return
    _run_with_lock(config_path, retry_failed=retry_failed, skip_unchanged=skip_unchanged)


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
@click.option("--retry-failed", is_flag=True, default=False, help="Also retry previously failed jobs.")
@click.option("--skip-unchanged", is_flag=True, default=False,
              help="Skip jobs where listing page HTML is unchanged from last run.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show info-level logs.")
@click.option("--debug", is_flag=True, default=False, help="Show debug-level logs (implies -v).")
def resume(config_path: str, retry_failed: bool, skip_unchanged: bool, verbose: bool, debug: bool) -> None:
    """Resume incomplete jobs (resets running jobs, optionally retries failed)."""
    configure_logging(verbose=verbose, debug=debug)
    cfg = load_config(config_path)
    lock = LockManager()

    if not lock.acquire():
        console.print("[red]Another facultyai process is already running.[/]")
        sys.exit(1)

    try:

        async def _run() -> None:
            db = Database(cfg.files.database)
            cache = CacheManager(cfg.files.cache_dir)
            schema = load_schema(cfg.files.schema_file)

            async with db:
                try:
                    running = await db.get_jobs_by_status("running")
                    for j in running:
                        await db.update_job_status(j["job_id"], "pending")

                    if retry_failed:
                        failed = await db.get_jobs_by_status("failed")
                        for j in failed:
                            await db.update_job_status(j["job_id"], "pending")

                    from .orchestrator import run_pipeline

                    summary = await run_pipeline(cfg, schema, db, cache,
                                                 retry_failed=retry_failed,
                                                 skip_unchanged=skip_unchanged)
                    if summary["failed"] > 0:
                        sys.exit(1)
                finally:
                    cache.close()

        _run_async(_run())
    finally:
        lock.release()


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show info-level logs.")
@click.option("--debug", is_flag=True, default=False, help="Show debug-level logs (implies -v).")
@click.argument("university")
@click.argument("department", required=False)
def retry(config_path: str, university: str, department: str | None, verbose: bool, debug: bool) -> None:
    """Retry a specific failed job by university (and optional department)."""
    configure_logging(verbose=verbose, debug=debug)
    cfg = load_config(config_path)
    lock = LockManager()

    if not lock.acquire():
        console.print("[red]Another facultyai process is already running.[/]")
        sys.exit(1)

    try:

        async def _run() -> None:
            db = Database(cfg.files.database)
            cache = CacheManager(cfg.files.cache_dir)
            schema = load_schema(cfg.files.schema_file)

            async with db:
                try:
                    from .database import _job_id

                    jid = _job_id(university, department)
                    job = await db.get_job(jid)
                    if not job:
                        console.print(
                            f"[red]No job found for {university}/{department or 'All'}[/]"
                        )
                        return

                    if job["status"] not in ("failed", "completed"):
                        console.print(
                            f"[yellow]Job is not in a retryable state (status: {job['status']}). "
                            f"Use 'facultyai run --retry-failed' instead.[/]"
                        )
                        return

                    await db.update_job_status(jid, "pending")

                    from .orchestrator import run_pipeline

                    summary = await run_pipeline(
                        cfg, schema, db, cache, retry_failed=True
                    )
                    if summary["failed"] > 0:
                        sys.exit(1)
                finally:
                    cache.close()

        _run_async(_run())
    finally:
        lock.release()


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
@click.option("--json", "json_out", is_flag=True, default=False, help="Emit machine-readable JSON to stdout.")
def status(config_path: str, json_out: bool) -> None:
    """Show job statuses and run history."""
    if json_out:
        from .agent_api import get_status

        result = _run_async(get_status(config_path))
        _emit_json(result)
        return

    cfg = load_config(config_path)

    async def _run() -> None:
        db = Database(cfg.files.database)
        async with db:
            jobs = await db.list_jobs()
            if not jobs:
                console.print("[yellow]No jobs yet. Run 'facultyai run' first.[/]")
            else:
                table = Table(title="Job Status")
                table.add_column("University", style="cyan")
                table.add_column("Department")
                table.add_column("Type")
                table.add_column("Status")
                table.add_column("URL", style="dim")
                table.add_column("Error", style="red")

                status_styles = {
                    "completed": "[green]completed[/]",
                    "running": "[yellow]running[/]",
                    "failed": "[red]failed[/]",
                    "pending": "[dim]pending[/]",
                }

                for j in jobs:
                    dept = j["department"] or "All"
                    url = j["listing_url"] or ""
                    if len(url) > 40:
                        url = url[:37] + "..."
                    table.add_row(
                        j["university"],
                        dept,
                        j["job_type"],
                        status_styles.get(j["status"], j["status"]),
                        url,
                        (j["error"] or "")[:60],
                    )

                console.print(table)

                counts = {}
                for j in jobs:
                    s = j["status"]
                    counts[s] = counts.get(s, 0) + 1

                console.print(
                    f"\n  [green]{counts.get('completed', 0)} completed[/], "
                    f"[yellow]{counts.get('running', 0)} running[/], "
                    f"[dim]{counts.get('pending', 0)} pending[/], "
                    f"[red]{counts.get('failed', 0)} failed[/]"
                )

            # Run history
            assert db._connection is not None
            async with db._connection.execute(
                "SELECT * FROM run_history ORDER BY id DESC LIMIT 5"
            ) as cursor:
                history = [dict(r) for r in await cursor.fetchall()]

            if history:
                console.print()
                htable = Table(title="Run History (last 5)")
                htable.add_column("ID")
                htable.add_column("Started")
                htable.add_column("Finished")
                htable.add_column("Total")
                htable.add_column("Success")
                htable.add_column("Failed")

                for h in history:
                    started = (h["started_at"] or "")[:16]
                    finished = (h["finished_at"] or "")[:16]
                    htable.add_row(
                        str(h["id"]),
                        started,
                        finished,
                        str(h["total_jobs"]) if h["total_jobs"] is not None else "-",
                        str(h["successful"]) if h["successful"] is not None else "-",
                        str(h["failed"]) if h["failed"] is not None else "-",
                    )
                console.print(htable)

    _run_async(_run())


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
@click.option("--format", "fmt", type=click.Choice(["excel", "json"]), default="excel", help="Output format.")
@click.option("--output", "output_path", default=None, help="Output file path (overrides config).")
@click.option("--json", "json_out", is_flag=True, default=False, help="Emit machine-readable JSON result to stdout.")
def export(config_path: str, fmt: str, output_path: str | None, json_out: bool) -> None:
    """Regenerate output (Excel or JSON) from database."""
    from .agent_api import export as api_export

    result = _run_async(api_export(config_path, fmt=fmt, output_path=output_path))
    if json_out:
        _emit_json(result)
        return
    if result.get("success"):
        d = result["data"]
        console.print(f"[green]{d['path']}[/] written ({d['count']} records).")
    else:
        console.print(f"[red]Export failed:[/] {result['error']['message']}")
        sys.exit(1)


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show info-level logs.")
@click.option("--debug", is_flag=True, default=False, help="Show debug-level logs (implies -v).")
@click.option("--json", "json_out", is_flag=True, default=False, help="Emit machine-readable JSON to stdout.")
def discover(config_path: str, verbose: bool, debug: bool, json_out: bool) -> None:
    """Run department discovery for university-only entries in the input Excel."""
    configure_logging(verbose=verbose, debug=debug)
    cfg = load_config(config_path)
    lock = LockManager()

    if not lock.acquire():
        if json_out:
            _emit_json({"success": False, "error": {"code": "LOCKED",
                        "message": "Another facultyai process is already running."}})
        console.print("[red]Another facultyai process is already running.[/]")
        sys.exit(1)

    try:

        async def _run() -> dict:
            db = Database(cfg.files.database)
            load_schema(cfg.files.schema_file)
            collected: list[dict] = []

            def _p(*args, **kwargs):
                if not json_out:
                    console.print(*args, **kwargs)

            async with db:
                from .input_manager import sync_input_excel
                from .llm_factory import get_llm

                _p("[bold blue]Discover[/] Loading input from Excel...")
                inserted, deleted = await sync_input_excel(db, cfg.files.input_excel)
                _p(f"  Input sync: {inserted} rows kept, {deleted} removed.")

                uni_rows = await db.get_input_universities()
                uni_rows = [
                    r for r in uni_rows
                    if r.get("department") is None
                    or str(r.get("department", "")).strip().lower() in ("", "none", "null", "nan")
                ]

                if not uni_rows:
                    _p("[yellow]No university-only entries to discover.[/]")
                    return {"universities": collected}

                # Skip rows already marked completed
                to_process = []
                for r in uni_rows:
                    s = r.get("status")
                    if s and str(s).strip().lower() not in ("", "none", "null", "nan"):
                        _p(f"  [dim]Skipping[/] {r['university']}: already completed")
                    else:
                        to_process.append(r)
                uni_rows = to_process

                _p(f"  Found {len(uni_rows)} universities to discover departments for.")

                llm = get_llm(cfg.llm)

                for r in uni_rows:
                    uni = r["university"]
                    _p(f"\n[cyan]Discovering departments for {uni}...[/]")

                    from .scraper_graph import _discover_departments_impl

                    link = r.get("link")
                    state = {
                        "university": uni,
                        "page_url": str(link).strip() if link and str(link).strip().startswith("http") else "",
                    }
                    result = await _discover_departments_impl(state, cfg, llm, None)
                    departments = result.get("discovered_departments", [])
                    collected.append({"university": uni, "departments": departments})

                    if departments:
                        _p(f"  [green]Found {len(departments)} departments:[/]")
                        for d in departments:
                            _p(f"    - {d}")

                        from .orchestrator import _append_departments_to_excel, _set_excel_status

                        _set_excel_status(cfg.files.input_excel, uni, None, "completed")
                        _append_departments_to_excel(cfg.files.input_excel, uni, departments)
                        await sync_input_excel(db, cfg.files.input_excel)
                        _p(f"  [dim]Updated {cfg.files.input_excel} with {len(departments)} departments[/]")
                    else:
                        _p("  [yellow]No departments found[/]")

            return {"universities": collected}

        data = _run_async(_run())
        if json_out:
            _emit_json({"success": True, "data": data})
    finally:
        lock.release()


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show info-level logs.")
@click.option("--debug", is_flag=True, default=False, help="Show debug-level logs (implies -v).")
@click.option("--json", "json_out", is_flag=True, default=False, help="Emit machine-readable JSON to stdout.")
def url_only(config_path: str, verbose: bool, debug: bool, json_out: bool) -> None:
    """Discover listing page URLs for department entries missing a link in the input Excel."""
    configure_logging(verbose=verbose, debug=debug)
    cfg = load_config(config_path)
    lock = LockManager()

    if not lock.acquire():
        if json_out:
            _emit_json({"success": False, "error": {"code": "LOCKED",
                        "message": "Another facultyai process is already running."}})
        console.print("[red]Another facultyai process is already running.[/]")
        sys.exit(1)

    try:

        async def _run() -> dict:
            db = Database(cfg.files.database)
            load_schema(cfg.files.schema_file)
            collected: list[dict] = []

            def _p(*args, **kwargs):
                if not json_out:
                    console.print(*args, **kwargs)

            async with db:
                from .input_manager import sync_input_excel
                from .llm_factory import get_llm

                _p("[bold blue]URL Discovery[/] Loading input from Excel...")
                inserted, deleted = await sync_input_excel(db, cfg.files.input_excel)
                _p(f"  Input sync: {inserted} rows kept, {deleted} removed.")

                all_rows = await db.get_input_universities()
                to_process = []
                for r in all_rows:
                    dept = r.get("department")
                    link = r.get("link")
                    if dept and str(dept).strip().lower() not in ("", "none", "null", "nan"):
                        raw_link = str(link).strip() if link else ""
                        if raw_link.lower() in ("", "none", "null", "nan"):
                            s = r.get("status")
                            if s and str(s).strip().lower() not in ("", "none", "null", "nan"):
                                _p(f"  [dim]Skipping[/] {r['university']}/{dept}: already completed")
                            else:
                                to_process.append(r)

                if not to_process:
                    _p("[yellow]No department entries missing a link to process.[/]")
                    return {"results": collected}

                _p(f"  Found {len(to_process)} department entries to discover URLs for.")

                llm = get_llm(cfg.llm)

                for r in to_process:
                    uni = r["university"]
                    dept = r["department"]
                    _p(f"\n[cyan]Discovering URL for {uni} / {dept}...[/]")

                    from .scraper_graph import _discover_url_impl

                    state = {
                        "university": uni,
                        "department": dept,
                        "listing_url": "",
                    }
                    result = await _discover_url_impl(state, cfg, llm, None)
                    url = result.get("listing_url")
                    error = result.get("error")

                    if url and str(url).startswith("http"):
                        _p(f"  [green]Found: {url}[/]")
                        collected.append({"university": uni, "department": dept, "url": url})

                        from .orchestrator import _set_excel_status

                        _set_excel_status(cfg.files.input_excel, uni, dept, "url-found", link=url)
                        _p(f"  [dim]Updated {cfg.files.input_excel} with URL[/]")
                    else:
                        _p(f"  [yellow]No URL found[/] {('— ' + error) if error else ''}")
                        collected.append({"university": uni, "department": dept, "url": None, "error": error})

            return {"results": collected}

        data = _run_async(_run())
        if json_out:
            _emit_json({"success": True, "data": data})
    finally:
        lock.release()


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
@click.argument("university")
@click.argument("department", required=False)
@click.option("--link", default=None, help="Pre-filled listing URL for this target.")
@click.option("--json", "json_out", is_flag=True, default=False, help="Emit JSON to stdout.")
def add_target(
    config_path: str, university: str, department: str | None, link: str | None, json_out: bool
) -> None:
    """Add a scrape target (university + optional department) to the queue."""
    from .agent_api import add_target as api_add

    result = _run_async(api_add(university, department, link, config_path=config_path))
    if json_out:
        _emit_json(result)
        return
    if result.get("success"):
        d = result["data"]
        console.print(f"[green]Added[/] {d['university']}/{d['department'] or 'All'}")
    else:
        console.print(f"[red]Error:[/] {result['error']['message']}")
        sys.exit(1)


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
@click.option("--json", "json_out", is_flag=True, default=False, help="Emit JSON to stdout.")
def targets(config_path: str, json_out: bool) -> None:
    """List all scrape targets in the input queue."""
    from .agent_api import list_targets

    result = _run_async(list_targets(config_path))
    if json_out:
        _emit_json(result)
        return
    if result.get("success"):
        rows = result["data"]["targets"]
        if not rows:
            console.print("[yellow]No targets configured.[/]")
        else:
            table = Table(title="Scrape Targets")
            table.add_column("University", style="cyan")
            table.add_column("Department")
            table.add_column("Status")
            table.add_column("Link", style="dim")
            for t in rows:
                table.add_row(
                    t["university"], t["department"] or "All",
                    t["status"] or "", (t["link"] or "")[:50],
                )
            console.print(table)


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
@click.option("--university", default=None, help="Filter by university.")
@click.option("--department", default=None, help="Filter by department.")
@click.option("--json", "json_out", is_flag=True, default=False, help="Emit JSON to stdout.")
def results(config_path: str, university: str | None, department: str | None, json_out: bool) -> None:
    """Show extracted faculty records from the database."""
    from .agent_api import get_results

    result = _run_async(get_results(config_path, university, department))
    if json_out:
        _emit_json(result)
        return
    if result.get("success"):
        d = result["data"]
        console.print(f"[green]{d['count']}[/] faculty records.")
        for rec in d["records"][:20]:
            name = rec.get("English Full Name", "?")
            email = rec.get("Email", "-")
            console.print(f"  {name}  [dim]{email}[/]")
        if d["count"] > 20:
            console.print(f"  [dim]... and {d['count'] - 20} more (use --json for all)[/]")


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
def chat(config_path: str) -> None:
    """Interactive chat agent for configuration & queries."""
    cfg = load_config(config_path)

    async def _run() -> None:
        db = Database(cfg.files.database)
        schema = load_schema(cfg.files.schema_file)
        async with db:
            from .chat import run_chat

            await run_chat(cfg, schema, db)

    _run_async(_run())


@cli.group()
def config() -> None:
    """Configuration commands."""


@config.command("validate")
@click.option("--path", default="config.yaml", help="Path to config file.")
def config_validate(path: str) -> None:
    """Validate config.yaml."""
    try:
        cfg = load_config(path)
        console.print(f"[green]Config valid.[/] version={cfg.version} provider={cfg.llm.provider}")
    except Exception as e:
        console.print(f"[red]Config invalid:[/] {e}")
        sys.exit(1)


@config.command("show")
@click.option("--path", default="config.yaml", help="Path to config file.")
def config_show(path: str) -> None:
    """Show current config (with secrets masked)."""
    cfg = load_config(path)
    console.print_json(json.dumps(mask_secrets(cfg)))


def _run_with_lock(config_path: str, retry_failed: bool = False, skip_unchanged: bool = False) -> None:
    cfg = load_config(config_path)
    lock = LockManager()

    if not lock.acquire():
        console.print("[red]Another facultyai process is already running.[/]")
        sys.exit(1)

    try:

        async def _run() -> None:
            db = Database(cfg.files.database)
            cache = CacheManager(cfg.files.cache_dir)
            schema = load_schema(cfg.files.schema_file)

            async with db:
                try:
                    from .orchestrator import run_pipeline

                    summary = await run_pipeline(
                        cfg, schema, db, cache, retry_failed=retry_failed,
                        skip_unchanged=skip_unchanged,
                    )
                    if summary["failed"] > 0:
                        sys.exit(1)
                finally:
                    cache.close()

        _run_async(_run())
    finally:
        lock.release()


if __name__ == "__main__":
    cli()
