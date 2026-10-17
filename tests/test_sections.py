from secdiff.sections import split_sections

BODY = "This section contains a substantial body of discussion text. " * 30

FILING = f"""TABLE OF CONTENTS
ITEM 1. Business
ITEM 1A. Risk Factors

ITEM 1. BUSINESS
{BODY}
ITEM 1A. RISK FACTORS
{BODY}
ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS
{BODY}
ITEM 8. FINANCIAL STATEMENTS
{BODY}
"""


def test_sections_found_with_confidence():
    r = split_sections(FILING, "10-K")
    assert "1" in r.sections and "1A" in r.sections
    assert "7" in r.sections and "8" in r.sections
    assert r.sections["1A"].title == "Risk Factors"
    assert r.confidence == "high"


def test_toc_does_not_become_the_section():
    r = split_sections(FILING, "10-K")
    assert len(r.sections["1"].text) > 500


def test_missing_reported_not_guessed():
    r = split_sections("No items here at all, just a paragraph of text.", "10-K")
    assert "1A" in r.missing
    assert r.confidence == "low"
