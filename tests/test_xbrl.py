from secdiff.xbrl import compute_delta, fmt_money

OLD_ACC = "0000000001-24-000001"
NEW_ACC = "0000000001-25-000001"


def entry(accn, val, end="2024-09-30", start="2023-10-01"):
    return {"accn": accn, "val": val, "end": end, "start": start,
            "form": "10-K", "fp": "FY", "filed": "2025-01-01"}


FACTS = {
    "facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            entry(OLD_ACC, 100_000_000_000, end="2023-09-30", start="2022-10-01"),
            entry(NEW_ACC, 120_000_000_000),
            # comparative value inside the NEW filing must not leak in:
            entry(NEW_ACC, 100_000_000_000, end="2023-09-30", start="2022-10-01"),
        ]}},
        # retagged tax concept: only in the newer filing -> unmapped, not merged
        "IncomeTaxExpenseBenefit": {"units": {"USD": [
            entry(NEW_ACC, 5_000_000_000)]}},
        # a tracked concept only in the newer filing:
        "GrossProfit": {"units": {"USD": [entry(NEW_ACC, 50_000_000_000)]}},
    }}
}


def test_delta_uses_each_filings_own_reported_value():
    r = compute_delta(FACTS, OLD_ACC, NEW_ACC)
    rev = [row for row in r.rows if row.label == "Revenue"]
    assert len(rev) == 1
    assert rev[0].old == 100_000_000_000
    assert rev[0].new == 120_000_000_000
    assert rev[0].pct == 20.0


def test_retagged_concept_is_unmapped_never_merged():
    r = compute_delta(FACTS, OLD_ACC, NEW_ACC)
    labels = [row.label for row in r.rows]
    assert "Gross profit" not in labels  # only in newer filing -> not a delta
    assert any("Gross profit" in u and "newer filing only" in u
               for u in r.unmapped)


def test_fmt_money():
    assert fmt_money(1_500_000_000) == "$1.50B"
    assert fmt_money(-2_500_000) == "-$2.5M"
    assert fmt_money(None) == "—"
