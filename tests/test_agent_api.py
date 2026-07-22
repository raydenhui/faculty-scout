"""Tests for the agent-facing programmatic API (agent_api)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fscout import agent_api


@pytest.fixture
def agent_env(tmp_path: Path) -> str:
    """Create a config + schema + output file in a temp dir, return config path."""
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


class TestExport:
    async def test_export_json(self, agent_env: str) -> None:
        out = Path(agent_env).parent / "out.json"
        result = await agent_api.export(agent_env, fmt="json", output_path=str(out))
        assert result["success"] is True
        assert result["data"]["format"] == "json"
        assert out.exists()
