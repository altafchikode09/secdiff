"""Bundled offline demo.

The demo data in demo_data/ is REAL: the plain text of two real Apple Inc.
10-K filings and the matching slice of SEC XBRL company-facts, fetched from
SEC EDGAR on 2026-10-17 (accession numbers and dates are in
demo_data/meta.json). Nothing is synthetic and nothing is mocked — `secdiff
demo` runs the exact same split/diff/XBRL code as a live run, just with the
network replaced by these bundled files.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "demo_data"


def load_demo() -> dict:
    meta = json.loads((DATA_DIR / "meta.json").read_text())
    old_text = gzip.decompress((DATA_DIR / "old_10k.txt.gz").read_bytes()).decode("utf-8")
    new_text = gzip.decompress((DATA_DIR / "new_10k.txt.gz").read_bytes()).decode("utf-8")
    facts = json.loads(gzip.decompress((DATA_DIR / "facts.json.gz").read_bytes()).decode("utf-8"))
    return {"meta": meta, "old_text": old_text, "new_text": new_text, "facts": facts}
