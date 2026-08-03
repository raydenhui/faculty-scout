# Faculty Scout

AI-driven web scraper that automatically extracts **university faculty directories** from public department listing pages and exports clean, structured data to Excel — no manual data entry.

**Faculty Scout** is an open-source academic data extraction tool built for researchers, administrators, and developers who need current professor and staff records. It uses large language models (LLMs) to understand the structure of any faculty listing page, then follows pagination and sub-category links to capture every member — names, titles, positions, emails, departments, and profile URLs. A **fully customizable schema** lets you define exactly which columns to extract, how they're validated, and how they're filled (scraped, static, or computed) — so the output Excel matches your exact requirements.

## What it does

- **Faculty directory scraping** — extracts professors, lecturers, and academic staff from university websites
- **Automated staff list extraction** — turns messy HTML listings into tidy Excel rows
- **Multi-department crawling** — follows child pages, "next page" links, and category sub-pages automatically
- **LLM-powered parsing** — understands arbitrary page structures without hand-written selectors
- **Schema-driven output** — define columns (name, email, position, Chinese name, etc.) in `schema.json`
- **Incremental updates** — re-scrapes only changed pages, so monthly refresh is cheap
- **Deduplication & validation** — merges duplicate people and validates emails/names
- **API, MCP & Docker ready** — REST API, MCP server, CLI, and containerized 24x7 deployment

## Fully customizable schema — tailor the output to your needs

Unlike one-size-fits-all scrapers, Faculty Scout is **driven by a schema you define**. You decide exactly which columns to extract, how each one is filled, and how strict its validation should be — no code changes required.

```json
{
  "columns": [
    { "name": "English Full Name", "type": "extracted", "hint": "The professor's full name in English", "required": true },
    { "name": "Chinese Full Name",  "type": "extracted", "hint": "The professor's full name in Traditional Chinese", "validation": { "contains_cjk": true } },
    { "name": "Position",           "type": "extracted", "hint": "Position or rank, e.g. Associate Professor" },
    { "name": "Email",              "type": "extracted", "hint": "Email address", "required": true, "validation": { "regex": "^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$" } },
    { "name": "Department",         "type": "fallback", "hint": "The official department name", "value_from": "department" },
    { "name": "Institution",        "type": "static",   "value_from": "university_name" }
  ],
  "dedup_keys": ["Email"]
}
```

Each column type controls **how** a value is produced:

| Column type | How the value is filled |
|-------------|--------------------------|
| `extracted` | Pulled from the HTML by the LLM, guided by a natural-language `hint` |
| `fallback` | LLM extraction first; falls back to a static value (e.g. department) if empty |
| `static` | Always filled from metadata (e.g. the university name), never scraped |
| `formula` | Computed by an Excel formula using other columns |

Add fields, change hints, or tighten validation at any time — the scraper adapts automatically. Whether you need English-only contact lists, bilingual CJK name columns, custom email regexes, or department-level grouping, it's all configured in `schema.json`, not in code. See [Schema Configuration](#schema-configuration-schemajson) for the full reference.

## Use cases

- Building and maintaining **university faculty databases**
- Tracking **academic staff directories** for accreditation, reporting, or research
- Replicating **professor contact lists** (names, emails, departments) into a spreadsheet
- Monitoring **faculty hiring and departures** over time with monthly automated runs
- Powering faculty directories for internal systems via the REST API or MCP

## Key terms

web scraping · faculty scraper · staff directory extraction · university professor data · academic staff crawler · LLM data extraction · HTML-to-Excel · faculty listing parser · automated directory sync

## How it works

Faculty Scout reads scrape targets (university + department + URL) from an input Excel file, fetches each listing page, uses an LLM to extract every faculty member, follows pagination and child pages, and writes structured results directly back to an output Excel file. There is no database — the Excel files are the sole source of truth.

## Features

| Feature | Description |
|---------|-------------|
| **Fully automated** | Scrape an entire university's faculty directory with one command |
| **Any page structure** | LLM parses faculty listing HTML without custom selectors |
| **Deep crawling** | Follows "next page", alphabetical, and category child links |
| **Structured export** | Configurable columns → clean Excel output |
| **Smart caching** | Skips unchanged pages across runs to cut LLM cost |
| **Deduplication** | Merges duplicate people within and across departments |
| **Validation** | Regex, length, and CJK checks per field |
| **Required-field fallback** | Visits profile pages when a key field (e.g. email) is missing |
| **REST API** | Trigger scraping and query results over HTTP |
| **MCP support** | Connect AI agents (e.g. n8n) to the scrape pipeline |
| **Docker-ready** | Run 24x7 with auto-restart for scheduled monthly updates |

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

Fetched page HTML is cached indefinitely (no expiry). On subsequent runs, a page whose content is **unchanged** from the cache is skipped — its `status` is set to `Skipped` in the input Excel instead of `completed`, saving LLM cost and time. Child/next pages are also compared against their caches before deciding to skip.

A fresh scrape is forced in these cases:
- **No existing records:** If the output Excel has no rows for a department (checked via the hidden `_source_key` column), the page is always scraped. This ensures new targets never get skipped.
- **Child page changed:** If any child/next page differs from its cache, the whole department is re-scraped.
- **`--force` flag:** Overrides all caching for the run.

Set `files.cache_enabled: false` to disable caching entirely.

## Schema Configuration (`schema.json`)

The `schema.json` file is the heart of Faculty Scout's customizability. Every column you want in the output Excel is defined here, along with how it should be extracted and validated.

### Column Types

| Type | Description |
|------|-------------|
| `extracted` | Value extracted from HTML by LLM. |
| `fallback` | LLM extraction first; if `null`/empty, falls back to static `value_from`. |
| `static` | Value filled from system-provided metadata via `value_from`. |
| `formula` | Excel formula using `[@[Column Name]]` syntax. |

### Column Attributes

Add any of these to a column definition to control extraction, fallback, and validation:

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
| `llm.max_tokens` | `8192` | Max output tokens |
| `search.provider` | `duckduckgo` | Search engine for URL discovery |
| `scraping.headless` | `true` | Run Playwright in headless mode |
| `scraping.browser_timeout` | `30` | Browser page load timeout (seconds) |
| `scraping.max_concurrent_jobs` | `3` | Number of concurrent scrape targets |
| `scraping.max_retries_per_step` | `3` | Retries per LangGraph node step |
| `scraping.request_delay_sec` | `1.0` | Minimum delay between LLM requests |
| `scraping.skip_children_if_records_ge` | `0` | Skip child pages if parent has ≥ N records (0=disabled) |
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

# Clear ALL statuses in universities.xlsx, then run every target fresh
python -m fscout clear-and-run
python -m fscout clear-and-run --force

# Discover departments for university-only entries
python -m fscout discover

# Discover listing URLs for entries missing a link
python -m fscout url-only

# Export from output Excel to JSON
python -m fscout export --format json --output faculty.json
```

Clearing a row's `status` column in `universities.xlsx` will re-scrape it on the next run.

### Error Handling

If the LLM returns a non-JSON response (or the call fails), the target's `status` column is marked `failed: ...` instead of silently proceeding. This surfaces scraping problems in the input Excel for easy retry.

### skip-children optimization

When `scraping.skip_children_if_records_ge` is set (e.g. `20`), the scraper skips child/sub-category pages if the root listing page already returns ≥ 20 records — useful when a single page already lists all faculty members.

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
await agent_api.clear_and_run()       # clear all statuses, then scrape everything
await agent_api.discover_departments("HKU")
await agent_api.export(fmt="json")
```

### MCP Server

```bash
pip install "faculty-scout[mcp]"
python -m fscout.mcp_server            # stdio transport
python -m fscout.mcp_server --sse      # HTTP/SSE transport (port 8000)
python -m fscout.mcp_server --sse --port 9000  # custom port
```

| Tool | Purpose |
|------|---------|
| `add_target` | Queue a university/department with optional link |
| `list_targets` | List targets from the input Excel |
| `discover_departments` | Discover departments for a university via LLM |
| `run_scrape` | Run the full scrape pipeline |
| `clear_and_run` | Clear all statuses, then run the full pipeline (full re-scrape) |
| `get_status` | Job statuses + summary |
| `get_results` | Extracted faculty records (filterable) |
| `export_results` | Export records to JSON/Excel |

### REST API

```bash
pip install "faculty-scout[api]"
python -m fscout.rest_api --host 0.0.0.0 --port 8000
# or
fscout-api --port 9000
```

Every operation from the Python API is exposed over HTTP. Interactive docs at `http://localhost:8000/docs`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/api/targets` | List all targets + status |
| POST | `/api/targets` | Add a target |
| GET | `/api/status` | Job statuses + summary |
| GET | `/api/results` | Faculty records (filter by `university`/`department`) |
| POST | `/api/discover` | Discover departments via LLM |
| POST | `/api/run` | Start a background scrape job, returns `job_id` |
| POST | `/api/clear-and-run` | Clear statuses, then start a background job |
| GET | `/api/jobs` | List all jobs (most recent first) |
| GET | `/api/jobs/{id}` | Job status/progress/result |
| POST | `/api/jobs/{id}/stop` | Stop a running job |
| POST | `/api/export` | Export to JSON/Excel |

### Async jobs (avoid HTTP timeouts)

`/api/run` and `/api/clear-and-run` are long-running (scraping 100+ departments can take 30–90 min). They return a `job_id` immediately and run in the background, so n8n or a client polls instead of holding a blocking HTTP request:

```bash
# Start a job
curl -X POST http://localhost:8000/api/run \
     -H "Content-Type: application/json" \
     -d '{"force": false}'
# → {"success": true, "data": {"job_id": "a1b2c3d4e5f6", "status": "running", "message": "Job started"}}

# Poll until done
curl http://localhost:8000/api/jobs/a1b2c3d4e5f6
# running → {status: "running", done: 42, total: 266, message: "Processed 42/266 targets"}
# done   → {status: "completed", result: {total, successful, failed, skipped}}

# Stop a running job
curl -X POST http://localhost:8000/api/jobs/a1b2c3d4e5f6/stop
```

Job status values: `running`, `stopping`, `stopped`, `completed`, `failed`.

### n8n monthly workflow

For a monthly full refresh, call `POST /api/clear-and-run`, then loop on `GET /api/jobs/{id}` until `status` is `completed` or `failed`:

```
n8n trigger (monthly)
  → POST /api/clear-and-run                 # returns job_id (no timeout)
  → LOOP until GET /api/jobs/{id} is completed/failed
  → IF completed → copy faculty_data.xlsx to archive folder
  → IF failed → alert
```

### Docker Deployment

```bash
docker compose up -d --build faculty-scout
```

The REST API server runs on `http://localhost:8000` with auto-restart. Set `FSC_MODE=mcp` to run the MCP (SSE) server instead:

```bash
FSC_MODE=mcp docker compose up -d --build faculty-scout
```

Volume mounts for Excel files and cache persist data across restarts. For n8n integration on the same Docker network:
- **REST:** `POST http://faculty-scout:8000/api/run`
- **MCP:** MCP Client node at `http://faculty-scout:8000/sse` (when `FSC_MODE=mcp`)

**Container hardening:** the server runs as the non-root `appuser`. An entrypoint runs as root only to `chown` the mounted volumes (Excel files + cache) and Playwright browsers, then drops privileges via `su`. Playwright browsers are installed to `/ms-playwright` (not `/root/.cache`) so the non-root user can read them. This fixes the common Docker issues:

- **`unable to open database file`** — non-root user couldn't write to the root-owned `scout_cache` volume (diskcache uses SQLite). Resolved by `chown` in the entrypoint.
- **`No page HTML available for scraping`** on JS-heavy pages — Playwright browsers were installed under `/root/.cache`, which `appuser` couldn't read. Resolved by installing to `/ms-playwright`.

**JS-protected emails:** many university sites obfuscate emails with JavaScript that only decodes on user click (e.g. `<span id="e...">[javascript protected email address]</span>`). These cannot be recovered from either raw or rendered HTML, so the `Remark` field notes the email is JavaScript-protected.

### dedup_keys

Schema `dedup_keys` merge records with matching values after each department scrape:

```json
{
  "columns": [...],
  "dedup_keys": ["Email"]
}
```
