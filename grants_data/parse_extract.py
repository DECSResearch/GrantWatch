"""Parse the Grants.gov XML database extract into pipeline-ready records.

The daily extract (see ``download_extract.gen_extract``) is a single large XML
document. This module streams it with ``iterparse`` (the file is ~300 MB, so
it is never fully loaded into memory) and maps each opportunity into the same
UPPER_SNAKE record shape produced by the search_export JSON endpoint, so the
existing date/status/keyword filters and CSV writer work unchanged.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree

from logs.status_logger import logger

_NS = "{http://apply.grants.gov/system/OpportunityDetail-V1.0}"
_SYNOPSIS_TAG = f"{_NS}OpportunitySynopsisDetail_1_0"
_FORECAST_TAG = f"{_NS}OpportunityForecastDetail_1_0"

_FUNDING_ACTIVITY_CATEGORIES = {
    "ACA": "Affordable Care Act",
    "AG": "Agriculture",
    "AR": "Arts",
    "BC": "Business and Commerce",
    "CD": "Community Development",
    "CP": "Consumer Protection",
    "DPR": "Disaster Prevention and Relief",
    "ED": "Education",
    "ELT": "Employment, Labor and Training",
    "EN": "Energy",
    "ENV": "Environment",
    "FN": "Food and Nutrition",
    "HL": "Health",
    "HO": "Housing",
    "HU": "Humanities",
    "IS": "Information and Statistics",
    "ISS": "Income Security and Social Services",
    "LJL": "Law, Justice and Legal Services",
    "NR": "Natural Resources",
    "O": "Other",
    "OZ": "Opportunity Zone Benefits",
    "RA": "Recovery Act",
    "RD": "Regional Development",
    "ST": "Science and Technology and other Research and Development",
    "T": "Transportation",
}

_FUNDING_INSTRUMENT_TYPES = {
    "CA": "Cooperative Agreement",
    "G": "Grant",
    "O": "Other",
    "PC": "Procurement Contract",
}

_OPPORTUNITY_CATEGORIES = {
    "C": "Continuation",
    "D": "Discretionary",
    "E": "Earmark",
    "M": "Mandatory",
    "O": "Other",
}


def _text(element: ElementTree.Element, tag: str) -> Optional[str]:
    child = element.find(f"{_NS}{tag}")
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _texts(element: ElementTree.Element, tag: str) -> List[str]:
    return [
        child.text.strip()
        for child in element.findall(f"{_NS}{tag}")
        if child.text and child.text.strip()
    ]


def _format_date(value: Optional[str]) -> Optional[str]:
    """Convert the extract's MMDDYYYY dates to MM/DD/YYYY."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%m%d%Y").strftime("%m/%d/%Y")
    except ValueError:
        return value


def _lookup_all(codes: List[str], table: Dict[str, str]) -> List[str]:
    return [table.get(code, code) for code in codes]


def _map_record(element: ElementTree.Element, status: str) -> Dict[str, object]:
    opportunity_id = _text(element, "OpportunityID")
    funding_categories = _lookup_all(
        _texts(element, "CategoryOfFundingActivity"), _FUNDING_ACTIVITY_CATEGORIES
    )
    instrument_types = _lookup_all(
        _texts(element, "FundingInstrumentType"), _FUNDING_INSTRUMENT_TYPES
    )
    opportunity_category = _text(element, "OpportunityCategory")

    if status == "Forecasted":
        posted_date = _format_date(_text(element, "EstimatedSynopsisPostDate"))
        close_date = _format_date(_text(element, "EstimatedApplicationDueDate"))
    else:
        posted_date = _format_date(_text(element, "PostDate"))
        close_date = _format_date(_text(element, "CloseDate"))

    return {
        "OPPORTUNITY_ID": opportunity_id,
        "OPPORTUNITY_NUMBER": _text(element, "OpportunityNumber"),
        "OPPORTUNITY_TITLE": _text(element, "OpportunityTitle"),
        "OPPORTUNITY_STATUS": status,
        "OPPORTUNITY_CATEGORY": _OPPORTUNITY_CATEGORIES.get(
            opportunity_category or "", opportunity_category
        ),
        "OPPORTUNITY_NUMBER_LINK": (
            f"https://www.grants.gov/search-results-detail/{opportunity_id}"
            if opportunity_id
            else None
        ),
        "OPPORTUNITY_URL": (
            f"https://www.grants.gov/search-results-detail/{opportunity_id}"
            if opportunity_id
            else None
        ),
        "AGENCY_CODE": _text(element, "AgencyCode"),
        "AGENCY_NAME": _text(element, "AgencyName"),
        "AGENCY": _text(element, "AgencyName"),
        "CATEGORY_OF_FUNDING_ACTIVITY": "; ".join(funding_categories) or None,
        "FUNDING_CATEGORIES": funding_categories,
        "FUNDING_CATEGORY_EXPLANATION": _text(element, "CategoryExplanation"),
        "FUNDING_INSTRUMENT_TYPE": "; ".join(instrument_types) or None,
        "ASSISTANCE_LISTINGS": "; ".join(_texts(element, "CFDANumbers")) or None,
        "ESTIMATED_TOTAL_FUNDING": _text(element, "EstimatedTotalProgramFunding"),
        "EXPECTED_NUMBER_OF_AWARDS": _text(element, "ExpectedNumberOfAwards"),
        "AWARD_CEILING": _text(element, "AwardCeiling"),
        "AWARD_FLOOR": _text(element, "AwardFloor"),
        "COST_SHARING_MATCH_REQUIRMENT": _text(element, "CostSharingOrMatchingRequirement"),
        "LINK_TO_ADDITIONAL_INFORMATION": _text(element, "AdditionalInformationURL"),
        "GRANTOR_CONTACT": _text(element, "GrantorContactText"),
        "GRANTOR_CONTACT_EMAIL": _text(element, "GrantorContactEmail"),
        "POSTED_DATE": posted_date,
        "CLOSE_DATE": close_date,
        "ARCHIVE_DATE": _format_date(_text(element, "ArchiveDate")),
        "LAST_UPDATED_DATETIME": _format_date(_text(element, "LastUpdatedDate")),
        "VERSION": _text(element, "Version"),
        "FUNDING_DESCRIPTION": _text(element, "Description"),
        "ADDITIONAL_INFORMATION_ON_ELIGIBILITY": _text(
            element, "AdditionalInformationOnEligibility"
        ),
    }


def process_extract_xml(
    file_path: str | Path,
    include_forecasted: bool = True,
) -> List[Dict[str, object]]:
    """Stream-parse an extract XML file into a list of pipeline records."""
    path = Path(file_path)
    if not path.exists():
        logger("error", f"Extract XML not found at {path}")
        return []

    records: List[Dict[str, object]] = []
    synopsis_count = 0
    forecast_count = 0

    try:
        for _event, element in ElementTree.iterparse(str(path), events=("end",)):
            if element.tag == _SYNOPSIS_TAG:
                records.append(_map_record(element, "Posted"))
                synopsis_count += 1
                element.clear()
            elif element.tag == _FORECAST_TAG:
                if include_forecasted:
                    records.append(_map_record(element, "Forecasted"))
                forecast_count += 1
                element.clear()
    except ElementTree.ParseError as exc:
        logger("error", f"Failed to parse extract XML at {path}: {exc}")
        return []

    logger(
        "info",
        f"Parsed extract {path.name}: {synopsis_count} posted, {forecast_count} forecasted "
        f"({len(records)} records returned)",
    )
    return records
