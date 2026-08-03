"""System tests for the REST API (fscout.rest_api).

Covers:
  - App creation and route registration
  - Health check
  - GET /api/status
  - GET /api/targets
  - POST /api/run with empty pending
  - Error handling (invalid discover input)
  - End-to-end: add target → list → results
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fscout.rest_api import app

client = TestClient(app)


@pytest.fixture()
def api_env(tmp_path: Path) -> dict[str, str]:
    """Point config to temp files and return a config path."""
    schema = {
        "columns": [
            {"name": "English Full Name", "type": "extracted", "hint": "Full name"},
            {"name": "Email", "type": "extracted", "hint": "Email"},
            {"name": "Institution", "type": "static", "value_from": "university_name"},
        ]
    }
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps(schema), encoding="utf-8")

    import yaml

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
    return {"config": str(config_file), "input": str(tmp_path / "universities.xlsx")}


class TestHealth:
    def test_health_ok(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestStatus:
    def test_status_empty(self, api_env):
        r = client.get("/api/status", params={"config_path": api_env["config"]})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["data"]["stats"]["total"] == 0


class TestTargets:
    def test_list_empty(self, api_env):
        r = client.get("/api/targets", params={"config_path": api_env["config"]})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["data"]["total"] == 0

    def test_add_target_then_list(self, api_env):
        add = client.post(
            "/api/targets",
            json={"university": "HKU", "department": "Computer Science",
                  "config_path": api_env["config"]},
        )
        assert add.status_code == 200
        assert add.json()["success"] is True

        lst = client.get("/api/targets", params={"config_path": api_env["config"]})
        body = lst.json()
        assert body["data"]["total"] == 1
        assert body["data"]["targets"][0]["university"] == "HKU"


class TestRun:
    def test_run_empty_pending(self, api_env):
        r = client.post(
            "/api/run",
            json={"force": False, "config_path": api_env["config"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["data"]["summary"]["total"] == 0

    def test_clear_and_run_empty(self, api_env):
        r = client.post(
            "/api/clear-and-run",
            json={"force": False, "config_path": api_env["config"]},
        )
        assert r.status_code == 200
        assert r.json()["success"] is True


class TestDiscover:
    def test_discover_missing_university(self, api_env):
        r = client.post(
            "/api/discover",
            json={"university": "", "config_path": api_env["config"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert body["error"]["code"] == "INVALID_INPUT"


class TestExport:
    def test_export_json(self, api_env):
        r = client.post(
            "/api/export",
            json={"fmt": "json", "config_path": api_env["config"]},
        )
        assert r.status_code == 200
        assert r.json()["success"] is True
