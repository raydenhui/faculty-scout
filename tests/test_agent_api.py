"""Tests for the agent-facing programmatic API (agent_api)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fscout import agent_api
from fscout.database import Database


@pytest.fixture
def agent_env(tmp_path: Path) -> str:
    """Create a config + schema + db in a temp dir, return config path."""
    schema = {
        "columns": [
            {"name": "English Full Name", "type": "extracted", "hint": "Full name"},
            {"name": "Email", "type": "extracted", "hint": "Email"},
            {"name": "Institution", "type": "static", "value_from": "university_name"},
        ]
    }
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps(schema), encoding="utf-8")

    config = {
        "version": 2,
        "llm": {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-test"},
        "files": {
            "input_excel": str(tmp_path / "universities.xlsx"),
            "output_excel": str(tmp_path / "faculty_data.xlsx"),
            "schema_file": str(schema_file),
            "database": str(tmp_path / "test.db"),
            "cache_dir": str(tmp_path / "cache"),
        },
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    return str(config_file)


class TestEnvelope:
    def test_ok(self) -> None:
        r = agent_api.ok({"x": 1})
        assert r == {"success": True, "data": {"x": 1}}

    def test_err(self) -> None:
        r = agent_api.err("CODE", "msg", university="HKU")
        assert r["success"] is False
        assert r["error"]["code"] == "CODE"
        assert r["error"]["university"] == "HKU"

    def test_clean_normalizes_nan(self) -> None:
        assert agent_api._clean("nan") is None
        assert agent_api._clean("none") is None
        assert agent_api._clean("") is None
        assert agent_api._clean(None) is None
        assert agent_api._clean("Computer Science") == "Computer Science"


class TestAddAndListTargets:
    async def test_add_target(self, agent_env: str) -> None:
        result = await agent_api.add_target("HKU", "Computer Science", config_path=agent_env)
        assert result["success"] is True
        assert result["data"]["university"] == "HKU"
        assert result["data"]["department"] == "Computer Science"

    async def test_add_target_rejects_empty(self, agent_env: str) -> None:
        result = await agent_api.add_target("", config_path=agent_env)
        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_INPUT"

    async def test_list_targets(self, agent_env: str) -> None:
        await agent_api.add_target("HKU", "Computer Science", config_path=agent_env)
        await agent_api.add_target("CUHK", link="https://cse.cuhk.edu.hk/", config_path=agent_env)

        result = await agent_api.list_targets(agent_env)
        assert result["success"] is True
        assert result["data"]["count"] == 2
        unis = {t["university"] for t in result["data"]["targets"]}
        assert unis == {"HKU", "CUHK"}

    async def test_list_targets_cleans_nan(self, agent_env: str) -> None:
        # Insert a raw "nan" link directly
        cfg_db = Path(agent_env).parent / "test.db"
        async with Database(cfg_db) as db:
            await db.upsert_input_university("HKU", "CS", link="nan", status="nan")
        result = await agent_api.list_targets(agent_env)
        t = result["data"]["targets"][0]
        assert t["link"] is None
        assert t["status"] is None


class TestGetStatusAndResults:
    async def test_status_empty(self, agent_env: str) -> None:
        result = await agent_api.get_status(agent_env)
        assert result["success"] is True
        assert result["data"]["summary"]["total"] == 0

    async def test_results_empty(self, agent_env: str) -> None:
        result = await agent_api.get_results(agent_env)
        assert result["success"] is True
        assert result["data"]["count"] == 0
        assert result["data"]["records"] == []

    async def test_results_with_data(self, agent_env: str) -> None:
        cfg_db = Path(agent_env).parent / "test.db"
        async with Database(cfg_db) as db:
            await db.upsert_faculty(
                "HKU", "Computer Science",
                {"Email": "prof@hku.hk"},
                {"English Full Name": "Prof X", "Email": "prof@hku.hk"},
            )
        result = await agent_api.get_results(agent_env, university="HKU")
        assert result["success"] is True
        assert result["data"]["count"] == 1
        assert result["data"]["records"][0]["Email"] == "prof@hku.hk"


class TestExport:
    async def test_export_json(self, agent_env: str) -> None:
        cfg_db = Path(agent_env).parent / "test.db"
        async with Database(cfg_db) as db:
            await db.upsert_faculty(
                "HKU", "CS",
                {"Email": "a@hku.hk"},
                {"English Full Name": "A", "Email": "a@hku.hk"},
            )
        out = Path(agent_env).parent / "out.json"
        result = await agent_api.export(agent_env, fmt="json", output_path=str(out))
        assert result["success"] is True
        assert result["data"]["format"] == "json"
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["Email"] == "a@hku.hk"
