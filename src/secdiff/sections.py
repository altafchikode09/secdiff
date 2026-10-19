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

# 8-K items are decimal-numbered ("Item 2.02 Results of Operations…") and
# mean completely different things from the same leading digit in a 10-K,
# so 8-Ks get their own heading pattern and their own title table. The
# 10-K/10-Q pattern above is deliberately left untouched.
_HEADING_RE_DECIMAL = re.compile(
    r"(?im)^\s*ITEM\s+([1-9]\.\d{2}|1[0-5]|1A|1B|7A|9A|9B|[1-9])\b\s*[.\-—:)]?\s*([A-Z][^\n]{0,90})?$"
)

# Canonical Form 8-K items (SEC Form 8-K item list).
ITEM_TITLES_8K = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety — Reporting of Shutdowns and Patterns of Violations",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate or Increase a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure of Directors or Certain Officers; Election of Directors",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.04": "Temporary Suspension of Trading Under Registrant's Employee Benefit Plans",
    "5.05": "Amendments to the Registrant's Code of Ethics",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "6.01": "ABS Informational and Computational Material",
    "6.02": "Change of Servicer or Trustee",
    "6.03": "Change in Credit Enhancement or Other External Support",
    "6.04": "Failure to Make a Required Distribution",
    "6.05": "Securities Act Updating Disclosure",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}


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
    if form == "8-K":
        heading_re, titles = _HEADING_RE_DECIMAL, ITEM_TITLES_8K
    elif form == "DEF 14A":
        # Proxy items are meeting proposals ("ITEM 1. Election of
        # Directors"), NOT 10-K items — the 10-K title table would call
        # Item 1 "Business", which is simply wrong here. Use no canned
        # titles: the heading's own text (or "Item N") is the title.
        heading_re, titles = _HEADING_RE, {}
    else:
        heading_re, titles = _HEADING_RE, ITEM_TITLES
    matches = list(heading_re.finditer(text))
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
        title = titles.get(item) or ((m.group(2) or "").strip().title() or f"Item {item}")
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
    if form == "10-K":
        expected = ["1", "1A", "7", "8"]
    elif form == "10-Q":
        expected = ["1", "1A", "2"]
    else:
        # 8-K / DEF 14A: there is no canonical item set every filing must
        # carry (an 8-K reports only the items its events need), so
        # nothing is ever reported "missing" for these forms.
        expected = []
    missing = [i for i in expected if i not in sections]
    overall = "high" if len(sections) >= 4 and not missing else "low"
    return SplitResult(sections=sections, missing=missing, confidence=overall)
