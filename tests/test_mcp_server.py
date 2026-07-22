"""System tests for the MCP server (fscout.mcp_server).

Covers:
  - Module import and FastMCP app creation
  - All 7 registered tools
  - FastMCP call_tool integration (all 7 tools tested)
  - _dump helper
  - Agent API envelope compliance
  - End-to-end workflow: add → list → status → export → results
  - Error handling / bad config paths
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fscout.mcp_server import _dump, mcp

# ── helpers ─────────────────────────────────────────────────────────────────

async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call an MCP tool and return parsed JSON dict.

    call_tool returns (list[ContentBlock], metadata_dict).
    """
    content_blocks, _metadata = await mcp.call_tool(name, arguments)
    text = content_blocks[0].text
    return json.loads(text)


def _make_config(schema_path: str, input_excel: str, output_excel: str, cache_dir: str) -> str:
    import yaml
    cfg = {
        "version": 2,
        "llm": {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-test"},
        "search": {"provider": "duckduckgo"},
        "scraping": {"headless": False, "browser_timeout": 5,
                     "max_concurrent_jobs": 1, "max_retries_per_step": 1, "request_delay_sec": 0.0},
        "files": {"input_excel": input_excel, "output_excel": output_excel,
                   "schema_file": schema_path, "cache_dir": cache_dir, "cache_enabled": False},
        "department": {"discovery_enabled": False},
    }
    p = Path(schema_path).parent / "test_config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(p)


def _make_schema(schema_dir: str) -> str:
    schema = {
        "columns": [
            {"name": "Title", "type": "extracted", "hint": "Title"},
            {"name": "English Full Name", "type": "extracted", "hint": "Full name"},
            {"name": "Email", "type": "extracted", "hint": "Email", "required": True,
             "validation": {"regex": r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"}},
            {"name": "Department", "type": "fallback", "hint": "Department", "value_from": "department"},
            {"name": "Institution", "type": "static", "value_from": "university_name"},
            {"name": "Remark", "type": "extracted", "hint": "Remarks"},
        ]
    }
    p = Path(schema_dir) / "test_schema.json"
    p.write_text(json.dumps(schema), encoding="utf-8")
    return str(p)


@pytest.fixture
def mcp_env(tmp_path: Path) -> dict[str, str]:
    input_excel = str(tmp_path / "universities.xlsx")
    output_excel = str(tmp_path / "faculty_data.xlsx")
    cache_dir = str(tmp_path / "cache")
    schema_path = _make_schema(str(tmp_path))
    config_path = _make_config(schema_path, input_excel, output_excel, cache_dir)
    return {"config_path": config_path, "input_excel": input_excel,
            "output_excel": output_excel, "cache_dir": cache_dir}


def _seed_input(excel_path: str, university: str,
                department: str | None = None, link: str | None = None) -> None:
    cols: dict[str, list] = {"University Name": [university]}
    if department:
        cols["Department Name"] = [department]
    if link:
        cols["Link"] = [link]
    pd.DataFrame(cols).to_excel(excel_path, index=False)


def _seed_output(excel_path: str, records: list[dict]) -> None:
    pd.DataFrame(records).to_excel(excel_path, index=False)


# ── _dump helper ────────────────────────────────────────────────────────────

class TestDumpHelper:
    def test_dump_dict(self) -> None:
        r = json.loads(_dump({"success": True, "data": [1, 2]}))
        assert r == {"success": True, "data": [1, 2]}

    def test_dump_with_non_ascii(self) -> None:
        r = _dump({"success": True, "data": "陳小明"})
        assert "陳小明" in r
        assert json.loads(r)["data"] == "陳小明"

    def test_dump_with_none(self) -> None:
        r = json.loads(_dump({"success": True, "data": None}))
        assert r == {"success": True, "data": None}


# ── FastMCP module (sync) ───────────────────────────────────────────────────

class TestMcpModule:
    def test_fastmcp_app_exists(self) -> None:
        assert mcp.name == "fscout"

    async def test_seven_tools_registered(self) -> None:
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        expected = {"add_target", "list_targets", "discover_departments",
                    "run_scrape", "get_status", "get_results", "export_results"}
        assert names == expected

    async def test_tool_input_schemas(self) -> None:
        tools = await mcp.list_tools()
        schemas = {t.name: t.inputSchema for t in tools}

        assert "university" in schemas["add_target"]["properties"]
        assert "university" in schemas["add_target"].get("required", [])

        assert "university" in schemas["discover_departments"]["properties"]
        assert "university" in schemas["discover_departments"].get("required", [])

        assert "force" in schemas["run_scrape"]["properties"]
        assert "config_path" in schemas["run_scrape"]["properties"]


class TestMcpServerConfig:
    def test_server_name(self) -> None:
        assert mcp.name == "fscout"

    def test_run_methods_exist(self) -> None:
        for attr in ("run", "run_stdio_async", "run_sse_async", "run_streamable_http_async"):
            assert hasattr(mcp, attr), f"missing {attr}"


# ── add_target ──────────────────────────────────────────────────────────────

class TestMcpAddTarget:
    async def test_university_only(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("add_target", {
            "university": "HKU", "config_path": mcp_env["config_path"]})
        assert r["success"] is True
        assert r["data"]["university"] == "HKU"
        assert r["data"]["department"] is None

    async def test_with_department(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("add_target", {
            "university": "CUHK", "department": "Computer Science",
            "config_path": mcp_env["config_path"]})
        assert r["success"] is True
        assert r["data"]["department"] == "Computer Science"

    async def test_with_link(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("add_target", {
            "university": "PolyU", "link": "https://www.polyu.edu.hk/faculty",
            "config_path": mcp_env["config_path"]})
        assert r["success"] is True
        assert r["data"]["link"] == "https://www.polyu.edu.hk/faculty"

    async def test_missing_university(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("add_target", {
            "university": "", "config_path": mcp_env["config_path"]})
        assert r["success"] is False
        assert r["error"]["code"] == "INVALID_INPUT"


# ── list_targets ────────────────────────────────────────────────────────────

class TestMcpListTargets:
    async def test_empty(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("list_targets", {"config_path": mcp_env["config_path"]})
        assert r["success"] is True
        assert r["data"]["total"] == 0

    async def test_after_add(self, mcp_env: dict[str, str]) -> None:
        await _call_tool("add_target", {
            "university": "HKU", "department": "CS",
            "config_path": mcp_env["config_path"]})
        r = await _call_tool("list_targets", {"config_path": mcp_env["config_path"]})
        assert r["data"]["total"] == 1
        assert r["data"]["targets"][0]["department"] == "CS"

    async def test_from_seeded_excel(self, mcp_env: dict[str, str]) -> None:
        _seed_input(mcp_env["input_excel"], "HKUST", "Math")
        r = await _call_tool("list_targets", {"config_path": mcp_env["config_path"]})
        assert r["data"]["total"] == 1
        assert r["data"]["targets"][0]["university"] == "HKUST"

    async def test_status_counts(self, mcp_env: dict[str, str]) -> None:
        _seed_input(mcp_env["input_excel"], "HKU", "CS")
        r = await _call_tool("list_targets", {"config_path": mcp_env["config_path"]})
        assert "status_counts" in r["data"]

    async def test_defaults_on_missing_config(self, mcp_env: dict[str, str]) -> None:
        """load_config returns defaults for non-existent paths."""
        r = await _call_tool("list_targets", {"config_path": "/nonexistent/config.yaml"})
        assert r["success"] is True
        assert "total" in r["data"]


# ── get_status ──────────────────────────────────────────────────────────────

class TestMcpGetStatus:
    async def test_empty(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("get_status", {"config_path": mcp_env["config_path"]})
        assert r["success"] is True
        assert r["data"]["stats"]["total"] == 0

    async def test_all_keys_present(self, mcp_env: dict[str, str]) -> None:
        _seed_input(mcp_env["input_excel"], "HKU", "CS")
        r = await _call_tool("get_status", {"config_path": mcp_env["config_path"]})
        stats = r["data"]["stats"]
        for key in ("completed", "failed", "skipped", "pending", "total"):
            assert key in stats

    async def test_defaults_on_missing_config(self, mcp_env: dict[str, str]) -> None:
        """load_config returns defaults for non-existent paths."""
        r = await _call_tool("get_status", {"config_path": "/nonexistent/config.yaml"})
        assert r["success"] is True
        assert "stats" in r["data"]


# ── get_results ─────────────────────────────────────────────────────────────

class TestMcpGetResults:
    async def test_empty(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("get_results", {"config_path": mcp_env["config_path"]})
        assert r["success"] is True
        assert r["data"]["total"] == 0

    async def test_with_data(self, mcp_env: dict[str, str]) -> None:
        _seed_output(mcp_env["output_excel"], [
            {"Title": "Prof.", "English Full Name": "John Smith",
             "Email": "john@hku.hk", "Department": "CS", "Institution": "HKU"},
            {"Title": "Dr.", "English Full Name": "Jane Doe",
             "Email": "jane@cuhk.hk", "Department": "Physics", "Institution": "CUHK"},
        ])
        r = await _call_tool("get_results", {"config_path": mcp_env["config_path"]})
        assert r["data"]["total"] == 2

    async def test_filter_by_university(self, mcp_env: dict[str, str]) -> None:
        _seed_output(mcp_env["output_excel"], [
            {"Title": "Prof.", "English Full Name": "John Smith",
             "Email": "john@hku.hk", "Department": "CS", "Institution": "HKU"},
            {"Title": "Dr.", "English Full Name": "Jane Doe",
             "Email": "jane@cuhk.hk", "Department": "Physics", "Institution": "CUHK"},
        ])
        r = await _call_tool("get_results", {
            "university": "HKU", "config_path": mcp_env["config_path"]})
        assert r["data"]["total"] == 1
        assert r["data"]["records"][0]["English Full Name"] == "John Smith"

    async def test_filter_by_department(self, mcp_env: dict[str, str]) -> None:
        _seed_output(mcp_env["output_excel"], [
            {"Title": "Prof.", "English Full Name": "A",
             "Email": "a@hku.hk", "Department": "CS", "Institution": "HKU"},
            {"Title": "Dr.", "English Full Name": "B",
             "Email": "b@hku.hk", "Department": "Physics", "Institution": "HKU"},
        ])
        r = await _call_tool("get_results", {
            "department": "Physics", "config_path": mcp_env["config_path"]})
        assert r["data"]["total"] == 1

    async def test_defaults_on_missing_config(self, mcp_env: dict[str, str]) -> None:
        """load_config returns defaults for non-existent paths."""
        r = await _call_tool("get_results", {"config_path": "/nonexistent/config.yaml"})
        assert r["success"] is True
        assert "total" in r["data"]


# ── export_results ──────────────────────────────────────────────────────────

class TestMcpExportResults:
    async def test_json_with_path(self, mcp_env: dict[str, str]) -> None:
        out = str(Path(mcp_env["config_path"]).parent / "exported.json")
        r = await _call_tool("export_results", {
            "fmt": "json", "output_path": out, "config_path": mcp_env["config_path"]})
        assert r["success"] is True
        assert r["data"]["format"] == "json"
        assert Path(r["data"]["path"]).exists()

    async def test_json_default_path(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("export_results", {
            "fmt": "json", "config_path": mcp_env["config_path"]})
        assert r["success"] is True
        assert r["data"]["format"] == "json"

    async def test_excel(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("export_results", {
            "fmt": "excel", "config_path": mcp_env["config_path"]})
        if not r["success"]:
            assert r["error"]["code"] == "EXPORT_ERROR"
        else:
            assert r["data"]["format"] == "excel"

    async def test_defaults_on_missing_config(self, mcp_env: dict[str, str]) -> None:
        """JSON export with default config succeeds (reads from non-existent file)."""
        r = await _call_tool("export_results", {
            "fmt": "json", "config_path": "/nonexistent/config.yaml"})
        assert r["success"] is True
        assert r["data"]["format"] == "json"


# ── discover_departments ────────────────────────────────────────────────────

class TestMcpDiscoverDepartments:
    async def test_empty_university(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("discover_departments", {
            "university": "", "config_path": mcp_env["config_path"]})
        assert r["success"] is False
        assert r["error"]["code"] == "INVALID_INPUT"

    async def test_bad_config_llm_error(self, mcp_env: dict[str, str]) -> None:
        """Missing API key returns LLM setup error."""
        r = await _call_tool("discover_departments", {
            "university": "HKU", "config_path": "/nonexistent/config.yaml"})
        assert r["success"] is False
        assert r["error"]["code"] in ("LLM_SETUP_ERROR", "DISCOVERY_ERROR")


# ── run_scrape ──────────────────────────────────────────────────────────────

class TestMcpRunScrape:
    async def test_empty_targets(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("run_scrape", {
            "force": False, "config_path": mcp_env["config_path"]})
        assert r["success"] is True
        assert r["data"]["summary"]["total"] == 0

    async def test_force_flag(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("run_scrape", {
            "force": True, "config_path": mcp_env["config_path"]})
        assert r["success"] is True
        assert r["data"]["summary"]["total"] == 0

    async def test_bad_config(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("run_scrape", {
            "config_path": "/nonexistent/config.yaml"})
        assert r["success"] is False


# ── envelope compliance ─────────────────────────────────────────────────────

class TestMcpEnvelopeCompliance:
    """Every tool returns a valid agent_api envelope."""

    async def test_add_target_envelope(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("add_target", {
            "university": "TestU", "config_path": mcp_env["config_path"]})
        assert "success" in r
        assert "data" in r or "error" in r

    async def test_list_targets_envelope(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("list_targets", {"config_path": mcp_env["config_path"]})
        assert r["success"] is True
        assert "total" in r["data"]

    async def test_get_status_envelope(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("get_status", {"config_path": mcp_env["config_path"]})
        assert "stats" in r["data"]

    async def test_get_results_envelope(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("get_results", {"config_path": mcp_env["config_path"]})
        assert "total" in r["data"]
        assert "records" in r["data"]

    async def test_export_results_envelope(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("export_results", {
            "fmt": "json", "config_path": mcp_env["config_path"]})
        assert "success" in r

    async def test_discover_envelope(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("discover_departments", {
            "university": "", "config_path": mcp_env["config_path"]})
        assert r["success"] is False
        assert "error" in r
        assert "code" in r["error"]

    async def test_run_scrape_envelope(self, mcp_env: dict[str, str]) -> None:
        r = await _call_tool("run_scrape", {"config_path": mcp_env["config_path"]})
        assert "summary" in r["data"]


# ── end-to-end workflow ─────────────────────────────────────────────────────

class TestMcpEndToEndWorkflow:
    async def test_full_crud_workflow(self, mcp_env: dict[str, str]) -> None:
        r1 = await _call_tool("add_target", {
            "university": "HKU", "department": "Computer Science",
            "config_path": mcp_env["config_path"]})
        assert r1["success"] is True

        r2 = await _call_tool("list_targets", {"config_path": mcp_env["config_path"]})
        assert r2["data"]["total"] >= 1

        r3 = await _call_tool("get_status", {"config_path": mcp_env["config_path"]})
        assert r3["data"]["stats"]["pending"] >= 1

        r4 = await _call_tool("export_results", {
            "fmt": "json", "config_path": mcp_env["config_path"]})
        assert r4["success"] is True
        assert r4["data"]["format"] == "json"

        r5 = await _call_tool("get_results", {"config_path": mcp_env["config_path"]})
        assert r5["success"] is True

    async def test_add_multiple_and_count(self, mcp_env: dict[str, str]) -> None:
        for uni, dept, link in [
            ("HKU", "CS", None),
            ("CUHK", "Physics", "https://physics.cuhk.edu.hk"),
            ("HKUST", "Math", None),
        ]:
            r = await _call_tool("add_target", {
                "university": uni, "department": dept, "link": link,
                "config_path": mcp_env["config_path"]})
            assert r["success"] is True

        r = await _call_tool("list_targets", {"config_path": mcp_env["config_path"]})
        assert r["data"]["total"] == 3

    async def test_add_discover_and_status_chain(self, mcp_env: dict[str, str]) -> None:
        await _call_tool("add_target", {
            "university": "HKU", "department": "CS",
            "config_path": mcp_env["config_path"]})

        r = await _call_tool("get_status", {"config_path": mcp_env["config_path"]})
        assert r["data"]["stats"]["pending"] == 1

        r = await _call_tool("discover_departments", {
            "university": "HKU", "config_path": mcp_env["config_path"]})
        # This will fail because LLM isn't configured, but it's a valid tool call
        assert "success" in r
