"""Unit tests for the XML extract parser and the validator Lambda helpers."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "aws" / "lambda"))

from grants_data.parse_extract import process_extract_xml

_SAMPLE_XML = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <Grants xmlns="http://apply.grants.gov/system/OpportunityDetail-V1.0">
        <OpportunitySynopsisDetail_1_0>
            <OpportunityID>360670</OpportunityID>
            <OpportunityTitle>Sample Grant</OpportunityTitle>
            <OpportunityNumber>SW-26</OpportunityNumber>
            <OpportunityCategory>D</OpportunityCategory>
            <FundingInstrumentType>G</FundingInstrumentType>
            <CategoryOfFundingActivity>CD</CategoryOfFundingActivity>
            <AgencyCode>USDA-RUS</AgencyCode>
            <AgencyName>Rural Utilities Service</AgencyName>
            <PostDate>07012026</PostDate>
            <CloseDate>12312026</CloseDate>
            <Description>Community development funding.</Description>
        </OpportunitySynopsisDetail_1_0>
        <OpportunityForecastDetail_1_0>
            <OpportunityID>360671</OpportunityID>
            <OpportunityTitle>Forecasted Grant</OpportunityTitle>
            <OpportunityNumber>FC-26</OpportunityNumber>
            <AgencyName>Department of Innovation</AgencyName>
            <EstimatedSynopsisPostDate>08012026</EstimatedSynopsisPostDate>
        </OpportunityForecastDetail_1_0>
    </Grants>
    """
)


class TestParseExtract:
    def test_maps_synopsis_and_forecast(self, tmp_path):
        xml_file = tmp_path / "extract.xml"
        xml_file.write_text(_SAMPLE_XML, encoding="utf-8")

        records = process_extract_xml(xml_file)
        assert len(records) == 2

        posted = records[0]
        assert posted["OPPORTUNITY_NUMBER"] == "SW-26"
        assert posted["OPPORTUNITY_STATUS"] == "Posted"
        assert posted["OPPORTUNITY_CATEGORY"] == "Discretionary"
        assert posted["FUNDING_CATEGORIES"] == ["Community Development"]
        assert posted["FUNDING_INSTRUMENT_TYPE"] == "Grant"
        assert posted["AGENCY"] == "Rural Utilities Service"
        assert posted["POSTED_DATE"] == "07/01/2026"
        assert posted["CLOSE_DATE"] == "12/31/2026"
        assert posted["OPPORTUNITY_URL"].endswith("/360670")

        forecast = records[1]
        assert forecast["OPPORTUNITY_STATUS"] == "Forecasted"
        assert forecast["POSTED_DATE"] == "08/01/2026"

    def test_can_exclude_forecasted(self, tmp_path):
        xml_file = tmp_path / "extract.xml"
        xml_file.write_text(_SAMPLE_XML, encoding="utf-8")
        records = process_extract_xml(xml_file, include_forecasted=False)
        assert len(records) == 1
        assert records[0]["OPPORTUNITY_STATUS"] == "Posted"

    def test_missing_file_returns_empty(self, tmp_path):
        assert process_extract_xml(tmp_path / "nope.xml") == []


class TestLambdaEventParsing:
    def test_eventbridge_shape(self):
        import validate_doc

        event = {
            "detail": {
                "bucket": {"name": "bucket-a"},
                "object": {"key": "submissions/s1/req/123-file.pdf"},
            }
        }
        assert validate_doc._extract_object_events(event) == [
            ("bucket-a", "submissions/s1/req/123-file.pdf")
        ]

    def test_s3_notification_shape_is_url_decoded(self):
        import validate_doc

        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "bucket-a"},
                        "object": {"key": "submissions/s1/req/123-my%20file.pdf"},
                    }
                }
            ]
        }
        assert validate_doc._extract_object_events(event) == [
            ("bucket-a", "submissions/s1/req/123-my file.pdf")
        ]

    def test_unknown_shape_yields_nothing(self):
        import validate_doc

        assert validate_doc._extract_object_events({}) == []
        assert validate_doc._extract_object_events({"detail": {}}) == []
