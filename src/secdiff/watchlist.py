"""Per-account watchlists for the secdiff website.

Storage: a `watchlist` table in the SAME SQLite file as the accounts
(secdiff.db in the site's cache dir), so accounts and watchlists are
always deployed, backed up and wiped together. Rows are keyed by
(user_id, cik): a company is one row per account however many ticker
spellings or share classes resolve to it, and one account's list is
never visible to another.

A watchlist row stores only what the add flow verified against EDGAR
at add time: the resolved CIK, the company name EDGAR returned, and
the ticker/name the user typed. It stores no report data — the
"last-known score" shown beside an entry is read from the majors
cache at render time, and when nothing has been computed the page
says so instead of inventing a number.

Email alerts are NOT part of this module: mail delivery is not
switched on for the site yet, and the watchlist page says alerts are
coming when email is enabled rather than pretending they exist.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL,
    cik INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL,
    PRIMARY KEY (user_id, cik)
);
"""


class WatchlistStore:
    """SQLite-backed per-account watchlists (see module docstring)."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript(_SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def add(self, user_id: int, cik: int, ticker: str,
            company: str) -> bool:
        """Add a company to a user's watchlist. Returns True when a
        new row was created, False when the company (by CIK) was
        already on the list."""
        with self._lock, self._db() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO watchlist "
                "(user_id, cik, ticker, company, added_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, cik, ticker, company,
                 datetime.now(timezone.utc).isoformat()))
            db.commit()
            return cur.rowcount > 0

    def remove(self, user_id: int, cik: int) -> bool:
        """Remove a company from THIS user's list only. Returns True
        when a row was deleted."""
        with self._lock, self._db() as db:
            cur = db.execute(
                "DELETE FROM watchlist WHERE user_id = ? AND cik = ?",
                (user_id, cik))
            db.commit()
            return cur.rowcount > 0

    def list(self, user_id: int) -> list[dict]:
        """The user's entries, in the order they were added."""
        with self._db() as db:
            rows = db.execute(
                "SELECT cik, ticker, company, added_at FROM watchlist "
                "WHERE user_id = ? ORDER BY added_at, cik",
                (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def ciks(self, user_id: int) -> set[int]:
        return {row["cik"] for row in self.list(user_id)}
