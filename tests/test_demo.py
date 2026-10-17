"""The bundled demo must be REAL filings and must run fully offline."""
from secdiff.cli import build_diffs
from secdiff.demo import load_demo
from secdiff.xbrl import compute_delta


def test_demo_bundle_is_real_aapl_filings():
    b = load_demo()
    meta = b["meta"]
    assert meta["ticker"] == "AAPL"
    assert meta["new"]["accession"] == "0000320193-25-000079"
    assert meta["old"]["accession"] == "0000320193-24-000123"
    assert "sec.gov" in meta["new"]["url"]
    assert len(b["new_text"]) > 100_000 and len(b["old_text"]) > 100_000


def test_demo_pipeline_finds_deletions_and_numbers():
    b = load_demo()
    diffs, conf = build_diffs(b["old_text"], b["new_text"], "10-K")
    deletions = sum(1 for d in diffs for h in d.hunks if h.kind == "deletion")
    assert deletions > 0
    assert conf == "high"
    x = compute_delta(b["facts"], b["meta"]["old"]["accession"],
                      b["meta"]["new"]["accession"])
    rev = [r for r in x.rows if r.label == "Revenue"]
    assert rev and rev[0].old == 391_035_000_000 and rev[0].new == 416_161_000_000
