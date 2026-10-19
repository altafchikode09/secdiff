"""The shortcut layer: section name resolution and diff filtering.

These are the functions behind --section, --risk-only, --deletions-only
and --top N. They are pure (no I/O) so they are unit-tested offline.
"""
from __future__ import annotations

from .diffing import SectionDiff

# Friendly name -> 10-K item number
SECTION_ALIASES = {
    "business": "1",
    "risk": "1A",
    "risks": "1A",
    "riskfactors": "1A",
    "risk-factors": "1A",
    "properties": "2",
    "legal": "3",
    "mdna": "7",
    "mda": "7",
    "md&a": "7",
    "management": "7",
    "marketrisk": "7A",
    "financials": "8",
    "financial": "8",
    "statements": "8",
    "controls": "9A",
    "directors": "10",
    "compensation": "11",
    "pay": "11",
    "exhibits": "15",
}


SECTION_ALIASES_10Q = {
    "financials": "1", "financial": "1", "statements": "1",
    "mdna": "2", "mda": "2", "md&a": "2", "management": "2",
    "marketrisk": "3", "controls": "4",
    "risk": "1A", "risks": "1A", "riskfactors": "1A", "risk-factors": "1A",
    "legal": "1",  # Part II Item 1 in full numbering; heading scan finds Part I first
}


def resolve_section(name: str, form: str = "10-K") -> str:
    """'risk' -> '1A', 'mdna' -> '7' (10-K) or '2' (10-Q), '1A' -> '1A'."""
    key = name.strip().lower().replace(" ", "")
    aliases = SECTION_ALIASES_10Q if form == "10-Q" else SECTION_ALIASES
    if key in aliases:
        return aliases[key]
    if key in SECTION_ALIASES:
        return SECTION_ALIASES[key]
    up = name.strip().upper()
    from .sections import ITEM_TITLES, ITEM_TITLES_8K
    if up in ITEM_TITLES or up in ITEM_TITLES_8K:
        return up
    raise SystemExit(
        f"Unknown section '{name}'. Try: risk, mdna, business, legal, "
        f"financials, controls, or an item number like 1A or 7.")


def filter_diffs(diffs: list[SectionDiff],
                 only_item: str | None = None,
                 deletions_only: bool = False,
                 top: int | None = None) -> list[SectionDiff]:
    """Apply --section / --deletions-only / --top filters.

    Sections are ranked by total change (biggest first); --top N keeps the
    N biggest individual changes overall, preserving the deletions-first
    order inside each section.
    """
    out: list[SectionDiff] = []
    for d in diffs:
        if only_item and d.item.upper() != only_item.upper():
            continue
        hunks = [h for h in d.hunks if not deletions_only or h.kind == "deletion"]
        out.append(SectionDiff(item=d.item, title=d.title, hunks=hunks,
                               old_sentences=d.old_sentences,
                               new_sentences=d.new_sentences))
    out.sort(key=lambda s: -s.total_change)
    if top is not None:
        budget = top
        trimmed: list[SectionDiff] = []
        for d in out:
            if budget <= 0:
                break
            keep = d.hunks[:budget]
            budget -= len(keep)
            trimmed.append(SectionDiff(item=d.item, title=d.title, hunks=keep,
                                       old_sentences=d.old_sentences,
                                       new_sentences=d.new_sentences))
        out = trimmed
    return [d for d in out if d.hunks]
