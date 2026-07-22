"""Tests for the agent graph helpers and routing logic."""

from __future__ import annotations

from pathlib import Path

from fscout.cache import CacheManager
from fscout.config import AppConfig, LLMConfig
from fscout.llm_factory import get_llm
from fscout.schema import ColumnDef, Schema
from fscout.scraper_graph import (
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


class TestCacheIntegration:
    def test_cache_set_and_get(self, tmp_path: Path) -> None:
        cm = CacheManager(tmp_path / "cache")
        try:
            url = "https://example.com/faculty"
            content = "<html>test page</html>"
            cm.set_url_content(url, content, ttl_sec=None)
            cached = cm.get_url_content(url)
            assert cached == content
        finally:
            cm.close()

    def test_cache_returns_none_on_miss(self, tmp_path: Path) -> None:
        cm = CacheManager(tmp_path / "cache")
        try:
            cached = cm.get_url_content("https://not-cached.com")
            assert cached is None
        finally:
            cm.close()

    def test_cache_separate_urls(self, tmp_path: Path) -> None:
        cm = CacheManager(tmp_path / "cache")
        try:
            cm.set_url_content("https://a.com", "content A", ttl_sec=None)
            cm.set_url_content("https://b.com", "content B", ttl_sec=None)
            assert cm.get_url_content("https://a.com") == "content A"
            assert cm.get_url_content("https://b.com") == "content B"
        finally:
            cm.close()

    def test_cache_overwrite(self, tmp_path: Path) -> None:
        cm = CacheManager(tmp_path / "cache")
        try:
            cm.set_url_content("https://x.com", "old", ttl_sec=None)
            cm.set_url_content("https://x.com", "new", ttl_sec=None)
            assert cm.get_url_content("https://x.com") == "new"
        finally:
            cm.close()

    def test_build_graph_with_force_rescrape(self, tmp_path: Path) -> None:
        config = AppConfig(llm=LLMConfig(api_key="sk-test-key"))
        schema = Schema(columns=[ColumnDef(name="Name", type="extracted")])
        llm = get_llm(config.llm)
        cm = CacheManager(tmp_path / "cache")
        try:
            graph = build_agent_graph(config, schema, llm, cm, force_rescrape=True)
            assert graph is not None
        finally:
            cm.close()


class TestConfigCacheFields:
    def test_cache_enabled_default(self) -> None:
        from fscout.config import FilesConfig
        fc = FilesConfig()
        assert fc.cache_enabled is True

    def test_cache_enabled_false(self) -> None:
        from fscout.config import FilesConfig
        fc = FilesConfig(cache_enabled=False)
        assert fc.cache_enabled is False

    def test_no_cache_ttl_field(self) -> None:
        from fscout.config import FilesConfig
        fc = FilesConfig()
        assert not hasattr(fc, "cache_ttl_url")

    def test_cache_dir_default(self) -> None:
        from fscout.config import FilesConfig
        fc = FilesConfig()
        assert fc.cache_dir == "./cache"
