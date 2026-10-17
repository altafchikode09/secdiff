"""XBRL delta table from SEC company-facts data.

Rules that keep this honest:
- Values are matched per filing by accession number, so each column is
  exactly what that filing reported — never a later restatement pulled
  silently from a newer filing.
- A concept is only delta'd when BOTH filings report the SAME concept
  (same alias). Concepts that appear in one filing but not the other are
  almost always retags; they are listed separately as unmapped and NEVER
  silently merged or treated as if the value went to zero.
- The tracked catalogue is the curated 100 concepts in concepts.py.

Matcher (v2, hardened after empirical miss-hunting on AAPL/MSFT/XOM/WMT
10-Ks — see README "XBRL coverage"):
- Unit series are chosen by the concept's expected unit kind
  (money -> USD, shares -> shares, per-share -> USD/shares, pure -> pure),
  never by dictionary order. (v1 picked the first unit series; Walmart's
  EPS facts carry an empty "pure" series before "USD/shares", which made
  v1 report EPS as missing.)
- Aliases are scored across BOTH filings: an alias reported by both
  filings always beats a higher-priority alias reported by only one.
- Aliases are searched in both the us-gaap and dei taxonomies (Walmart
  reports shares outstanding only as dei EntityCommonStockSharesOutstanding).
- "flow" concepts require a fiscal-year-duration fact (~300-400 days) in
  the filing; v1 silently fell back to whatever period was available,
  which could have labelled a quarter as a year. No annual fact -> the
  concept is reported as not matched, with the reason kept.
- If several DIFFERENT values survive for the target period (same unit,
  same period end), the matcher prefers fp="FY" facts; if ambiguity
  remains it refuses to guess and marks the concept ambiguous rather
  than picking an arbitrary first entry as v1 did.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .concepts import CONCEPTS as _CATALOGUE
from .concepts import Concept

# Backwards-compatible name: the tracked catalogue (rich Concept objects).
CONCEPTS = _CATALOGUE

_UNIT_PREFERENCE: dict[str, list[str]] = {
    "money": ["USD"],
    "shares": ["shares"],
    "per-share": ["USD/shares"],
    "pure": ["pure"],
}


@dataclass
class DeltaRow:
    label: str
    concept: str
    unit: str = "USD"
    old: float | None = None
    new: float | None = None
    change: float | None = None
    pct: float | None = None
    category: str = ""


@dataclass
class XbrlResult:
    rows: list[DeltaRow]
    unmapped: list[str]   # human-readable "Concept (only in newer/older filing)"
    matched: int = 0      # concepts reported by BOTH filings (== len(rows))
    considered: int = 0   # concepts in the tracked catalogue


def _span_days(e: dict) -> int | None:
    try:
        y1, m1, d1 = map(int, e["start"].split("-"))
        y2, m2, d2 = map(int, e["end"].split("-"))
        return (date(y2, m2, d2) - date(y1, m1, d1)).days
    except Exception:
        return None


def _pick_value(entries: list[dict], accession: str, kind: str) -> tuple[float | None, str]:
    """Pick the fiscal-year (flow) or period-end (stock) value one filing
    reported for one concept/unit series.

    Returns (value, status) where status is "ok", "absent-in-filing",
    "no-annual" (flow facts exist for this filing but none spans
    a fiscal year) or "ambiguous" (several different values for the
    target period and no fp="FY" tie-break).
    """
    mine = [e for e in entries if e.get("accn") == accession]
    if not mine:
        return None, "absent-in-filing"
    # De-duplicate identical facts (SEC company-facts repeats a fact when
    # it carries a "frame"; the value is the same, so this is safe).
    seen: dict[tuple, dict] = {}
    for e in mine:
        seen[(e.get("start"), e.get("end"), e.get("val"))] = e
    mine = list(seen.values())
    if kind == "flow":
        annual = [e for e in mine
                  if e.get("start") and (s := _span_days(e)) is not None
                  and 300 <= s <= 400]
        if not annual:
            return None, "no-annual"
        target_end = max(e["end"] for e in annual)
        pool = [e for e in annual if e["end"] == target_end]
        longest = max(_span_days(e) or 0 for e in pool)
        pool = [e for e in pool if (_span_days(e) or 0) == longest]
    else:  # stock: instant at the latest period end the filing reports
        target_end = max(e["end"] for e in mine)
        pool = [e for e in mine if e["end"] == target_end]
    values = {float(e["val"]) for e in pool}
    if len(values) == 1:
        return values.pop(), "ok"
    fy_values = {float(e["val"]) for e in pool if e.get("fp") == "FY"}
    if len(fy_values) == 1:
        return fy_values.pop(), "ok"
    return None, "ambiguous"


def _alias_result(units_by_taxonomy: dict[str, dict], concept: Concept,
                  old_accession: str, new_accession: str):
    """Score one alias across both filings and both taxonomies.

    Returns (best, reasons). ``best`` is None or a tuple
    (score, unit, old_v, new_v, old_status, new_status, alias, taxonomy)
    with score 2 = both filings matched, 1 = one filing, 0 = neither.
    The per-side pick statuses let the caller distinguish "not in that
    filing at all" (a retag) from "in that filing, but not safely
    matchable" (no annual-duration fact / ambiguous values).
    """
    best = None
    reasons: list[str] = []
    for alias in concept.aliases:
        for taxonomy, tax in units_by_taxonomy.items():
            node = tax.get(alias)
            if node is None:
                continue
            units = node.get("units", {})
            pref = _UNIT_PREFERENCE.get(concept.unit, [])
            unit_order = [u for u in pref if u in units] + \
                         [u for u in units if u not in pref]
            for unit in unit_order:
                entries = units.get(unit, [])
                old_v, old_s = _pick_value(entries, old_accession, concept.kind)
                new_v, new_s = _pick_value(entries, new_accession, concept.kind)
                score = (old_v is not None) + (new_v is not None)
                if score and (best is None or score > best[0]):
                    best = (score, unit, old_v, new_v, old_s, new_s,
                            alias, taxonomy)
                    if score == 2:
                        return best, reasons
                for st in (old_s, new_s):
                    if st not in ("ok", "absent-in-filing"):
                        reasons.append(f"{alias}: {st}")
    return best, reasons


def analyse_concepts(facts: dict, old_accession: str, new_accession: str,
                     concepts: list[Concept] | None = None) -> list[dict]:
    """Per-concept match analysis used by compute_delta and diagnostics.

    Each entry: label, category, status in {"matched", "one-sided",
    "absent", "ambiguous", "no-annual"}, plus alias/unit/old/new when
    found and a human-readable reason otherwise.
    """
    concepts = _CATALOGUE if concepts is None else concepts
    by_taxonomy = {
        "us-gaap": facts.get("facts", {}).get("us-gaap", {}),
        "dei": facts.get("facts", {}).get("dei", {}),
    }
    out: list[dict] = []
    for concept in concepts:
        best, reasons = _alias_result(by_taxonomy, concept,
                                      old_accession, new_accession)
        if best is not None and best[0] == 2:
            _, unit, old_v, new_v, _, _, alias, taxonomy = best
            out.append(dict(label=concept.label, category=concept.category,
                            status="matched", alias=alias, taxonomy=taxonomy,
                            unit=unit, old=old_v, new=new_v))
        elif best is not None and best[0] == 1:
            _, unit, old_v, new_v, old_s, new_s, alias, taxonomy = best
            missing_status = new_s if new_v is None else old_s
            if missing_status in ("no-annual", "ambiguous"):
                # The concept IS in both filings; one side simply has no
                # safely matchable fact (e.g. MSFT tags goodwill
                # impairment only as instant-dated annual-test facts).
                out.append(dict(label=concept.label, category=concept.category,
                                status=missing_status, alias=alias,
                                reason=f"{alias}: {missing_status}"))
            else:
                out.append(dict(label=concept.label, category=concept.category,
                                status="one-sided", alias=alias, taxonomy=taxonomy,
                                unit=unit, old=old_v, new=new_v,
                                side="newer only" if new_v is not None else "older only"))
        elif reasons:
            status = "ambiguous" if any("ambiguous" in r for r in reasons) \
                else "no-annual"
            out.append(dict(label=concept.label, category=concept.category,
                            status=status, alias=None,
                            reason="; ".join(sorted(set(reasons)))))
        else:
            out.append(dict(label=concept.label, category=concept.category,
                            status="absent", alias=None))
    return out


def compute_delta(facts: dict, old_accession: str, new_accession: str) -> XbrlResult:
    analysis = analyse_concepts(facts, old_accession, new_accession)
    rows: list[DeltaRow] = []
    unmapped: list[str] = []
    for r in analysis:
        if r["status"] == "matched":
            old_v, new_v = r["old"], r["new"]
            change = new_v - old_v
            pct = (change / abs(old_v) * 100.0) if old_v else None
            rows.append(DeltaRow(r["label"], r["alias"], r["unit"],
                                 old_v, new_v, change, pct, r["category"]))
        elif r["status"] == "one-sided":
            side_word = "newer" if r["side"].startswith("newer") else "older"
            unmapped.append(
                f"{r['label']} ({r['alias']}) — reported in {side_word} filing only; "
                "likely retagged or newly disclosed, NOT merged into the delta table")
        elif r["status"] in ("ambiguous", "no-annual"):
            unmapped.append(
                f"{r['label']} — reported by both filings but not safely matchable "
                f"({r['reason']}); NOT guessed")
    return XbrlResult(rows=rows, unmapped=unmapped,
                      matched=len(rows), considered=len(analysis))


def fmt_money(v: float | None, unit: str = "USD") -> str:
    if v is None:
        return "—"
    if unit == "shares":
        return f"{v:,.0f}"
    if unit == "USD/shares":
        return f"${v:,.2f}"
    if unit == "pure":
        return f"{v * 100:,.1f}%" if abs(v) <= 1.5 else f"{v:,.2f}"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e9:
        return f"{sign}${a/1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a/1e6:,.1f}M"
    if a >= 1e3:
        return f"{sign}${a/1e3:,.1f}K"
    return f"{sign}${a:,.0f}"
