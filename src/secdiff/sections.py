"""Split a 10-K / 10-Q plain text into ITEM sections.

Section detection is heuristic: filings are not perfectly uniform. Every
split therefore carries a confidence label, and sections that could not be
found are reported as missing instead of being silently guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Canonical 10-K items we try to locate (10-Q reuses 1, 1A, 2, 3, 4 of Part I).
ITEM_TITLES = {
    "1": "Business",
    "1A": "Risk Factors",
    "1B": "Unresolved Staff Comments",
    "2": "Properties",
    "3": "Legal Proceedings",
    "4": "Mine Safety Disclosures",
    "5": "Market for Registrant's Common Equity",
    "6": "Selected Financial Data",
    "7": "Management's Discussion and Analysis (MD&A)",
    "7A": "Quantitative and Qualitative Disclosures About Market Risk",
    "8": "Financial Statements and Supplementary Data",
    "9": "Changes in and Disagreements With Accountants",
    "9A": "Controls and Procedures",
    "9B": "Other Information",
    "10": "Directors, Executive Officers and Corporate Governance",
    "11": "Executive Compensation",
    "12": "Security Ownership",
    "13": "Certain Relationships and Related Transactions",
    "14": "Principal Accounting Fees and Services",
    "15": "Exhibits and Financial Statement Schedules",
}

# Matches both TOC lines ("ITEM 1A. Risk Factors") and body headings in
# inline-XBRL filings, where "Item 1A." often sits alone on its own line.
_HEADING_RE = re.compile(
    r"(?im)^\s*ITEM\s+(1[0-5]|1A|1B|7A|9A|9B|[1-9])\b\s*[.\-—:)]?\s*([A-Z][^\n]{0,90})?$"
)


@dataclass
class Section:
    item: str
    title: str
    text: str
    confidence: str  # "high" | "low"


@dataclass
class SplitResult:
    sections: dict[str, Section]
    missing: list[str]           # canonical items not found
    confidence: str              # overall: "high" | "low"


def split_sections(text: str, form: str = "10-K") -> SplitResult:
    matches = list(_HEADING_RE.finditer(text))
    # A table of contents lists every item early in the document; the real
    # body headings come later. Heuristic: if we see the same item twice,
    # keep the LAST occurrence as the body start.
    by_item: dict[str, re.Match] = {}
    for m in matches:
        by_item[m.group(1).upper()] = m
    ordered = sorted(by_item.items(), key=lambda kv: kv[1].start())

    sections: dict[str, Section] = {}
    for idx, (item, m) in enumerate(ordered):
        start = m.end()
        end = ordered[idx + 1][1].start() if idx + 1 < len(ordered) else len(text)
        body = text[start:end].strip()
        if len(body) < 40:  # heading with no real body — treat as not found
            continue
        title = ITEM_TITLES.get(item) or ((m.group(2) or "").strip().title() or f"Item {item}")
        conf = "high" if len(body) > 500 else "low"
        sections[item] = Section(item=item, title=title, text=body, confidence=conf)

    if form == "10-Q":  # 10-Q Part I numbering differs from 10-K
        titles_10q = {"1": "Financial Statements",
                      "2": "Management's Discussion and Analysis (MD&A)",
                      "3": "Quantitative and Qualitative Disclosures About Market Risk",
                      "4": "Controls and Procedures",
                      "1A": "Risk Factors"}
        for item, s in sections.items():
            if item in titles_10q:
                s.title = titles_10q[item]
    expected = ["1", "1A", "7", "8"] if form == "10-K" else ["1", "1A", "2"]
    missing = [i for i in expected if i not in sections]
    overall = "high" if len(sections) >= 4 and not missing else "low"
    return SplitResult(sections=sections, missing=missing, confidence=overall)
