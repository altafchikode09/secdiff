"""Major filers, the curated sector map, and the cached
latest-10-K summary store behind the home page's "Biggest deletes"
section and the /sectors page.

Honesty rules:

- The filer set and the sector map are CURATED, static and labelled
  as such on the pages that use them — they are not full-market
  coverage and never claim to be.
- A summary entry exists only after the real pipeline computed it
  from live EDGAR filings; the store caches the RESULT (deletion
  counts + change-intensity score + the filing pair + the UTC day it
  was checked). Nothing is estimated, and an entry that has not been
  computed yet is shown as "computing", never as a made-up number.
- Entries go stale at the UTC day boundary: a page that finds a stale
  or missing entry shows what is cached and refreshes in the
  background (see secdiff.web).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

# The major-filer set whose latest 10-K deletion counts the home page
# shows. Small on purpose: each entry costs one real pipeline run.
MAJORS: list[str] = ["AAPL", "MSFT", "GOOGL", "TSLA", "XOM", "AMZN",
                     "NVDA", "META"]

# Curated sector labels for well-known tickers (the /sectors universe
# is the major set plus any of these that visitors actually run
# reports on). Plain-language groupings, not a market taxonomy.
SECTOR_MAP: dict[str, str] = {
    "AAPL": "Technology",
    "MSFT": "Technology",
    "NVDA": "Technology",
    "AMD": "Technology",
    "INTC": "Technology",
    "ORCL": "Technology",
    "CRM": "Technology",
    "GOOGL": "Internet & platforms",
    "GOOG": "Internet & platforms",
    "META": "Internet & platforms",
    "AMZN": "Internet & platforms",
    "NFLX": "Internet & platforms",
    "XOM": "Energy",
    "CVX": "Energy",
    "TSLA": "Automakers & EVs",
    "F": "Automakers & EVs",
    "GM": "Automakers & EVs",
    "JPM": "Financials",
    "BAC": "Financials",
    "BRK-B": "Financials",
    "BRK.B": "Financials",
    "V": "Financials",
    "MA": "Financials",
    "WMT": "Retail & consumer",
    "HD": "Retail & consumer",
    "PG": "Retail & consumer",
    "UNH": "Healthcare",
    "JNJ": "Healthcare",
}

# Display order for the sectors page; unlisted sectors sort after,
# alphabetically.
SECTOR_ORDER: list[str] = ["Technology", "Internet & platforms",
                           "Energy", "Automakers & EVs", "Financials",
                           "Retail & consumer", "Healthcare"]


def sector_of(ticker: str) -> str | None:
    return SECTOR_MAP.get(ticker.upper())


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class MajorsStore:
    """JSON cache of computed major-filer summaries.

    File shape: {ticker: {ticker, company, cik, deletions,
    modifications, additions, score, band, new_date, old_date,
    checked}}. A missing or corrupt file is treated as empty —
    the cache must never take the site down.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def get(self, ticker: str) -> dict | None:
        entry = self.load().get(ticker.upper())
        return entry if isinstance(entry, dict) else None

    def put(self, entry: dict) -> None:
        """Insert/replace one computed entry (atomic file replace)."""
        with self._lock:
            data = self.load()
            data[entry["ticker"].upper()] = entry
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data), "utf-8")
                import os
                os.replace(tmp, self.path)
            except OSError:
                pass  # best-effort cache; never break a page over it

    @staticmethod
    def is_fresh(entry: dict | None, day: str | None = None) -> bool:
        """An entry is fresh only on the UTC day it was checked."""
        if not entry:
            return False
        return entry.get("checked") == (day or _today_utc())


def sort_entries(entries: list[dict]) -> list[dict]:
    """Biggest deletes first: deletions desc, then score desc, then
    ticker — the ranking used by the home section and /sectors."""
    return sorted(entries,
                  key=lambda e: (-int(e.get("deletions", 0)),
                                 -int(e.get("score", 0)),
                                 str(e.get("ticker", ""))))
