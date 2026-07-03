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
| `llm.provider` | `deepseek` | LLM provider (openai, deepseek, anthropic, google) |
| `llm.model` | `deepseek-v4-flash` | Model name |
| `search.provider` | `duckduckgo` | Search engine for URL discovery |
| `scraping.item_extract_mode` | `split` | `split` = CSS selector item separation + per-field methods. `direct` = LLM outputs all items in one JSON. |
| `scraping.max_detail_pages` | `5` | Detail pages to analyze for extraction patterns |
| `scraping.max_concurrent_jobs` | `3` | Number of concurrent scrape jobs |
| `files.cache_ttl_url` | `604800` | URL content cache TTL in seconds (7 days). Set to `0` to disable. |
| `output.unique_keys` | `["Email", "English Full Name"]` | Keys used for DB record deduplication |
| `output.archive_after_not_found_runs` | `3` | Archive records not seen after N runs |

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
