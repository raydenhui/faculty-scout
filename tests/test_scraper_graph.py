"""Tests for the agent graph helpers and routing logic."""

from __future__ import annotations

from pathlib import Path

from facultyai.cache import CacheManager
from facultyai.config import AppConfig, LLMConfig
from facultyai.llm_factory import get_llm
from facultyai.schema import ColumnDef, Schema
from facultyai.scraper_graph import (
    AgentState,
    _has_content,
    _llm_response_text,
    _route_dept,
    build_agent_graph,
)


class TestHelpers:
    def test_has_content(self) -> None:
        short = "<html><body>hi</body></html>"
        assert _has_content(short) is False

        long = "<html><body>" + "x " * 200 + "</body></html>"
        assert _has_content(long) is True

    def test_llm_response_text_str(self) -> None:
        assert _llm_response_text("hello") == "hello"

    def test_llm_response_text_object(self) -> None:
        class FakeResponse:
            content = "fake content"

        assert _llm_response_text(FakeResponse()) == "fake content"


class TestRouting:
    def test_route_need_discovery(self) -> None:
        state: AgentState = {"university": "MIT", "need_discovery": True}
        assert _route_dept(state) == "discover"

    def test_route_null_department(self) -> None:
        state: AgentState = {"university": "MIT"}
        assert _route_dept(state) == "discover"

    def test_route_with_department(self) -> None:
        state: AgentState = {"university": "MIT", "department": "EECS", "need_discovery": False}
        assert _route_dept(state) == "direct"


class TestGraphConstruction:
    def test_build_graph_returns_compiled_graph(self, tmp_path: Path) -> None:
        config = AppConfig(llm=LLMConfig(api_key="sk-fake-for-test"))
        schema = Schema(columns=[ColumnDef(name="Name", type="extracted")])
        llm = get_llm(config.llm)
        cm = CacheManager(tmp_path / "cache")

        try:
            graph = build_agent_graph(config, schema, llm, cm)
            assert graph is not None
            assert hasattr(graph, "ainvoke")
        finally:
            cm.close()
