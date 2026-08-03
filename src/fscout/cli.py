"""Click-based CLI entry point for fscout."""

from __future__ import annotations

import asyncio
import json
import sys
import warnings

import click
from rich.console import Console

from . import __version__
from .cache import CacheManager
from .config import load_config, mask_secrets
from .logging_config import configure as configure_logging
from .schema import load_schema

console = Console()


def _run_async(coro):
    """Run an async coroutine with proper event-loop cleanup on Windows."""
    warnings.filterwarnings("ignore", category=ResourceWarning)
    try:
        with asyncio.Runner() as runner:
            result = runner.run(coro)
            try:
                runner.run(asyncio.sleep(0))
            except Exception:
                pass
            return result
    except KeyboardInterrupt:
        pass


def _emit_json(result: dict) -> None:
    """Print a structured result as JSON to stdout and exit accordingly."""
    click.echo(json.dumps(result, ensure_ascii=False, default=str))
    if not result.get("success", False):
        sys.exit(1)


@click.group()
@click.version_option(__version__, prog_name="fscout")
def cli() -> None:
    """fscout – AI-driven faculty information scraper."""


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
@click.option("--force", is_flag=True, default=False,
              help="Always re-scrape even if page content is unchanged.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show info-level logs.")
@click.option("--debug", is_flag=True, default=False, help="Show debug-level logs (implies -v).")
@click.option("--json", "json_out", is_flag=True, default=False, help="Emit machine-readable JSON to stdout.")
def run(config_path: str, force: bool, verbose: bool, debug: bool, json_out: bool) -> None:
    """Run all pending scrape targets from the input Excel.

    By default, pages whose HTML is unchanged from the previous run are skipped.
    Use --force to always re-scrape regardless of cache.
    """
    configure_logging(verbose=verbose, debug=debug)
    if json_out:
        from .agent_api import run as api_run

        result = _run_async(api_run(config_path, force=force))
        _emit_json(result)
        return

    async def _do_run() -> None:
        cfg = load_config(config_path)
        cache = CacheManager(cfg.files.cache_dir)
        schema = load_schema(cfg.files.schema_file)
        try:
            from .pipeline import run_pipeline

            summary = await run_pipeline(cfg, schema, cache, force=force)
            if summary["failed"] > 0:
                sys.exit(1)
        finally:
            cache.close()

    _run_async(_do_run())


@cli.command("clear-and-run")
@click.option("--config-path", default="config.yaml", help="Path to config file.")
@click.option("--force", is_flag=True, default=False,
              help="Always re-scrape even if page content is unchanged.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show info-level logs.")
@click.option("--debug", is_flag=True, default=False, help="Show debug-level logs (implies -v).")
@click.option("--json", "json_out", is_flag=True, default=False, help="Emit machine-readable JSON to stdout.")
def clear_and_run(config_path: str, force: bool, verbose: bool, debug: bool, json_out: bool) -> None:
    """Clear all statuses in the input Excel, then run all targets."""
    configure_logging(verbose=verbose, debug=debug)
    if json_out:
        from .agent_api import clear_and_run as api_clear_and_run

        result = _run_async(api_clear_and_run(config_path, force=force))
        _emit_json(result)
        return

    async def _do_run() -> None:
        cfg = load_config(config_path)
        cache = CacheManager(cfg.files.cache_dir)
        schema = load_schema(cfg.files.schema_file)
        try:
            from .pipeline import clear_all_status, run_pipeline

            cleared = clear_all_status(cfg.files.input_excel)
            console.print(f"[yellow]Cleared status for {cleared} target rows.[/]")
            summary = await run_pipeline(cfg, schema, cache, force=force)
            if summary["failed"] > 0:
                sys.exit(1)
        finally:
            cache.close()

    _run_async(_do_run())


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

    async def _run() -> dict:
        from .llm_factory import get_llm
        from .pipeline import _append_departments_to_excel, _set_excel_status, read_targets
        from .scraper_graph import _discover_departments_impl

        targets = read_targets(cfg.files.input_excel)
        uni_targets = [t for t in targets if t["department"] is None
                       and not (t["status"] and str(t["status"]).strip().lower()
                                not in ("", "none", "null", "nan", "skipped"))]

        if not uni_targets:
            console.print("[yellow]No university-only entries to discover.[/]")
            return {"universities": []}

        llm = get_llm(cfg.llm)
        collected: list[dict] = []

        for t in uni_targets:
            uni = t["university"]
            console.print(f"\n[cyan]Discovering departments for {uni}...[/]")
            link = t.get("link") or ""
            state = {
                "university": uni,
                "page_url": link if link.startswith("http") else "",
            }
            result = await _discover_departments_impl(state, cfg, llm, None)
            departments = result.get("discovered_departments", [])
            collected.append({"university": uni, "departments": departments})

            if departments:
                console.print(f"  [green]Found {len(departments)} departments:[/]")
                for d in departments:
                    console.print(f"    - {d}")
                _set_excel_status(cfg.files.input_excel, uni, None, "completed")
                _append_departments_to_excel(cfg.files.input_excel, uni, departments)
            else:
                console.print("  [yellow]No departments found[/]")

        return {"universities": collected}

    data = _run_async(_run())
    if json_out:
        _emit_json({"success": True, "data": data})


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show info-level logs.")
@click.option("--debug", is_flag=True, default=False, help="Show debug-level logs (implies -v).")
@click.option("--json", "json_out", is_flag=True, default=False, help="Emit machine-readable JSON to stdout.")
def url_only(config_path: str, verbose: bool, debug: bool, json_out: bool) -> None:
    """Discover listing page URLs for department entries missing a link in the input Excel."""
    configure_logging(verbose=verbose, debug=debug)
    cfg = load_config(config_path)

    async def _run() -> dict:
        from .llm_factory import get_llm
        from .pipeline import _set_excel_status, read_targets
        from .scraper_graph import _discover_url_impl

        all_targets = read_targets(cfg.files.input_excel)
        to_process = []
        for t in all_targets:
            dept = t["department"]
            if not dept:
                continue
            link = t.get("link") or ""
            if link.startswith("http"):
                continue
            s = t.get("status")
            if s and str(s).strip().lower() not in ("", "none", "null", "nan", "skipped"):
                continue
            to_process.append(t)

        if not to_process:
            console.print("[yellow]No department entries missing a link to process.[/]")
            return {"results": []}

        console.print(f"  Found {len(to_process)} department entries to discover URLs for.")
        llm = get_llm(cfg.llm)
        collected: list[dict] = []

        for t in to_process:
            uni = t["university"]
            dept = t["department"]
            console.print(f"\n[cyan]Discovering URL for {uni} / {dept}...[/]")
            state = {"university": uni, "department": dept, "listing_url": ""}
            result = await _discover_url_impl(state, cfg, llm, None)
            url = result.get("listing_url")
            error = result.get("error")

            if url and str(url).startswith("http"):
                console.print(f"  [green]Found: {url}[/]")
                collected.append({"university": uni, "department": dept, "url": url})
                _set_excel_status(cfg.files.input_excel, uni, dept, "url-found", link=url)
            else:
                console.print(f"  [yellow]No URL found[/] {('— ' + error) if error else ''}")
                collected.append({"university": uni, "department": dept, "url": None, "error": error})

        return {"results": collected}

    data = _run_async(_run())
    if json_out:
        _emit_json({"success": True, "data": data})


@cli.command()
@click.option("--config-path", default="config.yaml", help="Path to config file.")
def chat(config_path: str) -> None:
    """Interactive chat agent for configuration & queries."""
    cfg = load_config(config_path)

    async def _run() -> None:
        schema = load_schema(cfg.files.schema_file)
        from .chat import run_chat

        await run_chat(cfg, schema)

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


if __name__ == "__main__":
    cli()
