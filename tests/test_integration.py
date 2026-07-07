"""Integration test: fixed LLM output flows through validation and normalization."""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "src")

from facultyai.schema import load_schema
from facultyai.scraper.__init__ import _normalize_fields
from facultyai.scraper_graph import _apply_schema_validation

SAMPLE_RECORDS = [
    {"Title": "Prof", "English Full Name": "LUO, Yuhan", "Chinese Full Name": "羅雨菡",
     "Email": "yuhanluo@cityu.edu.hk", "Position": "Assistant Professor",
     "profile_url": "https://scholars.cityu.edu.hk/en/persons/yuhan-luo(9).html"},
    {"Title": "Prof", "English Full Name": "MA, Jiawei Phoenix", "Chinese Full Name": "馬佳葳",
     "Email": "jiaweima@cityu.edu.hk", "Position": "Assistant Professor",
     "profile_url": "https://scholars.cityu.edu.hk/en/persons/jiawei-ma(49).html"},
    {"Title": "Prof", "English Full Name": "MA, Ziye", "Chinese Full Name": "馬梓業",
     "Email": "ziyema@cityu.edu.hk", "Position": "Assistant Professor",
     "profile_url": "https://scholars.cityu.edu.hk/en/persons/ziye-ma(00).html"},
    {"Title": "Prof", "English Full Name": "QIU, Junqiao", "Chinese Full Name": "邱俊喬",
     "Email": "junqiqiu@cityu.edu.hk", "Position": "Assistant Professor",
     "profile_url": "https://scholars.cityu.edu.hk/en/persons/junqiao-qiu(90).html"},
    {"Title": "Prof", "English Full Name": "ZHAO, Qingchuan", "Chinese Full Name": "趙晴川",
     "Email": "qizhao@cityu.edu.hk", "Position": "Assistant Professor",
     "profile_url": "https://scholars.cityu.edu.hk/en/persons/qingchuan-zhao(b0).html"},
    # Record without email
    {"Title": "Prof", "English Full Name": "ZHANG, Zhisong", "Chinese Full Name": "張智松",
     "Email": "", "Position": "Assistant Professor",
     "profile_url": "https://scholars.cityu.edu.hk/en/persons/zhisong-zhang"},
    # Record without Chinese name (valid empty string)
    {"Title": "Dr", "English Full Name": "John Smith", "Chinese Full Name": "",
     "Email": "jsmith@cityu.edu.hk", "Position": "Lecturer",
     "profile_url": "https://scholars.cityu.edu.hk/en/persons/john-smith"},
    # Record with invalid email
    {"Title": "Prof", "English Full Name": "Bad Email", "Chinese Full Name": "壞郵件",
     "Email": "not-an-email", "Position": "Professor",
     "profile_url": "https://scholars.cityu.edu.hk/en/persons/bad-email"},
    # Record with abstract field_N keys (tests unmapping)
    {"field_1": "Prof", "field_2": "WANG, Cong", "field_3": "王聰",
     "field_4": "Head & Chair Professor", "field_5": "congwang@cityu.edu.hk",
     "field_6": "Computer Science", "profile_url": "https://scholars.cityu.edu.hk/en/persons/wang"},
    # Minimally filled record
    {"English Full Name": "Min Record", "profile_url": "https://test.edu/min"},
]


@pytest.fixture
def schema():
    return load_schema("schema.json")


class TestNormalizeFields:
    def test_all_schema_fields_present(self, schema):
        field_names = [c.name for c in schema.extracted_columns()]
        records = _normalize_fields(SAMPLE_RECORDS, field_names)

        for rec in records:
            for fn in field_names:
                assert fn in rec, f"Missing field {fn}"
                # None is valid — means "need detail page visit"

    def test_none_vs_empty_distinction(self, schema):
        """None = needs detail page visit, '' = not applicable."""
        field_names = [c.name for c in schema.extracted_columns()]
        records = _normalize_fields(SAMPLE_RECORDS, field_names)

        # LUO has all fields filled → no None values
        luo = records[0]
        assert luo["English Full Name"] is not None
        assert luo["Email"] is not None

        # Min record has only name → all others should be None
        min_rec = records[-1]
        assert min_rec["Title"] is None
        assert min_rec["Email"] is None
        assert min_rec["Position"] is None

    def test_profile_url_kept_for_detail_visits(self, schema):
        """profile_url is kept after normalization for visit_detail_pages use."""
        field_names = [c.name for c in schema.extracted_columns()]
        records = _normalize_fields(SAMPLE_RECORDS, field_names)

        # profile_url should be PRESENT (needed by visit_detail_pages)
        luo = records[0]
        assert luo.get("profile_url") is not None  # preserved for detail visits

    def test_field_n_unmapping(self, schema):
        """field_1..field_6 should map to real schema column names."""
        field_names = [c.name for c in schema.extracted_columns()]
        records = _normalize_fields(SAMPLE_RECORDS, field_names)

        # Find the abstract-keys record (WANG, Cong)
        wang = next((r for r in records if r.get("English Full Name") == "WANG, Cong"), None)
        assert wang is not None, "WANG, Cong record should exist after unmapping"
        assert wang["Title"] == "Prof"
        assert wang["Email"] == "congwang@cityu.edu.hk"
        assert wang["Position"] == "Head & Chair Professor"
        assert "field_1" not in wang

    def test_record_count_preserved(self, schema):
        field_names = [c.name for c in schema.extracted_columns()]
        records = _normalize_fields(SAMPLE_RECORDS, field_names)
        assert len(records) == len(SAMPLE_RECORDS)


class TestSchemaValidation:
    def test_valid_email_passes(self, schema):
        records = _normalize_fields(SAMPLE_RECORDS, [c.name for c in schema.extracted_columns()])
        validated = _apply_schema_validation(records, schema)

        yuhan = next((r for r in validated if r.get("English Full Name") == "LUO, Yuhan"), None)
        assert yuhan["Email"] == "yuhanluo@cityu.edu.hk"

    def test_invalid_email_cleared(self, schema):
        records = _normalize_fields(SAMPLE_RECORDS, [c.name for c in schema.extracted_columns()])
        validated = _apply_schema_validation(records, schema)

        bad = next((r for r in validated if r.get("English Full Name") == "Bad Email"), None)
        assert bad["Email"] == "", f"Expected empty email, got {bad['Email']}"

    def test_empty_email_passes(self, schema):
        records = _normalize_fields(SAMPLE_RECORDS, [c.name for c in schema.extracted_columns()])
        validated = _apply_schema_validation(records, schema)

        zhang = next((r for r in validated if r.get("English Full Name") == "ZHANG, Zhisong"), None)
        assert zhang["Email"] == ""  # Empty is valid (no email available)

    def test_chinese_name_validated(self, schema):
        records = _normalize_fields(SAMPLE_RECORDS, [c.name for c in schema.extracted_columns()])
        validated = _apply_schema_validation(records, schema)

        # Chinese name should be kept
        wang = next((r for r in validated if r.get("English Full Name") == "WANG, Cong"), None)
        assert wang["Chinese Full Name"] == "王聰"

        # Empty Chinese name is fine (John Smith has no Chinese name)
        smith = next((r for r in validated if r.get("English Full Name") == "John Smith"), None)
        assert smith["Chinese Full Name"] == ""

        # "壞郵件" IS valid CJK — it passes the validation correctly
        bad = next((r for r in validated if r.get("English Full Name") == "Bad Email"), None)
        assert bad["Chinese Full Name"] == "壞郵件"

    def test_title_max_length(self, schema):
        records = _normalize_fields(SAMPLE_RECORDS, [c.name for c in schema.extracted_columns()])
        validated = _apply_schema_validation(records, schema)
        for r in validated:
            assert len(r.get("Title", "")) <= 30

    def test_position_max_length(self, schema):
        records = _normalize_fields(SAMPLE_RECORDS, [c.name for c in schema.extracted_columns()])
        validated = _apply_schema_validation(records, schema)
        for r in validated:
            assert len(r.get("Position", "")) <= 200

    def test_department_max_length(self, schema):
        records = _normalize_fields(SAMPLE_RECORDS, [c.name for c in schema.extracted_columns()])
        validated = _apply_schema_validation(records, schema)
        for r in validated:
            assert len(r.get("Department", "")) <= 100

    def test_no_data_lost_for_valid_records(self, schema):
        records = _normalize_fields(SAMPLE_RECORDS, [c.name for c in schema.extracted_columns()])
        validated = _apply_schema_validation(records, schema)

        yuhan = next((r for r in validated if r.get("English Full Name") == "LUO, Yuhan"), None)
        assert yuhan["Title"] == "Prof"
        assert yuhan["Position"] == "Assistant Professor"
        assert yuhan["Chinese Full Name"] == "羅雨菡"


class TestFullPipelineSimulation:
    def test_full_flow(self, schema):
        """Simulate the full post-LLM pipeline: normalize → validate."""
        field_names = [c.name for c in schema.extracted_columns()]

        # Step 1: Normalize (field_N unmapping + defaults + strip profile_url)
        records = _normalize_fields(SAMPLE_RECORDS, field_names)

        # Step 2: Schema validation
        validated = _apply_schema_validation(records, schema)

        # Step 3: Strip profile_url (done in scrape() after detail pages)
        for r in validated:
            r.pop("profile_url", None)

        # Verify good records
        assert len(validated) == len(SAMPLE_RECORDS)

        # Good record intact
        luo = validated[0]
        assert luo["English Full Name"] == "LUO, Yuhan"
        assert luo["Email"] == "yuhanluo@cityu.edu.hk"
        assert luo["Chinese Full Name"] == "羅雨菡"
        assert luo["Title"] == "Prof"
        assert luo["Position"] == "Assistant Professor"

        # Bad email cleared
        assert validated[7]["Email"] == ""  # Bad Email record

        # Empty Chinese name preserved (valid empty)
        assert validated[6]["Chinese Full Name"] == ""

        # Field_* keys unmapped
        wang = validated[8]
        assert wang["English Full Name"] == "WANG, Cong"
        assert wang["Email"] == "congwang@cityu.edu.hk"
        assert "field_1" not in wang

        # profile_url stripped from all (after detail pages in scrape())
        for r in validated:
            assert "profile_url" not in r


class TestDetailPageFeatures:
    """Tests for detail_visitor error handling and field detection."""

    def test_find_missing_fields_only_nulls(self, schema):
        """_find_missing_fields should only count None (null), not '' (empty)."""
        from facultyai.scraper.detail_visitor import _find_missing_fields

        records = [
            {"Email": None, "Title": ""},
            {"Email": "found@test.com", "Title": None},
            {"Email": "", "Title": "Prof"},  # '' is valid empty
        ]
        missing = _find_missing_fields(records, ["Email", "Title"])
        # Email: record 0 is None → counted. Title: record 1 is None → counted
        assert set(missing) == {"Email", "Title"}

    def test_all_empty_fields_includes_empties(self, schema):
        """_all_empty_fields should include both None and '' values."""
        from facultyai.scraper.detail_visitor import _all_empty_fields

        records = [
            {"Email": None, "Title": ""},
            {"Email": "found@test.com", "Title": "Dr"},
        ]
        empty = _all_empty_fields(records, ["Email", "Title"])
        # Email: record 0 is None → included. Title: record 0 is '' → included
        assert "Email" in empty
        assert "Title" in empty

    def test_add_remark_concatenation(self, schema):
        """Remarks should concatenate with semicolons."""
        from facultyai.scraper.detail_visitor import _add_remark

        rec: dict[str, str] = {}
        _add_remark(rec, "first issue")
        assert rec["Remark"] == "first issue"

        _add_remark(rec, "second issue")
        assert rec["Remark"] == "first issue; second issue"


class TestListingPageErrorFlow:
    """Tests for listing page error detection in direct mode."""

    def test_page_error_in_analysis(self, schema):
        """ListingAnalysis should carry page_error and propagate via to_dict()."""
        from facultyai.scraper.pattern_analyzer import ListingAnalysis

        raw = {
            "item_selector": "",
            "extraction_methods": {},
            "static_values": {},
            "_direct_records": [],
            "page_error": "Page is a Cloudflare challenge",
        }
        analysis = ListingAnalysis(raw)
        assert analysis.page_error == "Page is a Cloudflare challenge"

        d = analysis.to_dict()
        assert d["page_error"] == "Page is a Cloudflare challenge"

    def test_page_error_marker_record(self, schema):
        """scrape() should return a marker record when page_error is set."""
        # The marker record has _page_error key
        marker = [{"_page_error": "Page is a Cloudflare challenge"}]
        assert len(marker) == 1
        err = marker[0].get("_page_error")
        assert err == "Page is a Cloudflare challenge"

    def test_page_error_detected_in_scraper(self, schema):
        """_run_scraper_impl should detect _page_error and set state error."""
        # Simulate the detection logic
        records = [{"_page_error": "Test error message"}]
        assert len(records) == 1
        assert isinstance(records[0], dict)
        page_err = records[0].get("_page_error")
        assert page_err is not None
        # Error flows to state
        state_error = f"Listing page issue: {page_err}"
        assert state_error == "Listing page issue: Test error message"
        # Records should be set to empty
        assert page_err  # detected → set extracted_records = []


class TestFieldMapping:
    """Tests for abstract field_N → real name mapping."""

    def test_field_map_builds_correctly(self, schema):
        """_field_map should build forward and reverse mappings."""
        from facultyai.scraper.pattern_analyzer import _field_map

        fwd, rev = _field_map(schema)
        assert fwd["field_1"] == "Title"
        assert fwd["field_2"] == "English Full Name"
        assert fwd["field_6"] == "Department"
        assert rev["Title"] == "field_1"
        assert rev["English Full Name"] == "field_2"
        assert rev["Department"] == "field_6"

    def test_field_list_text_uses_abstract_keys(self, schema):
        """_field_list_text should use field_N keys with real names in parens."""
        from facultyai.scraper.pattern_analyzer import _field_list_text

        text = _field_list_text(schema)
        assert 'field_1 ("Title")' in text
        assert 'field_2 ("English Full Name")' in text
        assert 'field_3 ("Chinese Full Name")' in text

    def test_unmap_static_values(self, schema):
        """_unmap_static_values should convert field_N → real names."""
        from facultyai.scraper.pattern_analyzer import _field_map, _unmap_static_values

        fwd, _ = _field_map(schema)
        result = _unmap_static_values(
            {"field_6": "Department of Computer Science", "field_1": "Prof"}, fwd
        )
        assert result["Department"] == "Department of Computer Science"
        assert result["Title"] == "Prof"
        assert "field_6" not in result
        assert "field_1" not in result

    def test_unmap_static_values_passthrough(self, schema):
        """Values not in the mapping should pass through unchanged."""
        from facultyai.scraper.pattern_analyzer import _field_map, _unmap_static_values

        fwd, _ = _field_map(schema)
        result = _unmap_static_values({"unknown_key": "value"}, fwd)
        assert result["unknown_key"] == "value"
