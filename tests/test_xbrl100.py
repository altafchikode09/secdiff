"""Tests for the 100-concept catalogue and the v2 matcher.

Each test encodes a miss class found empirically on real AAPL/MSFT/XOM/WMT
10-Ks (see README "XBRL coverage") — they run offline on synthetic facts.
"""
from secdiff.concepts import CONCEPTS
from secdiff.xbrl import analyse_concepts, compute_delta, fmt_money

OLD = "0000000001-24-000001"
NEW = "0000000001-25-000001"
FY_OLD = {"start": "2023-10-01", "end": "2024-09-30"}
FY_NEW = {"start": "2024-10-01", "end": "2025-09-30"}


def entry(accn, val, fp="FY", **kw):
    e = {"accn": accn, "val": val, "form": "10-K", "fp": fp,
         "filed": "2025-11-01"}
    e.update(kw)
    return e


def facts(us_gaap=None, dei=None):
    f = {"facts": {"us-gaap": us_gaap or {}}}
    if dei is not None:
        f["facts"]["dei"] = dei
    return f


def status_of(rows, label):
    return [r for r in rows if r["label"] == label][0]


def test_catalogue_has_exactly_100_unique_concepts():
    assert len(CONCEPTS) == 100
    labels = [c.label for c in CONCEPTS]
    assert len(set(labels)) == 100
    for c in CONCEPTS:
        assert c.aliases and all(c.aliases)
        assert c.kind in ("flow", "stock")
        assert c.unit in ("money", "shares", "per-share", "pure")


def test_unit_kind_beats_dictionary_order_walmart_eps():
    """WMT's EPS facts carry an (empty-here) 'pure' series BEFORE the real
    'USD/shares' series. v1 picked the first series and reported EPS as
    missing; the matcher must choose by expected unit kind."""
    f = facts({"EarningsPerShareBasic": {"units": {
        "pure": [entry(OLD, 0, **FY_OLD)],  # wrong series has junk, not FY values for both
        "USD/shares": [entry(OLD, 2.42, **FY_OLD), entry(NEW, 2.74, **FY_NEW)],
    }}})
    # remove the pure series' usefulness: pure only in old filing
    r = status_of(analyse_concepts(f, OLD, NEW), "EPS (basic)")
    assert r["status"] == "matched"
    assert r["unit"] == "USD/shares"
    assert r["old"] == 2.42 and r["new"] == 2.74


def test_shares_outstanding_found_via_dei_taxonomy():
    """WMT reports shares outstanding only as dei
    EntityCommonStockSharesOutstanding — aliases must search dei too."""
    f = facts({}, dei={"EntityCommonStockSharesOutstanding": {"units": {
        "shares": [entry(OLD, 8_000_000_000, end="2025-01-31", start=None),
                   entry(NEW, 7_900_000_000, end="2026-01-31", start=None)]}}})
    r = status_of(analyse_concepts(f, OLD, NEW), "Shares outstanding")
    assert r["status"] == "matched" and r["taxonomy"] == "dei"
    assert r["new"] == 7_900_000_000


def test_one_sided_alias_does_not_shadow_both_filings_alias():
    """A high-priority alias reported by only one filing must not beat a
    lower-priority alias reported by both."""
    f = facts({
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {
            "USD": [entry(NEW, 999, **FY_NEW)]}},          # newer only
        "Revenues": {"units": {
            "USD": [entry(OLD, 100, **FY_OLD), entry(NEW, 120, **FY_NEW)]}},
    })
    r = status_of(analyse_concepts(f, OLD, NEW), "Revenue")
    assert r["status"] == "matched" and r["alias"] == "Revenues"
    assert (r["old"], r["new"]) == (100, 120)


def test_flow_never_falls_back_to_a_quarter():
    """v1 fell back to any available period; a concept tagged only with
    quarterly durations in a 10-K must be 'no-annual', never a quarter
    dressed up as a year."""
    q_old = {"start": "2024-07-01", "end": "2024-09-30"}
    q_new = {"start": "2025-07-01", "end": "2025-09-30"}
    f = facts({"GrossProfit": {"units": {
        "USD": [entry(OLD, 10, **q_old), entry(NEW, 12, **q_new)]}}})
    r = status_of(analyse_concepts(f, OLD, NEW), "Gross profit")
    assert r["status"] == "no-annual"


def test_stock_picks_latest_instant_and_ignores_startless_flow_noise():
    """End-of-period cash facts are instant (start=None): a stock-kind
    concept must take the latest period-end value from each filing."""
    f = facts({"CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents":
               {"units": {"USD": [
                   entry(OLD, 30, end="2024-09-30", start=None),
                   entry(OLD, 25, end="2023-09-30", start=None),
                   entry(NEW, 36, end="2025-09-27", start=None),
                   entry(NEW, 30, end="2024-09-28", start=None)]}}})
    r = status_of(analyse_concepts(f, OLD, NEW),
                  "Cash & restricted cash, end of period")
    assert r["status"] == "matched"
    assert (r["old"], r["new"]) == (30, 36)


def test_ambiguous_values_are_refused_not_guessed():
    """Two different values for the same target period with no fp='FY'
    tie-break: refuse (v1 silently took the first)."""
    f = facts({"Assets": {"units": {"USD": [
        entry(OLD, 100, end="2024-09-30", start=None, fp=None),
        entry(OLD, 130, end="2024-09-30", start=None, fp=None),
        entry(NEW, 150, end="2025-09-27", start=None)]}}})
    r = status_of(analyse_concepts(f, OLD, NEW), "Total assets")
    assert r["status"] == "ambiguous"
    assert not [row for row in compute_delta(f, OLD, NEW).rows
                if row.label == "Total assets"]


def test_fp_fy_tiebreak_resolves_duplicate_period_values():
    f = facts({"Assets": {"units": {"USD": [
        entry(OLD, 100, end="2024-09-30", start=None, fp="FY"),
        entry(OLD, 130, end="2024-09-30", start=None, fp=None),
        entry(NEW, 150, end="2025-09-27", start=None)]}}})
    r = status_of(analyse_concepts(f, OLD, NEW), "Total assets")
    assert r["status"] == "matched" and r["old"] == 100


def test_instant_only_flow_concept_is_no_annual_not_one_sided():
    """MSFT goodwill-impairment pattern: duration fact in the old filing,
    only instant-dated annual-test facts in the new one. That is
    'no-annual', not a retag."""
    f = facts({"GoodwillImpairmentLoss": {"units": {"USD": [
        entry(OLD, 0, start="2023-07-01", end="2024-06-30"),
        entry(NEW, 0, end="2025-05-01", start=None)]}}})
    r = status_of(analyse_concepts(f, OLD, NEW), "Goodwill impairment")
    assert r["status"] == "no-annual"


def test_fmt_money_new_units():
    assert fmt_money(2.74, "USD/shares") == "$2.74"
    assert fmt_money(0.157, "pure") == "15.7%"
    assert fmt_money(15_000_000, "shares") == "15,000,000"
