"""Listing page analysis — two LLM calls.

Cell 2a: Analyze listing page → item pattern + static values
Cell 2b: Determine per-field extraction methods + follow link selector
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from ..logging_config import get_llm_logger, get_logger
from ..schema import Schema
from .html_cleaner import clean_html

log = get_logger("scraper.analyzer")
_llm_log = get_llm_logger()

_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


def _field_map(schema: Schema) -> tuple[dict[str, str], dict[str, str]]:
    """Build field_1..field_N → real name mapping and reverse."""
    columns = list(schema.extracted_columns())
    fwd: dict[str, str] = {}
    rev: dict[str, str] = {}
    for i, col in enumerate(columns, 1):
        alias = f"field_{i}"
        fwd[alias] = col.name
        rev[col.name] = alias
    return fwd, rev


def _field_list_text(schema: Schema) -> str:
    """Produce abstract field listing for prompt."""
    columns = list(schema.extracted_columns())
    lines = []
    for i, col in enumerate(columns, 1):
        lines.append(f'  field_{i} ("{col.name}"): {col.hint or "no hint"}')
    return "\n".join(lines)


def _static_list_text(schema: Schema) -> str:
    """Produce static field listing for prompt."""
    columns = list(schema.static_columns())
    if not columns:
        return ""
    lines = ["  Static fields (filled automatically, ignore in extraction):"]
    for _i, col in enumerate(columns):
        src = col.value_from or "system"
        lines.append(f'    "{col.name}" ← {src}')
    return "\n".join(lines)


def _unmap_records(records: list[dict[str, Any]], rev: dict[str, str]) -> list[dict[str, Any]]:
    """Convert field_N keys back to real column names."""
    for rec in records:
        for key in list(rec.keys()):
            if key.startswith("field_"):
                pass  # keep as-is for now, will be mapped in _normalize_fields
    return records


def _unmap_static_values(static_values: dict[str, str], fwd: dict[str, str]) -> dict[str, str]:
    """Convert field_N keys in static_values to real names."""
    return {fwd.get(k, k): v for k, v in static_values.items()}


class ListingAnalysis:
    """Result of Cell 2a + 2b LLM calls."""

    def __init__(self, raw: dict[str, Any]):
        self.item_selector: str = raw.get("item_selector", "")
        self.item_regex: str = raw.get("item_regex", "")
        self.extraction_methods: dict[str, dict[str, Any]] = raw.get("extraction_methods", {})
        self.static_values: dict[str, str] = raw.get("static_values", {})
        self.follow_link_selector: str = raw.get("follow_link_selector", "")
        self.has_detail_pages: bool = raw.get("has_detail_pages", bool(self.follow_link_selector))
        self._direct_records: list[dict[str, Any]] = raw.get("_direct_records", [])
        self.page_error: str = raw.get("page_error", "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_selector": self.item_selector,
            "item_regex": self.item_regex,
            "extraction_methods": self.extraction_methods,
            "static_values": self.static_values,
            "follow_link_selector": self.follow_link_selector,
            "has_detail_pages": self.has_detail_pages,
            "_direct_records": self._direct_records,
            "page_error": self.page_error,
        }


async def analyze_listing_page(
    llm: BaseChatModel,
    html: str,
    schema: Schema,
    url: str = "",
    mode: str = "split",
) -> ListingAnalysis | None:
    """Cell 2a + 2b or Direct mode: Analyze listing page and determine extraction plan."""
    if mode == "direct":
        return await _analyze_direct(llm, html, schema, url)

    # --- split mode: Cell 2a + 2b ---
    step1_prompt = _build_cell2a_prompt(html, schema, url)
    step1_result = await _call_llm(llm, step1_prompt, "cell2a")
    if step1_result is None:
        return None

    item_selector = step1_result.get("item_selector", "")
    item_regex = step1_result.get("item_regex", "")
    fwd, rev = _field_map(schema)
    static_values = _unmap_static_values(step1_result.get("static_values", {}), fwd)

    log.info("Cell 2a done: selector=%s, statics=%d",
             item_selector or item_regex, len(static_values))

    # --- Cell 2b: per-field extraction methods + follow link ---
    if not item_selector and not item_regex:
        log.warning("Cell 2a returned no item selector or regex")
        return None

    # Apply the item selector to get one sample item
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(item_selector) if item_selector else []
    sample_html = ""
    if items:
        sample_html = str(items[0])[:10_000]
    elif item_regex:
        m = re.search(item_regex, html)
        sample_html = m.group(0)[:10_000] if m else ""

    step2_prompt = _build_cell2b_prompt(sample_html, schema, static_values, url)
    step2_result = await _call_llm(llm, step2_prompt, "cell2b")
    if step2_result is None:
        return None

    extraction_methods = step2_result.get("extraction_methods", {})
    # Unmap field_N → real names
    extraction_methods = {rev.get(k, k): v for k, v in extraction_methods.items()}
    follow_link_selector = step2_result.get("follow_link_selector", "")

    log.info("Cell 2b done: methods=%d, follow_link=%s",
             len(extraction_methods), follow_link_selector or "(none)")

    combined = step1_result
    combined["extraction_methods"] = extraction_methods
    combined["follow_link_selector"] = follow_link_selector
    combined["static_values"] = static_values
    combined["has_detail_pages"] = bool(follow_link_selector)

    return ListingAnalysis(combined)


def _build_cell2a_prompt(html: str, schema: Schema, url: str) -> str:
    field_list = _field_list_text(schema)
    static_info = _static_list_text(schema)

    return f"""Analyze this faculty listing page HTML and create an extraction plan.

URL: {url}

Fields in the schema:
{field_list}
{static_info}

Your task — Step 1: Item separation
Identify how individual faculty entries repeat on the page.
- Provide a CSS selector if entries are in repeating DOM elements.
- Provide a regex if entries follow a textual repeating pattern.
- Prefer CSS selectors. The selector must work with BeautifulSoup select().

Your task — Step 2: Static values
Some fields may have the SAME value for EVERY entry.
Use the abstract field_* keys. List them in "static_values".
Example output for static_values:
{{"field_6": "Department of Computer Science", "field_1": "Prof"}}

Do NOT determine extraction methods yet — that is the next step.

Return ONLY a JSON object:
{{
  "item_selector": "CSS selector string (empty if none)",
  "item_regex": "Regex string (empty if using selector)",
  "static_values": {{"field_2": "shared value"}}
}}

HTML:
{clean_html(html)}"""


def _build_cell2b_prompt(
    sample_item_html: str,
    schema: Schema,
    static_values: dict[str, str],
    url: str,
) -> str:
    field_list = _field_list_text(schema)
    static_note = ""
    if static_values:
        static_note = f"\nAlready known static values: {json.dumps(static_values)}"

    return f"""Analyze this one sample faculty entry from the listing page.

URL: {url}
{static_note}

Fields to extract (use the abstract field_* keys):
{field_list}

Your task — Step 1: Per-field extraction method
For each field, choose a method:
- "selector": CSS selector relative to this item (e.g., "h2", ".name span")
- "regex": A regex pattern matching the field value
- "llm": Only if selector and regex won't work
Omit fields already in static_values.

Your task — Step 2: Follow-up link
Find the link leading to this person's DETAIL/PROFILE page.
Return its CSS selector as "follow_link_selector" (empty if none).

Return ONLY a JSON object:
{{
  "extraction_methods": {{
    "field_1": {{"method": "selector", "pattern": "h2"}},
    "field_2": {{"method": "regex", "pattern": "..."}}
  }},
  "follow_link_selector": "a.profile-link"
}}

HTML (one sample faculty entry):
{sample_item_html}"""


async def _call_llm(llm: BaseChatModel, prompt: str, label: str) -> dict[str, Any] | None:
    try:
        _llm_log.info("===== %s PROMPT =====\n%s\n===== END PROMPT =====", label, prompt)
        response = await llm.ainvoke(prompt)
        text = _response_text(response)
        _llm_log.info("===== %s RESPONSE =====\n%s\n===== END RESPONSE =====", label, text)
        log.debug("%s response:\n%s", label, text)
    except Exception as e:
        log.warning("%s LLM call failed: %s", label, e)
        return None

    m = _JSON_BLOCK_RE.search(text.strip())
    if not m:
        log.warning("%s no JSON found in response", label)
        return None

    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        log.warning("%s invalid JSON", label)
        return None


def _response_text(response: Any) -> str:
    if hasattr(response, "content"):
        return str(response.content)
    if isinstance(response, str):
        return response
    return str(response)


async def _analyze_direct(
    llm: BaseChatModel,
    html: str,
    schema: Schema,
    url: str,
) -> ListingAnalysis | None:
    """Direct mode: LLM outputs all items with fields + profile links in one JSON."""
    field_list = _field_list_text(schema)
    static_info = _static_list_text(schema)
    fwd, rev = _field_map(schema)

    example_fields = ", ".join(f'"{alias}": "..."' for alias in sorted(fwd.keys(), key=lambda x: int(x.split("_")[1])))

    prompt = f"""Extract ALL faculty members from this listing page HTML.

URL: {url}

Fields to extract per person (use the abstract field_* keys):
{field_list}
{static_info}

For each person, fill in every field using the field_* keys shown above.
Use the hint next to each field to understand what to extract.

Missing value rules — use different values for different situations:
- Use "" (empty string) if the field is NOT APPLICABLE to this person
  (e.g., no Chinese name for a non-Chinese professor, no email listed).
- Use null if the field SHOULD exist but is NOT VISIBLE on this listing page
  (e.g., email hidden behind a separate profile page, title missing from snippet).
  null means "go to the profile page to find this".
- Also find each person's profile/detail page link as "profile_url".
  If no profile link exists for a person, set it to null.

Return ONLY a JSON object. If the page is NOT a faculty listing (e.g., error page,
Cloudflare challenge, redirect, empty page, or any page without faculty information),
set "error" to a short description and leave records empty.
Otherwise, omit the "error" field:

{{
  "static_values": {{"field_6": "Department of Computer Science"}},
  "records": [
    {{
      {example_fields},
      "profile_url": "https://..."
    }}
  ],
  "error": null
}}

HTML:
{clean_html(html)}"""

    label = "direct_extraction"
    try:
        _llm_log.info("===== %s PROMPT =====\n%s\n===== END PROMPT =====", label, prompt)
        response = await llm.ainvoke(prompt)
        text = _response_text(response)
        _llm_log.info("===== %s RESPONSE =====\n%s\n===== END RESPONSE =====", label, text)
        log.debug("%s response:\n%s", label, text)
    except Exception as e:
        log.warning("%s LLM call failed: %s", label, e)
        return None

    m = _JSON_BLOCK_RE.search(text.strip())
    if not m:
        log.warning("%s no JSON found in response", label)
        return None

    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        log.warning("%s invalid JSON", label)
        return None

    records = data.get("records", [])
    static_values = _unmap_static_values(data.get("static_values", {}), fwd)
    llm_error = data.get("error")
    if llm_error:
        log.warning("direct mode LLM reported error: %s", llm_error)

    log.info("direct mode: %d records, %d static values", len(records), len(static_values))

    return ListingAnalysis({
        "item_selector": "",
        "item_regex": "",
        "extraction_methods": {},
        "static_values": static_values,
        "follow_link_selector": "",
        "has_detail_pages": True,
        "_direct_records": records,
        "page_error": llm_error or "",
    })
