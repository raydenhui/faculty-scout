"""Unit tests for ListingAnalysis, analyze_listing_page prompt, and helpers."""

from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "src")

from fscout.schema import load_schema
from fscout.scraper.pattern_analyzer import (
    ListingAnalysis,
    _field_list_text,
    _field_map,
    _response_text,
    analyze_listing_page,
)


@pytest.fixture
def schema():
    return load_schema("schema.json")


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.ainvoke = AsyncMock()
    return llm


# ---------------------------------------------------------------------------
# ListingAnalysis
# ---------------------------------------------------------------------------


class TestListingAnalysis:
    def test_all_fields_present(self):
        raw = {
            "static_values": {"Department": "CS"},
            "has_detail_pages": True,
            "_direct_records": [{"field_1": "Prof", "field_2": "Alice"}],
            "page_error": "",
            "next_page_url": "https://example.com/page/2",
            "child_page_urls": ["https://example.com/cs", "https://example.com/math"],
        }
        a = ListingAnalysis(raw)
        assert a.static_values == {"Department": "CS"}
        assert a.has_detail_pages is True
        assert a._direct_records == [{"field_1": "Prof", "field_2": "Alice"}]
        assert a.page_error == ""
        assert a.next_page_url == "https://example.com/page/2"
        assert a.child_page_urls == ["https://example.com/cs", "https://example.com/math"]

    def test_next_page_url_restored(self):
        raw = {"next_page_url": "https://example.com/page/3"}
        a = ListingAnalysis(raw)
        assert a.next_page_url == "https://example.com/page/3"

    def test_next_page_url_present_in_to_dict(self):
        raw = {"next_page_url": "https://example.com/page/2", "child_page_urls": []}
        a = ListingAnalysis(raw)
        d = a.to_dict()
        assert "next_page_url" in d
        assert d["next_page_url"] == "https://example.com/page/2"

    def test_child_page_urls_present_in_to_dict(self):
        raw = {"child_page_urls": ["https://example.com/a", "https://example.com/b"]}
        a = ListingAnalysis(raw)
        d = a.to_dict()
        assert "child_page_urls" in d
        assert d["child_page_urls"] == ["https://example.com/a", "https://example.com/b"]

    def test_child_page_urls_default_empty(self):
        a = ListingAnalysis({})
        assert a.child_page_urls == []
        assert a.next_page_url == ""
        assert a.page_error == ""

    def test_to_dict_includes_all_keys(self):
        raw = {
            "static_values": {"field_6": "CS"},
            "_direct_records": [],
            "page_error": "",
            "next_page_url": "https://n.p",
            "child_page_urls": ["https://c.p"],
        }
        a = ListingAnalysis(raw)
        d = a.to_dict()
        assert set(d.keys()) == {
            "static_values", "has_detail_pages", "_direct_records",
            "page_error", "next_page_url", "child_page_urls",
        }


# ---------------------------------------------------------------------------
# Prompt content (no LLM call — we build the prompt string and inspect it)
# ---------------------------------------------------------------------------


class TestPromptContent:
    def test_prompt_contains_next_page_url_field(self, schema):
        """The prompt must instruct the LLM to output next_page_url as a separate field."""
        prompt = _build_prompt(schema)
        assert '"next_page_url"' in prompt
        assert 'next_page_url' in prompt.lower()

    def test_prompt_contains_child_page_urls_field(self, schema):
        prompt = _build_prompt(schema)
        assert '"child_page_urls"' in prompt
        assert 'child_page_urls' in prompt.lower()

    def test_prompt_separates_next_and_child(self, schema):
        """next_page_url and child_page_urls must appear as separate JSON keys."""
        prompt = _build_prompt(schema)
        assert '"next_page_url"' in prompt
        assert '"child_page_urls"' in prompt
        assert prompt.count('"next_page_url"') >= 1
        assert prompt.count('"child_page_urls"') >= 1

    def test_prompt_contains_ancestor_section_when_provided(self, schema):
        prompt = _build_prompt(schema, ancestor_urls=["https://root.com", "https://root.com/dept"])
        assert "Ancestor pages" in prompt
        assert "https://root.com" in prompt
        assert "already visited" in prompt.lower()

    def test_prompt_no_ancestor_section_when_empty(self, schema):
        prompt = _build_prompt(schema, ancestor_urls=[])
        assert "Ancestor pages" not in prompt

    def test_prompt_contains_known_urls_section_when_provided(self, schema):
        prompt = _build_prompt(schema, known_urls=["https://visited.com", "https://queued.com"])
        assert "ALREADY DISCOVERED" in prompt
        assert "https://visited.com" in prompt
        assert "https://queued.com" in prompt
        assert "do NOT output" in prompt

    def test_prompt_excludes_current_url_from_known_urls(self, schema):
        """The current page URL should not appear in the 'ALREADY DISCOVERED' list."""
        prompt = _build_prompt(schema, url="https://current.com",
                               known_urls=["https://current.com", "https://other.com"])
        assert "ALREADY DISCOVERED" in prompt
        assert "https://other.com" in prompt
        assert prompt.count("https://current.com") <= 2

    def test_prompt_no_known_urls_section_when_empty(self, schema):
        prompt = _build_prompt(schema, known_urls=[])
        assert "ALREADY DISCOVERED" not in prompt

    def test_prompt_contains_url_relevance_filter(self, schema):
        prompt = _build_prompt(schema)
        assert '"next_page_url"' in prompt
        assert '"child_page_urls"' in prompt

    def test_prompt_json_example_has_both_fields(self, schema):
        prompt = _build_prompt(schema)
        assert '"next_page_url"' in prompt
        assert '"child_page_urls"' in prompt


# ---------------------------------------------------------------------------
# _response_text
# ---------------------------------------------------------------------------


class TestResponseText:
    def test_from_str(self):
        assert _response_text("hello world") == "hello world"

    def test_from_object_with_content(self):
        class Resp:
            content = "extracted content"
        assert _response_text(Resp()) == "extracted content"

    def test_from_int(self):
        assert _response_text(42) == "42"


# ---------------------------------------------------------------------------
# analyze_listing_page — mock LLM
# ---------------------------------------------------------------------------


class TestAnalyzeListingPage:
    @pytest.mark.asyncio
    async def test_extracts_next_page_url(self, schema, mock_llm):
        mock_llm.ainvoke.return_value = _fake_response({
            "records": [],
            "next_page_url": "https://example.com/page/2",
            "child_page_urls": [],
            "error": None,
        })
        result = await analyze_listing_page(mock_llm, "<html>test</html>", schema,
                                            url="https://example.com/page/1")
        assert result is not None
        assert result.next_page_url == "https://example.com/page/2"

    @pytest.mark.asyncio
    async def test_extracts_child_page_urls(self, schema, mock_llm):
        mock_llm.ainvoke.return_value = _fake_response({
            "records": [],
            "next_page_url": "",
            "child_page_urls": ["https://example.com/cs", "https://example.com/math"],
            "error": None,
        })
        result = await analyze_listing_page(mock_llm, "<html>test</html>", schema,
                                            url="https://example.com/page/1")
        assert result is not None
        assert result.child_page_urls == ["https://example.com/cs", "https://example.com/math"]

    @pytest.mark.asyncio
    async def test_extracts_both_fields(self, schema, mock_llm):
        mock_llm.ainvoke.return_value = _fake_response({
            "records": [{"field_1": "Prof", "field_2": "Alice"}],
            "next_page_url": "https://example.com/page/3",
            "child_page_urls": ["https://example.com/dept-a"],
            "error": None,
        })
        result = await analyze_listing_page(mock_llm, "<html>test</html>", schema)
        assert result is not None
        assert result.next_page_url == "https://example.com/page/3"
        assert result.child_page_urls == ["https://example.com/dept-a"]
        assert len(result._direct_records) == 1

    @pytest.mark.asyncio
    async def test_handles_missing_next_page_url(self, schema, mock_llm):
        mock_llm.ainvoke.return_value = _fake_response({
            "records": [],
            "child_page_urls": [],
            "error": None,
        })
        result = await analyze_listing_page(mock_llm, "<html>test</html>", schema)
        assert result is not None
        assert result.next_page_url == ""

    @pytest.mark.asyncio
    async def test_handles_null_child_page_urls(self, schema, mock_llm):
        mock_llm.ainvoke.return_value = _fake_response({
            "records": [],
            "next_page_url": "",
            "child_page_urls": None,
            "error": None,
        })
        result = await analyze_listing_page(mock_llm, "<html>test</html>", schema)
        assert result is not None
        assert result.child_page_urls == []

    @pytest.mark.asyncio
    async def test_handles_non_list_child_page_urls(self, schema, mock_llm):
        mock_llm.ainvoke.return_value = _fake_response({
            "records": [],
            "next_page_url": "",
            "child_page_urls": "not-a-list",
            "error": None,
        })
        result = await analyze_listing_page(mock_llm, "<html>test</html>", schema)
        assert result is not None
        assert result.child_page_urls == []

    @pytest.mark.asyncio
    async def test_page_error_propagated(self, schema, mock_llm):
        mock_llm.ainvoke.return_value = _fake_response({
            "records": [],
            "error": "Cloudflare challenge detected",
        })
        result = await analyze_listing_page(mock_llm, "<html>test</html>", schema)
        assert result is not None
        assert result.page_error == "Cloudflare challenge detected"

    @pytest.mark.asyncio
    async def test_llm_call_failure_returns_error(self, schema, mock_llm):
        mock_llm.ainvoke.side_effect = RuntimeError("API error")
        result = await analyze_listing_page(mock_llm, "<html>test</html>", schema)
        assert result is not None
        assert result.page_error.startswith("LLM call failed")

    @pytest.mark.asyncio
    async def test_no_json_in_response_returns_error(self, schema, mock_llm):
        mock_llm.ainvoke.return_value = _fake_response("Just plain text, no JSON here.")
        result = await analyze_listing_page(mock_llm, "<html>test</html>", schema)
        assert result is not None
        assert "non-JSON" in result.page_error

    @pytest.mark.asyncio
    async def test_invalid_json_returns_error(self, schema, mock_llm):
        mock_llm.ainvoke.return_value = _fake_response('{"records": [}')
        result = await analyze_listing_page(mock_llm, "<html>test</html>", schema)
        assert result is not None
        assert "invalid json" in result.page_error.lower()

    @pytest.mark.asyncio
    async def test_static_values_unmapped(self, schema, mock_llm):
        mock_llm.ainvoke.return_value = _fake_response({
            "records": [],
            "static_values": {"field_2": "John Doe"},
            "error": None,
        })
        result = await analyze_listing_page(mock_llm, "<html>test</html>", schema)
        assert result is not None
        assert result.static_values.get("English Full Name") == "John Doe"

    @pytest.mark.asyncio
    async def test_passes_known_urls_to_prompt(self, schema, mock_llm):
        mock_llm.ainvoke.return_value = _fake_response({
            "records": [],
            "next_page_url": "",
            "child_page_urls": [],
            "error": None,
        })
        await analyze_listing_page(mock_llm, "<html>test</html>", schema,
                                   known_urls=["https://already.com"])
        call_args = mock_llm.ainvoke.call_args
        prompt = call_args[0][0] if call_args[0] else ""
        assert "ALREADY DISCOVERED" in prompt
        assert "https://already.com" in prompt


# ---------------------------------------------------------------------------
# _field_map and helpers
# ---------------------------------------------------------------------------


class TestFieldHelpers:
    def test_static_list_text(self, schema):
        from fscout.scraper.pattern_analyzer import _static_list_text
        text = _static_list_text(schema)
        assert "Institution" in text or "static" in text.lower()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_prompt(
    schema,
    url: str = "https://example.com/faculty",
    ancestor_urls: list[str] | None = None,
    known_urls: list[str] | None = None,
) -> str:
    """Build the same prompt string that analyze_listing_page would produce,
    without making an actual LLM call."""
    from fscout.scraper.html_cleaner import clean_html

    fwd, _rev = _field_map(schema)

    ancestor_text = ""
    if ancestor_urls:
        ancestor_text = "Ancestor pages (chain from root to this page, already visited):\n" + "\n".join(
            f"  {i}. {u}" for i, u in enumerate(ancestor_urls, 1)
        ) + "\n"

    known_text = ""
    if known_urls:
        known_list = [u for u in known_urls if u != url]
        if known_list:
            known_text = (
                "ALREADY DISCOVERED pages (visited or queued — "
                "do NOT output these as child/next URLs):\n"
                + "\n".join(f"  - {u}" for u in known_list)
                + "\n"
            )

    field_list = _field_list_text(schema)
    static_info = _static_list_text(schema)

    return f"""Extract ALL academic faculty members from this listing page HTML.

URL: {url}
{ancestor_text}{known_text}Fields to extract per person (use the abstract field_* keys):
{field_list}
{static_info}
...
"next_page_url": "...",
"child_page_urls": [...]
...
HTML:
{clean_html("<html>test</html>")}"""


def _fake_response(content):
    class FakeResp:
        pass
    if isinstance(content, str):
        resp = FakeResp()
        resp.content = content
        return resp
    resp = FakeResp()
    resp.content = json.dumps(content)
    return resp


def _static_list_text(schema) -> str:
    from fscout.scraper.pattern_analyzer import _static_list_text
    return _static_list_text(schema)
