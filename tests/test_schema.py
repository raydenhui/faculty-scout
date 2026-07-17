"""Tests for schema loading and prompt building."""

from __future__ import annotations

from pathlib import Path

from fscout.schema import ColumnDef, Schema, load_schema


class TestSchemaLoading:
    def test_load_from_file(self, sample_schema_file: Path) -> None:
        schema = load_schema(sample_schema_file)
        assert len(schema.columns) == 4
        assert schema.columns[0].name == "English Full Name"
        assert schema.columns[0].is_extracted()

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        schema = load_schema(tmp_path / "missing.json")
        assert len(schema.columns) == 0

    def test_column_type_helpers(self, sample_schema_file: Path) -> None:
        schema = load_schema(sample_schema_file)
        assert len(schema.extracted_columns()) == 2
        assert len(schema.formula_columns()) == 1
        assert len(schema.static_columns()) == 1

    def test_column_names(self, sample_schema_file: Path) -> None:
        schema = load_schema(sample_schema_file)
        assert schema.column_names() == [
            "English Full Name",
            "Last Name",
            "Email",
            "Institution",
        ]


class TestFallbackType:
    def test_is_fallback(self) -> None:
        col = ColumnDef(name="Department", type="fallback", value_from="department")
        assert col.is_fallback()
        assert not col.is_extracted()
        assert not col.is_static()
        assert not col.is_formula()

    def test_fallback_columns(self) -> None:
        schema = Schema(
            columns=[
                ColumnDef(name="Name", type="extracted"),
                ColumnDef(name="Department", type="fallback", value_from="department"),
                ColumnDef(name="Institution", type="static", value_from="university_name"),
            ]
        )
        assert len(schema.fallback_columns()) == 1
        assert schema.fallback_columns()[0].name == "Department"

    def test_extractable_columns_includes_fallback(self) -> None:
        schema = Schema(
            columns=[
                ColumnDef(name="Name", type="extracted"),
                ColumnDef(name="Department", type="fallback", value_from="department"),
                ColumnDef(name="Email", type="extracted"),
                ColumnDef(name="Institution", type="static", value_from="university_name"),
            ]
        )
        extractable = schema.extractable_columns()
        assert len(extractable) == 3
        assert {c.name for c in extractable} == {"Name", "Department", "Email"}

    def test_fallback_type_from_json(self, tmp_path: Path) -> None:
        import json

        data = {
            "columns": [
                {"name": "Department", "type": "fallback", "value_from": "department", "hint": "Dept name"},
                {"name": "Institution", "type": "static", "value_from": "university_name"},
            ]
        }
        p = tmp_path / "schema_fallback.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        schema = load_schema(p)
        assert len(schema.extracted_columns()) == 0
        assert len(schema.fallback_columns()) == 1
        assert len(schema.static_columns()) == 1
        assert len(schema.extractable_columns()) == 1


class TestFillFallbackFields:
    def test_uses_extracted_value_when_present(self) -> None:
        from fscout.orchestrator import _fill_fallback_fields

        schema = Schema(
            columns=[
                ColumnDef(name="Department", type="fallback", value_from="department"),
            ]
        )
        rec = {"Department": "Computer Science"}
        job = {"university": "HKU", "department": "CS Department"}

        _fill_fallback_fields(rec, job, schema)
        assert rec["Department"] == "Computer Science"

    def test_falls_back_when_none(self) -> None:
        from fscout.orchestrator import _fill_fallback_fields

        schema = Schema(
            columns=[
                ColumnDef(name="Department", type="fallback", value_from="department"),
            ]
        )
        rec = {"Department": None}
        job = {"university": "HKU", "department": "CS Department"}

        _fill_fallback_fields(rec, job, schema)
        assert rec["Department"] == "CS Department"

    def test_falls_back_when_empty_string(self) -> None:
        from fscout.orchestrator import _fill_fallback_fields

        schema = Schema(
            columns=[
                ColumnDef(name="Department", type="fallback", value_from="department"),
            ]
        )
        rec = {"Department": ""}
        job = {"university": "HKU", "department": "CS Department"}

        _fill_fallback_fields(rec, job, schema)
        assert rec["Department"] == "CS Department"

    def test_falls_back_when_whitespace_only(self) -> None:
        from fscout.orchestrator import _fill_fallback_fields

        schema = Schema(
            columns=[
                ColumnDef(name="Department", type="fallback", value_from="department"),
            ]
        )
        rec = {"Department": "   "}
        job = {"university": "HKU", "department": "CS Department"}

        _fill_fallback_fields(rec, job, schema)
        assert rec["Department"] == "CS Department"

    def test_preserves_extracted_value_when_not_fallback_column(self) -> None:
        from fscout.orchestrator import _fill_fallback_fields

        schema = Schema(
            columns=[
                ColumnDef(name="Department", type="extracted"),
            ]
        )
        rec = {"Department": None}
        job = {"university": "HKU", "department": "CS Department"}

        _fill_fallback_fields(rec, job, schema)
        assert rec["Department"] is None

    def test_no_value_from_leaves_field_unchanged(self) -> None:
        from fscout.orchestrator import _fill_fallback_fields

        schema = Schema(
            columns=[
                ColumnDef(name="Department", type="fallback"),
            ]
        )
        rec = {"Department": None}
        job = {"university": "HKU"}

        _fill_fallback_fields(rec, job, schema)
        assert rec["Department"] is None

    def test_multiple_fallback_columns(self) -> None:
        from fscout.orchestrator import _fill_fallback_fields

        schema = Schema(
            columns=[
                ColumnDef(name="Department", type="fallback", value_from="department"),
                ColumnDef(name="Institution", type="fallback", value_from="university_name"),
            ]
        )
        rec = {"Department": None, "Institution": ""}
        job = {"university": "HKU", "department": "CS Department"}

        _fill_fallback_fields(rec, job, schema)
        assert rec["Department"] == "CS Department"
        assert rec["Institution"] == "HKU"

    def test_does_not_overwrite_non_fallback_columns(self) -> None:
        from fscout.orchestrator import _fill_fallback_fields

        schema = Schema(
            columns=[
                ColumnDef(name="Name", type="extracted"),
                ColumnDef(name="Department", type="fallback", value_from="department"),
                ColumnDef(name="Institution", type="static", value_from="university_name"),
            ]
        )
        rec = {"Name": None, "Department": None, "Institution": ""}
        job = {"university": "HKU", "department": "CS Department"}

        _fill_fallback_fields(rec, job, schema)
        assert rec["Name"] is None
        assert rec["Department"] == "CS Department"
        assert rec["Institution"] == ""


class TestFingerprint:
    def test_fingerprint_stable(self, sample_schema_file: Path) -> None:
        s1 = load_schema(sample_schema_file)
        s2 = load_schema(sample_schema_file)
        assert s1.fingerprint() == s2.fingerprint()

    def test_fingerprint_changes_with_hint(self, sample_schema_dict: dict) -> None:
        s1 = Schema.model_validate(sample_schema_dict)
        modified = sample_schema_dict.copy()
        modified["columns"] = [
            {**sample_schema_dict["columns"][0], "hint": "Different hint"},
            *sample_schema_dict["columns"][1:],
        ]
        s2 = Schema.model_validate(modified)
        assert s1.fingerprint() != s2.fingerprint()
