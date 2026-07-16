"""Listing page analysis — direct LLM extraction.

Sends full listing page HTML to LLM, which returns all faculty records
with fields + profile URLs in one JSON response.
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


def _unmap_static_values(static_values: dict[str, str], fwd: dict[str, str]) -> dict[str, str]:
    """Convert field_N keys in static_values to real names."""
    return {fwd.get(k, k): v for k, v in static_values.items()}


class ListingAnalysis:
    """Result of LLM listing page analysis."""

    def __init__(self, raw: dict[str, Any]):
        self.static_values: dict[str, str] = raw.get("static_values", {})
        self.has_detail_pages: bool = raw.get("has_detail_pages", True)
        self._direct_records: list[dict[str, Any]] = raw.get("_direct_records", [])
        self.page_error: str = raw.get("page_error", "")
        self.next_page_url: str = raw.get("next_page_url", "")
        self.child_page_urls: list[str] = raw.get("child_page_urls", [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "static_values": self.static_values,
            "has_detail_pages": self.has_detail_pages,
            "_direct_records": self._direct_records,
            "page_error": self.page_error,
            "next_page_url": self.next_page_url,
            "child_page_urls": self.child_page_urls,
        }


async def analyze_listing_page(
    llm: BaseChatModel,
    html: str,
    schema: Schema,
    url: str = "",
    ancestor_urls: list[str] | None = None,
    known_urls: list[str] | None = None,
) -> ListingAnalysis | None:
    """Direct mode: LLM outputs all items with fields + profile links in one JSON."""
    field_list = _field_list_text(schema)
    static_info = _static_list_text(schema)
    fwd, rev = _field_map(schema)

    example_fields = ", ".join(
        f'"{alias}": "..."'
        for alias in sorted(fwd.keys(), key=lambda x: int(x.split("_")[1]))
    )

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

    prompt = f"""Extract ALL academic faculty members from this listing page HTML.

URL: {url}
{ancestor_text}{known_text}Fields to extract per person (use the abstract field_* keys):
{field_list}
{static_info}

Who to include — ONLY academic teaching/research personnel:
- Professors (Full, Associate, Assistant, Emeritus, Chair, Visiting, Adjunct, Honorary)
- Lecturers (Senior Lecturer, Lecturer, Assistant Lecturer, Part-time Lecturer)
- Department Heads, Deans, Directors of academic programmes

Who to EXCLUDE — skip these entirely:
- Postdoctoral Fellows / Research Fellows / Research Assistants
- Research Staff, Senior Research Staff, Scientific Staff
- Teaching Support Staff, Teaching Assistants, Lab Technicians
- Administrative staff, Secretaries, IT Support, HR, Finance
- PhD students, Graduate students, Interns

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
Otherwise, omit the "error" field.

PAGINATION RULES — two separate fields below:

next_page_url — sequential continuation of the CURRENT listing:
- If the listing has a "next page" link, include ONLY the IMMEDIATE next page URL.
  Do NOT enumerate all future pages — just the very next one.
  Example: on /faculty?page=1, set next_page_url to ".../faculty?page=2" (not page=3).
  Example: on /faculty/A/, set next_page_url to ".../faculty/B/" (not /C/).
- If this is the last page or pagination does not exist, set next_page_url to "".

child_page_urls — separate sibling/category listing pages:
- These are DIFFERENT listing pages at the SAME level (e.g., separate department pages).
- Include ALL sibling category/department sub-pages that contain faculty listings.
- Include alphabet index pages ONLY if they appear as parallel sibling links (not "next").
  Example: [".../cs/faculty", ".../math/faculty", ".../physics/faculty"]

CRITICAL — URL relevance filter:
- ONLY include URLs that clearly belong to the same faculty/staff listing system
  (same website section, same navigation structure, same domain path prefix).
- Every URL MUST be a page that continues listing faculty members.
- Do NOT include: homepages, contact pages, about pages, external websites,
  login pages, search pages, or generic university navigation links.
- Do NOT include any URL listed in the "ALREADY DISCOVERED" section above.
- If you are NOT certain a URL leads to more faculty listings, do NOT include it.
- Each URL must have a visible, clickable anchor tag in the HTML — never guess or construct URLs.

{{
  "static_values": {{"field_6": "Department of Computer Science"}},
  "records": [
    {{
      {example_fields},
      "profile_url": "https://..."
    }}
  ],
  "error": null,
  "next_page_url": "https://..." or "",
  "child_page_urls": ["https://...", "https://..."]
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
    next_page_url = (data.get("next_page_url") or "").strip()
    child_page_urls = data.get("child_page_urls", [])
    if not isinstance(child_page_urls, list):
        child_page_urls = []
    if llm_error:
        log.warning("direct mode LLM reported error: %s", llm_error)

    log.info("direct mode: %d records, %d static values, next=%s, child_pages=%d",
             len(records), len(static_values), next_page_url or "(none)", len(child_page_urls))

    return ListingAnalysis({
        "static_values": static_values,
        "has_detail_pages": True,
        "_direct_records": records,
        "page_error": llm_error or "",
        "next_page_url": next_page_url,
        "child_page_urls": child_page_urls,
    })


def _response_text(response: Any) -> str:
    if hasattr(response, "content"):
        return str(response.content)
    if isinstance(response, str):
        return response
    return str(response)
