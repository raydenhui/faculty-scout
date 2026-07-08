# FacultyAI

AI-driven CLI tool that scrapes university faculty information from listing pages and exports structured data.

## Schema Configuration (`schema.json`)

### Column Types

| Type | Description |
|------|-------------|
| `extracted` | Value extracted from HTML by LLM. The LLM chooses `selector`, `regex`, `llm`, or `static` extraction method per field. |
| `static` | Value filled from system-provided metadata. Use `value_from` to specify the metadata key. |
| `formula` | Excel formula using `[@[Column Name]]` syntax. |

### Available Static Metadata Keys

These keys can be referenced in `value_from` for `static` columns:

| Key | Value | Source |
|-----|-------|--------|
| `university_name` | The university name as entered in the input Excel file | `universities.xlsx` university column |

### Validation Rules (Optional)

Each column can have a `validation` object:

| Rule | Type | Description |
|------|------|-------------|
| `regex` | string | Value must match this regex pattern |
| `max_length` | int | Maximum character length |
| `min_length` | int | Minimum character length |
| `contains_cjk` | bool | Value must contain CJK characters (Chinese, Japanese, Korean) |
| `url_like` | bool | Value must start with `http://` or `https://` |

### Dedup Keys (`dedup_keys`)

Top-level array specifying which columns identify a row for Excel replacement.
When the exporter writes incremental results, it removes existing rows whose
values in these columns match the new data.

```json
{
  "columns": [...],
  "dedup_keys": ["Institution", "Department"]
}
```

The merge logic: load existing Excel → find each dedup key's column → remove rows
where ALL dedup values match → append new rows at the bottom.

### Null vs Empty String in LLM Extraction

The LLM uses different values to signal extraction status:

| Value | Meaning | Triggers detail page? |
|-------|---------|----------------------|
| `"actual value"` | Extracted successfully | No |
| `""` (empty string) | Field not applicable to this person | No |
| `null` | Field should exist but not visible on listing page | **Yes** — visits profile page |

### Example Schema

```json
{
  "columns": [
    {
      "name": "English Full Name",
      "type": "extracted",
      "hint": "The professor's full name in English",
      "validation": { "max_length": 100 }
    },
    {
      "name": "Email",
      "type": "extracted",
      "hint": "Email address",
      "validation": { "regex": "^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$" }
    },
    {
      "name": "Institution",
      "type": "static",
      "value_from": "university_name"
    },
    {
      "name": "Remark",
      "type": "extracted",
      "hint": "Tracking notes for fields that could not be found"
    }
  ],
  "dedup_keys": ["Institution", "Department"]
}
```

## Input Excel (`universities.xlsx`)

The input file defines which universities and departments to scrape.

| Column | Required | Description |
|--------|----------|-------------|
| `university_name` | Yes | University name (e.g., "CityUHK", "CUHK") |
| `department_name` | No | Department name (e.g., "Computer Science"). If omitted, triggers department discovery. |
| `status` | No | Empty = scrape. Any value (e.g., "completed") = skip. Clear the cell to re-scrape. |

## Configuration (`config.yaml`)

Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `llm.provider` | `deepseek` | LLM provider (openai, deepseek, openai_compatible, azure, anthropic, google) |
| `llm.model` | `deepseek-v4-flash` | Model name |
| `search.provider` | `duckduckgo` | Search engine for URL discovery |
| `scraping.item_extract_mode` | `split` | `split` = CSS selector item separation + per-field methods. `direct` = LLM outputs all items in one JSON. |
| `scraping.max_detail_pages` | `5` | Detail pages to analyze for extraction patterns |
| `scraping.max_concurrent_jobs` | `3` | Number of concurrent scrape jobs |
| `files.cache_ttl_url` | `604800` | URL content cache TTL in seconds (7 days). Set to `0` to disable. |
| `output.unique_keys` | `["Email", "English Full Name"]` | Keys used for DB record deduplication |
| `output.archive_after_not_found_runs` | `3` | Archive records not seen after N runs |

## LLM Provider & AI Gateway

All LLM calls are routed through a single provider defined in the `llm` section
of `config.yaml`. FacultyAI can talk directly to a vendor API **or** route every
request through an AI gateway (LiteLLM, OpenRouter, Cloudflare AI Gateway,
Portkey, or any OpenAI-compatible proxy) for centralized keys, caching,
rate-limiting, fail-over, and cost tracking.

### `llm` config fields

| Field | Applies to | Description |
|-------|-----------|-------------|
| `provider` | all | `openai`, `deepseek`, `openai_compatible`, `azure`, `anthropic`, `google` |
| `model` | all | Model / deployment name (as the gateway or vendor expects it) |
| `temperature` | all | Sampling temperature (default `0.2`) |
| `max_tokens` | all | Max output tokens (default `4096`) |
| `api_key` | all | API key. Supports `${ENV_VAR}` placeholders |
| `base_url` | openai / deepseek / openai_compatible | **Gateway endpoint** — point this at your AI gateway |
| `azure_endpoint` | azure | Azure OpenAI resource endpoint |
| `api_version` | azure | Azure API version |

### Routing through an AI gateway

Use the `openai_compatible` provider and set `base_url` to the gateway's
OpenAI-compatible endpoint. The gateway forwards to the real model and returns
OpenAI-shaped responses.

```yaml
llm:
  provider: openai_compatible
  model: deepseek-v4-flash          # or "openai/gpt-4o", "anthropic/claude-3-5-sonnet", etc.
  base_url: "${AI_GATEWAY_URL}"     # e.g. https://gateway.example.com/v1
  api_key: "${AI_GATEWAY_KEY}"
  temperature: 0.2
```

Common gateway endpoints for `base_url`:

| Gateway | Example `base_url` |
|---------|--------------------|
| LiteLLM proxy | `http://localhost:4000/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Cloudflare AI Gateway | `https://gateway.ai.cloudflare.com/v1/<acct>/<gw>/openai` |
| Portkey | `https://api.portkey.ai/v1` |

Secrets are never hard-coded — `${AI_GATEWAY_URL}` / `${AI_GATEWAY_KEY}` are
resolved from environment variables at load time. Validate with:

```bash
python -m facultyai config show      # secrets masked
python -m facultyai config validate
```

## Usage

```bash
# Run all universities in input Excel (skips rows with status filled)
python -m facultyai run

# Run with debug logging (full LLM prompts + responses)
python -m facultyai run --debug

# Re-scrape: clear the "status" column in universities.xlsx and run again
# Each completed job writes "completed" back to the Excel status column.

# Check job status and run history
python -m facultyai status

# Export from database
python -m facultyai export

# Resume incomplete jobs
python -m facultyai resume
```

## AI Agent / Programmatic Usage

FacultyAI is designed to be driven by AI agents (e.g. Hermes) and scripts.
Every command supports a `--json` flag that emits a machine-readable envelope
to stdout, and logs/progress go to stderr so stdout stays clean.

### JSON envelope

```json
{"success": true, "data": {...}}
{"success": false, "error": {"code": "PIPELINE_ERROR", "message": "..."}}
```

Exit code is `0` on success, `1` on failure.

### JSON-mode commands

```bash
# Add a target without touching Excel
python -m facultyai add-target HKU "Computer Science" --link https://cs.hku.hk/people --json

# List queued targets
python -m facultyai targets --json

# Discover departments for a university-only entry
python -m facultyai discover --json

# Run the pipeline, get per-job results
python -m facultyai run --json

# Query job status + summary counts
python -m facultyai status --json

# Fetch extracted records (optionally filtered)
python -m facultyai results --university HKU --department "Computer Science" --json

# Export to JSON instead of Excel
python -m facultyai export --format json --output faculty.json --json
```

Example: pipe to `jq`:

```bash
python -m facultyai status --json 2>/dev/null | jq '.data.summary'
python -m facultyai results --json 2>/dev/null | jq '.data.records[].Email'
```

### Python API

```python
from facultyai import agent_api

await agent_api.add_target("HKU", "Computer Science")
await agent_api.run()                       # {"success": True, "data": {...}}
await agent_api.get_results(university="HKU")
await agent_api.export(fmt="json")
```

### MCP Server (for Hermes and other MCP agents)

Install the optional MCP dependency and run the server:

```bash
pip install "facultyai[mcp]"
facultyai-mcp        # stdio transport
```

Exposed tools:

| Tool | Purpose |
|------|---------|
| `add_target` | Queue a university/department (with optional link) |
| `list_targets` | List queued targets and their status |
| `discover_departments` | LLM-discover departments for a university |
| `run_scrape` | Run the full scrape pipeline |
| `get_status` | Job statuses + summary + run history |
| `get_results` | Extracted faculty records (filterable) |
| `export_results` | Export records to JSON/Excel |

Each tool returns the same JSON envelope as the CLI/`agent_api`.

