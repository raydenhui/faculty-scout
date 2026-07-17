"""Schema definition handling for scout.

Parses ``schema.json`` and resolves column types (extracted, formula, static).
"""

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

ColumnType = Literal["extracted", "formula", "static"]


class ColumnValidation(BaseModel):
    regex: str | None = None
    max_length: int | None = None
    min_length: int | None = None
    contains_cjk: bool | None = None
    url_like: bool | None = None
    case_insensitive: bool = False


class ColumnDef(BaseModel):
    name: str
    type: ColumnType
    hint: str | None = None
    formula: str | None = None
    comment: str | None = None
    value: Any | None = None
    value_from: str | None = None
    validation: ColumnValidation | None = None

    def is_extracted(self) -> bool:
        return self.type == "extracted"

    def is_formula(self) -> bool:
        return self.type == "formula"

    def is_static(self) -> bool:
        return self.type == "static"


class Schema(BaseModel):
    columns: list[ColumnDef] = Field(default_factory=list)
    dedup_keys: list[str] = Field(default_factory=list)

    def extracted_columns(self) -> list[ColumnDef]:
        return [c for c in self.columns if c.is_extracted()]

    def formula_columns(self) -> list[ColumnDef]:
        return [c for c in self.columns if c.is_formula()]

    def static_columns(self) -> list[ColumnDef]:
        return [c for c in self.columns if c.is_static()]

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def fingerprint(self) -> str:
        """A stable hash of the extracted columns for cache keys."""
        import hashlib

        payload = json.dumps(
            [{"name": c.name, "hint": c.hint} for c in self.extracted_columns()],
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


def load_schema(path: str | Path = "schema.json") -> Schema:
    """Load a schema from a JSON file. Returns empty schema if missing."""
    path = Path(path)
    if not path.exists():
        return Schema()
    data = json.loads(path.read_text(encoding="utf-8"))
    return Schema.model_validate(data)
