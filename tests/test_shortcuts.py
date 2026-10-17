import pytest

from secdiff.diffing import Hunk, SectionDiff
from secdiff.shortcuts import filter_diffs, resolve_section
from secdiff.summarize import extractive_summary


def mk(item, *kinds_sizes):
    return SectionDiff(item=item, title=f"Item {item}", hunks=[
        Hunk(kind=k, old="o" * s if k != "addition" else "",
             new="n" * s if k != "deletion" else "", size=s)
        for k, s in kinds_sizes])


def test_resolve_section_aliases():
    assert resolve_section("risk") == "1A"
    assert resolve_section("mdna") == "7"
    assert resolve_section("business") == "1"
    assert resolve_section("1A") == "1A"
    assert resolve_section("7") == "7"
    with pytest.raises(SystemExit):
        resolve_section("nonsense")


def test_risk_only_via_only_item():
    diffs = [mk("1", ("addition", 50)), mk("1A", ("deletion", 80))]
    out = filter_diffs(diffs, only_item="1A")
    assert [d.item for d in out] == ["1A"]


def test_deletions_only():
    diffs = [mk("1A", ("deletion", 80), ("addition", 90), ("modification", 10))]
    out = filter_diffs(diffs, deletions_only=True)
    assert all(h.kind == "deletion" for d in out for h in d.hunks)
    assert sum(len(d.hunks) for d in out) == 1


def test_top_n_keeps_biggest_sections_first_and_limits_total():
    diffs = [mk("1", ("addition", 10), ("addition", 10)),
             mk("7", ("deletion", 500), ("deletion", 400))]
    out = filter_diffs(diffs, top=1)
    total = sum(len(d.hunks) for d in out)
    assert total == 1
    assert out[0].item == "7"  # biggest-change section wins the budget


def test_extractive_summary_uses_only_computed_hunks():
    old = ("The company faces intense competition in all of its markets today. "
           "Supply shortages could materially harm the business and results.")
    new = "The company faces intense competition in all of its markets today."
    from secdiff.diffing import diff_section
    d = diff_section("1A", "Risk Factors", old, new)
    lines = extractive_summary([d])
    assert any("DELETED" in ln and "Supply shortages" in ln for ln in lines)
