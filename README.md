# Faculty Scout

AI-driven CLI tool that scrapes university faculty information from listing pages and exports structured data to Excel.

## Architecture

Faculty Scout reads scrape targets from an Excel file, fetches listing pages, extracts faculty data via LLM, and writes results directly back to an output Excel file. There is no database — the Excel files are the sole source of truth.

```
universities.xlsx  ──read──►  pipeline (in-memory)
                                    │
                          ┌─────────┼─────────┐
                          ▼         ▼         ▼
                       scrape    scrape    scrape    (concurrent)
                          │         │         │
                          └─────────┼─────────┘
                                    ▼
                           faculty_data.xlsx   (incremental merge per dept)
```

### Deduplication

Output rows are deduplicated using a hidden `_source_key` column containing `{department}/{university}` from the input file. When re-scraping a department, old rows matching that key are replaced — no LLM-filled columns are used for matching, guaranteeing consistency regardless of schema configuration.

### Caching

Fetched page HTML is cached indefinitely (no expiry). On subsequent runs, pages whose content is unchanged from the cache are skipped — saving LLM cost and time. Two exceptions always force a fresh scrape regardless:

- **No existing records:** If the output Excel has no rows for a department (checked via the hidden `_source_key` column), the page is always scraped. This ensures new targets never get skipped.
- **`--force` flag:** Overrides all caching for the run.

Set `files.cache_enabled: false` to disable caching entirely.

## Schema Configuration (`schema.json`)

### Column Types

| Type | Description |
|------|-------------|
| `extracted` | Value extracted from HTML by LLM. |
| `fallback` | LLM extraction first; if `null`/empty, falls back to static `value_from`. |
| `static` | Value filled from system-provided metadata via `value_from`. |
| `formula` | Excel formula using `[@[Column Name]]` syntax. |

### Column Attributes

| Attribute | Applies to | Description |
|-----------|------------|-------------|
| `name` | all | Column header name |
| `type` | all | One of `extracted`, `fallback`, `static`, `formula` |
| `hint` | `extracted`, `fallback` | Natural-language hint for the LLM during extraction |
| `value_from` | `fallback`, `static` | Metadata key for static / fallback value |
| `value` | `static` | Hard-coded static value (used when `value_from` is not set) |
| `formula` | `formula` | Excel formula using `[@[Column Name]]` syntax |
| `required` | `extracted`, `fallback` | If `true`, a `null` or `""` value triggers a detail page visit |
| `validation` | `extracted`, `fallback` | Validation rules (regex, max_length, etc.) |

### Available `value_from` Keys

| Key | Value | Source |
|-----|-------|--------|
| `university_name` | University name from input Excel | `universities.xlsx` |
| `department` | Department name from input Excel | `universities.xlsx` |
| `listing_url` | Listing page URL being scraped | Discovered or provided link |

### Validation Rules (Optional)

| Rule | Type | Description |
|------|------|-------------|
| `regex` | string | Value must match this regex pattern |
| `max_length` | int | Maximum character length |
| `min_length` | int | Minimum character length |
| `contains_cjk` | bool | Value must contain CJK characters |
| `url_like` | bool | Value must start with `http://` or `https://` |

### Null vs Empty String in LLM Extraction

| Value | Meaning | Triggers detail page? |
|-------|---------|----------------------|
| `"actual value"` | Extracted successfully | No |
| `""` (empty string) | Field not applicable to this person | Only if `required: true` |
| `null` | Field should exist but not visible on listing page | Yes (always), or via `required: true` |

The `required` attribute provides a schema-level override: if a field is marked `required: true`, the scraper visits the profile page whenever that field is `null` **or** `""` — regardless of what other fields say. This guarantees critical fields like Email and Name are always pursued, even if the LLM returned `""` for them on the listing page.

### Example Schema

```json
{
  "columns": [
    {
      "name": "Title",
      "type": "extracted",
      "hint": "The person's title, e.g. Dr., Prof.",
      "validation": { "max_length": 30 }
    },
    {
      "name": "English Full Name",
      "type": "extracted",
      "hint": "The professor's full name in English",
      "required": true
    },
    {
      "name": "Chinese Full Name",
      "type": "extracted",
      "hint": "The professor's full name in Traditional Chinese",
      "validation": { "contains_cjk": true }
    },
    {
      "name": "Position",
      "type": "extracted",
      "hint": "Position or rank, e.g. Associate Professor",
      "validation": { "max_length": 200 }
    },
    {
      "name": "Email",
      "type": "extracted",
      "hint": "Email address",
      "required": true,
      "validation": { "regex": "^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$" }
    },
    {
      "name": "Department",
      "type": "fallback",
      "hint": "The official department name",
      "validation": { "max_length": 100 },
      "value_from": "department"
    },
    {
      "name": "Institution",
      "type": "static",
      "value_from": "university_name"
    },
    {
      "name": "Remark",
      "type": "extracted",
      "hint": "Additional information about the scraping process"
    }
  ]
}
```

## Input Excel (`universities.xlsx`)

| Column | Required | Description |
|--------|----------|-------------|
| `university_name` | Yes | University name (e.g., "HKUST", "CUHK") |
| `department_name` | No | Department name. If omitted, triggers department discovery. |
| `link` | No | Pre-filled listing URL. If omitted, URL is auto-discovered. |
| `status` | No | Controls re-scraping. Empty = scrape. `"completed"` = skip. `"failed:..."` = skip. Clear to re-scrape. |

## Configuration (`config.yaml`)

| Setting | Default | Description |
|---------|---------|-------------|
| `llm.provider` | `deepseek` | LLM provider (openai, deepseek, openai_compatible, azure, anthropic, google) |
| `llm.model` | `deepseek-v4-flash` | Model name |
| `llm.temperature` | `0.2` | Sampling temperature |
| `llm.max_tokens` | `4096` | Max output tokens |
| `search.provider` | `duckduckgo` | Search engine for URL discovery |
| `scraping.headless` | `true` | Run Playwright in headless mode |
| `scraping.browser_timeout` | `30` | Browser page load timeout (seconds) |
| `scraping.max_concurrent_jobs` | `3` | Number of concurrent scrape targets |
| `scraping.max_retries_per_step` | `3` | Retries per LangGraph node step |
| `scraping.request_delay_sec` | `1.0` | Minimum delay between LLM requests |
| `files.cache_enabled` | `true` | Cache fetched HTML for skip-unchanged detection |
| `department.discovery_enabled` | `true` | Auto-discover departments for university-only entries |

## LLM Provider & AI Gateway

All LLM calls route through a single provider. Use `openai_compatible` with a gateway `base_url` for centralized keys, caching, rate-limiting, and cost tracking.

```yaml
llm:
  provider: openai_compatible
  model: deepseek-v4-flash
  base_url: "${AI_GATEWAY_URL}"
  api_key: "${AI_GATEWAY_KEY}"
  temperature: 0.2
```

| Gateway | Example `base_url` |
|---------|--------------------|
| LiteLLM proxy | `http://localhost:4000/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Cloudflare AI Gateway | `https://gateway.ai.cloudflare.com/v1/<acct>/<gw>/openai` |
| Portkey | `https://api.portkey.ai/v1` |

Secrets use `${ENV_VAR}` placeholders resolved at load time:

```bash
python -m fscout config show      # secrets masked
python -m fscout config validate
```

## Usage

```bash
# Run all pending targets from the input Excel
# By default, pages with unchanged HTML are skipped (cached comparison)
python -m fscout run

# Run with debug logging (full LLM prompts + responses)
python -m fscout run --debug

# Always re-scrape even if page content is unchanged
python -m fscout run --force

# Discover departments for university-only entries
python -m fscout discover

# Discover listing URLs for entries missing a link
python -m fscout url-only

# Export from output Excel to JSON
python -m fscout export --format json --output faculty.json
```

Clearing a row's `status` column in `universities.xlsx` will re-scrape it on the next run.

## AI Agent / Programmatic Usage

Every command supports a `--json` flag for machine-readable output (stdout is clean JSON, logs go to stderr):

```json
{"success": true, "data": {...}}
{"success": false, "error": {"code": "PIPELINE_ERROR", "message": "..."}}
```

Exit code is `0` on success, `1` on failure.

```bash
python -m fscout run --json
python -m fscout discover --json
python -m fscout export --format json --output faculty.json --json
```

### Python API

```python
from fscout import agent_api

await agent_api.run()
await agent_api.run(force=True)
await agent_api.discover_departments("HKU")
await agent_api.export(fmt="json")
```

### MCP Server

```bash
pip install "faculty-scout[mcp]"
fscout-mcp        # stdio transport
```

Exposed tools:

| Tool | Purpose |
|------|---------|
| `add_target` | Queue a university/department with optional link |
| `list_targets` | List targets from the input Excel |
| `discover_departments` | Discover departments for a university via LLM |
| `run_scrape` | Run the full scrape pipeline |
| `get_status` | Job statuses + summary |
| `get_results` | Extracted faculty records (filterable) |
| `export_results` | Export records to JSON/Excel |
