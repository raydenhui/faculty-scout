"""MCP server exposing fscout as tools for AI agents (e.g. Hermes).

Wraps the functions in ``fscout.agent_api`` as MCP tools so an agent
can orchestrate the scrape → discover → export loop through typed tool
calls instead of parsing CLI text.

Run with:
    fscout-mcp
    # or
    python -m fscout.mcp_server

Requires the optional ``mcp`` dependency:
    pip install "faculty-scout[mcp]"
"""

from __future__ import annotations

import json
from typing import Any

from . import agent_api

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "The 'mcp' package is required to run the MCP server.\n"
        'Install it with:  pip install "faculty-scout[mcp]"'
    ) from e


mcp = FastMCP("fscout")

_DEFAULT_CONFIG = "config.yaml"


@mcp.tool()
async def add_target(
    university: str,
    department: str | None = None,
    link: str | None = None,
    config_path: str = _DEFAULT_CONFIG,
) -> str:
    """Add a scrape target (university + optional department + optional listing link).

    If link is provided, URL discovery is skipped and the link is used directly.
    Returns a JSON envelope: {"success": bool, "data"|"error": ...}.
    """
    result = await agent_api.add_target(university, department, link, config_path=config_path)
    return _dump(result)


@mcp.tool()
async def list_targets(config_path: str = _DEFAULT_CONFIG) -> str:
    """List all configured scrape targets and their status."""
    return _dump(await agent_api.list_targets(config_path))


@mcp.tool()
async def discover_departments(
    university: str,
    link: str | None = None,
    config_path: str = _DEFAULT_CONFIG,
) -> str:
    """Discover academic departments for a university via LLM + web search.

    Returns the list of departments and writes them into the input queue.
    """
    return _dump(await agent_api.discover_departments(university, link, config_path=config_path))


@mcp.tool()
async def run_scrape(
    force: bool = False,
    config_path: str = _DEFAULT_CONFIG,
) -> str:
    """Run the full scrape pipeline over all pending targets.

    Returns per-job results and a summary {completed, failed, total}.
    Long-running: fetches pages and calls the LLM for every target.
    Set force=True to rescrape even when existing records are found.
    """
    return _dump(await agent_api.run(config_path, force=force))


@mcp.tool()
async def clear_and_run(
    force: bool = False,
    config_path: str = _DEFAULT_CONFIG,
) -> str:
    """Clear all target statuses in the input Excel, then run the full pipeline.

    Useful for a full re-scrape: resets every target to pending before scraping.
    Returns {cleared: N, summary: {...}}.
    """
    return _dump(await agent_api.clear_and_run(config_path, force=force))


@mcp.tool()
async def get_status(config_path: str = _DEFAULT_CONFIG) -> str:
    """Get current job statuses, summary counts, and recent run history."""
    return _dump(await agent_api.get_status(config_path))


@mcp.tool()
async def get_results(
    university: str | None = None,
    department: str | None = None,
    config_path: str = _DEFAULT_CONFIG,
) -> str:
    """Get extracted faculty records, optionally filtered by university/department."""
    return _dump(await agent_api.get_results(config_path, university, department))


@mcp.tool()
async def export_results(
    fmt: str = "json",
    output_path: str | None = None,
    config_path: str = _DEFAULT_CONFIG,
) -> str:
    """Export active faculty records to a file. fmt = 'json' or 'excel'."""
    return _dump(await agent_api.export(config_path, fmt=fmt, output_path=output_path))


def _dump(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


def main() -> None:
    """Entry point for the ``fscout-mcp`` script.

    Stdio transport by default.  Pass ``--sse`` for HTTP/SSE transport.
    Options: ``--host HOST`` (default 0.0.0.0), ``--port PORT`` (default 8000).
    """
    import sys
    if "--sse" in sys.argv:
        for i, arg in enumerate(sys.argv):
            if arg == "--host" and i + 1 < len(sys.argv):
                mcp.settings.host = sys.argv[i + 1]
            if arg == "--port" and i + 1 < len(sys.argv):
                mcp.settings.port = int(sys.argv[i + 1])
        if "--host" not in sys.argv:
            mcp.settings.host = "0.0.0.0"
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )
        mcp.run(transport="sse")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
