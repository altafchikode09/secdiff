"""secdiff web — a small, dependency-free web front end for secdiff.

Run it any of these ways:

    python -m secdiff.web            # module
    secdiff-web                      # console script
    secdiff serve [--host H] [--port P]

then open http://127.0.0.1:8000 — type a ticker, get the filing-diff
report in the browser. Every report is generated **live** from real SEC
EDGAR data at request time, for whatever ticker the visitor enters.
There is deliberately no demo, sample or pre-baked report anywhere on
the site (the CLI's separate offline `secdiff demo` command is a
terminal feature and is not used here).

Design rules, same as the rest of the project:

- **Standard library only** (`http.server`). The package stays
  zero-dependency; this module needs nothing beyond it.
- **No duplicated diff logic.** Every report is computed by the exact
  same pipeline the CLI uses — `cli.live_pipeline` /
  `cli.render_reports` — and rendered by `report.render_html`.
- **EDGAR politeness is inherited.** Live runs go through the package's
  `EdgarClient`: declared User-Agent, pacing well under the SEC's
  10 req/s fair-access limit, and the shared on-disk cache
  (`~/.cache/secdiff`, override with `SECDIFF_CACHE_DIR` or the CLI's
  `--cache-dir` equivalent). A lock serialises report runs so the cache
  is never written concurrently and EDGAR is never hit in parallel.
- **Honest failures.** Unknown tickers and EDGAR outages produce an
  error page that says what happened. Nothing is faked or filled in.
- **Free limit + pricing (website only; the CLI is unlimited).** Each
  visitor (long-lived cookie) gets 4 distinct companies per UTC day
  free; beyond that a paywall page shows the paid plans (Weekly $9,
  Monthly $27, unlimited while active). Payments are not live yet, so
  the paid plans take waitlist signups (name + email, stored locally)
  instead of a fake checkout, and the limit applies to everyone until
  real payments land.
- **Owner stats.** Home visits, reports and signups are counted into
  small daily aggregates (counts only — no IP addresses, no raw logs)
  shown on a hidden `/stats` page visible only to the site account
  named by the `STATS_USERNAME` environment variable; when it is unset,
  the page does not exist for anyone.

Host/port: `--host` / `--port` flags win, then the `HOST` / `PORT`
environment variables (the convention Render/Railway/Heroku use). If
`PORT` is set and `HOST` is not, the server binds `0.0.0.0` so a
platform router can reach it; otherwise it binds `127.0.0.1`.

The module also exposes a plain WSGI `application`, so it can be mounted
under any WSGI server (`gunicorn secdiff.web:application`) unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import urllib.error
import urllib.parse
from contextlib import redirect_stderr
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from . import accounts as _accounts
from .accounts import AccountStore
from .card import build_card_data, render_card_png
from .cli import _filtered_diffs, build_diffs, live_pipeline, \
    one_line_summary, render_reports
from .edgar import EdgarClient
from .export import xbrl_to_csv, xbrl_to_xls
from .icons import manifest_bytes, render_icon_png
from .theme import SITE_CSS, THEME_COOKIE, THEME_INIT_JS, clean_theme, \
    head_block, head_links, html_tag
from .majors import MAJORS, SECTOR_MAP, SECTOR_ORDER, MajorsStore, \
    sector_of, sort_entries
from .patterns import annotate_repeats
from .pdf import render_pdf
from .report import _counts
from .score import compute_score
from .search import clean_search_term
from .watchlist import WatchlistStore

__all__ = ["application", "main", "serve", "run_report",
           "parse_report_form", "ReportParams", "home_page", "error_page",
           "paywall_page", "UsageStore", "FREE_DAILY_COMPANIES",
           "AccountStore", "get_account_store",
           "StatsStore", "get_stats_store", "stats_page",
           "WatchlistStore", "get_watchlist_store",
           "MajorsStore", "get_majors_store",
           "company_page", "trending_page",
           "sectors_page", "watchlist_page", "api_page",
           "THEME_COOKIE"]


# ---------------------------------------------------------------------------
# Form parameters (pure, unit-tested offline)

_QUERY_RE = re.compile(r"^[A-Z][A-Z0-9 .,&\-]{0,59}$")
_FORMS = ("10-K", "10-Q", "8-K", "DEF 14A")


@dataclass
class ReportParams:
    ticker: str                      # upper-cased ticker or company name
    form: str = "10-K"
    risk_only: bool = False
    deletions_only: bool = False
    top: int | None = None
    vs_year: int | None = None       # newer filing's year (default: latest)
    base_year: int | None = None     # older filing's year (default: previous)
    q: str = ""                      # search-in-report term ("" = no search)


def parse_report_form(data: str) -> ReportParams:
    """Parse a urlencoded form body (or query string) into ReportParams.

    Raises ValueError with a visitor-friendly message on bad input.
    """
    qs = urllib.parse.parse_qs(data, keep_blank_values=True)

    def first(name: str, default: str = "") -> str:
        vals = qs.get(name)
        return vals[0].strip() if vals else default

    ticker = " ".join(first("ticker").upper().split())
    if not ticker:
        raise ValueError("Please enter a ticker symbol or company name "
                         "(for example AAPL or Microsoft).")
    if not _QUERY_RE.match(ticker):
        raise ValueError(
            f"“{ticker}” does not look like a ticker or company name. Use "
            "letters, digits, spaces, '.' or '-' (e.g. AAPL, BRK.B, "
            "Berkshire Hathaway).")
    form = first("form", "10-K").upper()
    if form not in _FORMS:
        raise ValueError(f"Form must be one of: {', '.join(_FORMS)}.")

    def parse_year(name: str, label: str) -> int | None:
        raw = first(name)
        if not raw:
            return None
        try:
            year = int(raw)
        except ValueError:
            raise ValueError(f"“{label}” must be a year, e.g. 2024.") \
                from None
        if not 1990 <= year <= 2100:
            raise ValueError(f"“{label}” must be a year between 1990 "
                             "and 2100.")
        return year

    top_raw = first("top")
    top: int | None = None
    if top_raw:
        try:
            top = int(top_raw)
        except ValueError:
            raise ValueError("“Top N biggest changes” must be a whole number.") \
                from None
        if not 1 <= top <= 500:
            raise ValueError("“Top N biggest changes” must be between 1 and 500.")
    return ReportParams(
        ticker=ticker, form=form,
        risk_only=first("risk_only") in ("on", "true", "1"),
        deletions_only=first("deletions_only") in ("on", "true", "1"),
        top=top,
        vs_year=parse_year("vs_year", "Compare year"),
        base_year=parse_year("base_year", "Against year"),
        # clean_search_term raises ValueError on an unsafe/unusable
        # term — the usual honest 400 — and returns "" for no search.
        q=clean_search_term(first("q")))


# ---------------------------------------------------------------------------
# Free daily limit (website only — the CLI is never limited)
#
# Owner's pricing rules: each visitor may view up to FREE_DAILY_COMPANIES
# distinct companies per calendar day (UTC) for free; re-viewing a company
# already counted today is free. Beyond that, paid plans (Weekly $9,
# Monthly $27) unlock unlimited reports — payments are NOT live yet (the
# site takes waitlist signups instead), so until real payments land the
# limit applies to everyone. Counting is by resolved company (CIK), and
# a slot is only consumed AFTER the ticker has successfully resolved —
# unknown tickers and other resolution errors never consume one.

FREE_DAILY_COMPANIES = 4
_VISITOR_COOKIE = "secdiff_vid"


def _cache_dir() -> Path:
    return Path(os.environ["SECDIFF_CACHE_DIR"]) \
        if os.environ.get("SECDIFF_CACHE_DIR") else \
        Path.home() / ".cache" / "secdiff"


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class UsageStore:
    """Per-visitor daily distinct-company counter, persisted as JSON.

    File shape: {visitor_id: {"day": "YYYY-MM-DD", "ciks": [int, ...]}}.
    A missing or corrupt file is treated as empty — counting must never
    take the site down.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), "utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # counting is best-effort; never break a report over it

    def companies_today(self, visitor_id: str, day: str | None = None) -> list:
        day = day or _today_utc()
        entry = self._load().get(visitor_id) or {}
        return list(entry.get("ciks", [])) if entry.get("day") == day else []

    def check_and_add(self, visitor_id: str, cik: int,
                      day: str | None = None) -> bool:
        """True if this company may be viewed now (recording it if new);
        False when the visitor has already used all free companies today."""
        day = day or _today_utc()
        with self._lock:
            data = self._load()
            entry = data.get(visitor_id) or {}
            ciks = list(entry.get("ciks", [])) if entry.get("day") == day else []
            if cik in ciks:
                return True
            if len(ciks) >= FREE_DAILY_COMPANIES:
                return False
            ciks.append(cik)
            data[visitor_id] = {"day": day, "ciks": ciks}
            self._save(data)
            return True


_STORE: UsageStore | None = None
_STORE_LOCK = threading.RLock()   # RLock: getters may nest (watchlist
                                  # store creation needs the account
                                  # store) — a plain Lock deadlocked
                                  # the first cold watchlist call


def get_usage_store() -> UsageStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = UsageStore(_cache_dir() / "web_usage.json")
        return _STORE


def _edgar_client() -> EdgarClient:
    """The site's EDGAR client (declared User-Agent, shared on-disk
    cache, SEC pacing) — one construction for every route."""
    email = os.environ.get("EDGAR_EMAIL") or os.environ.get("SECDIFF_EMAIL") \
        or "you@example.com"
    return EdgarClient(email=email, cache_dir=_cache_dir())


def _resolve_company(params: ReportParams) -> tuple[int, str]:
    """Resolve the visitor's ticker/name to (CIK, company) using the same
    verified EDGAR resolution the CLI uses. Raises SystemExit with the
    usual honest message when nothing matches — the caller turns that
    into the normal error page, and no free slot is consumed."""
    client = _edgar_client()
    with redirect_stderr(io.StringIO()):   # the live run re-emits the notes
        return client.resolve_ticker(params.ticker, form=params.form,
                                     min_count=2)


def _visitor_id(environ) -> tuple[str, bool]:
    """(visitor_id, is_new) from the visitor cookie, minting one if the
    cookie is absent or malformed."""
    raw = environ.get("HTTP_COOKIE", "")
    if raw:
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            jar = SimpleCookie()
        morsel = jar.get(_VISITOR_COOKIE)
        if morsel is not None and re.fullmatch(r"[0-9a-f]{32}", morsel.value):
            return morsel.value, False
    return secrets.token_hex(16), True


def _cookie_header(visitor_id: str) -> tuple[str, str]:
    return ("Set-Cookie",
            f"{_VISITOR_COOKIE}={visitor_id}; Path=/; Max-Age=31536000; "
            "HttpOnly; SameSite=Lax")


# ---------------------------------------------------------------------------
# Theme preference (Phase 5: light/dark)
#
# The visitor's explicit choice lives in a `secdiff_theme` cookie set
# by POST /theme. It is NOT HttpOnly on purpose: the inline theme-init
# script reads it to stamp data-theme on <html> before first paint.
# It carries no identity and no session data — just "light"/"dark".
# Pages rendered for a request read the choice from a request-scoped
# context set at the top of `application`, so the many page functions
# keep their existing signatures.

_THEME_CTX = threading.local()


def _theme_pref(environ) -> str | None:
    """The visitor's stored theme choice for THIS request, or None
    (no cookie / invalid value -> the CSS media query decides)."""
    morsel = _cookies(environ).get(THEME_COOKIE)
    return clean_theme(morsel.value) if morsel is not None else None


def _request_theme() -> str | None:
    return getattr(_THEME_CTX, "value", None)


def _theme_cookie_header(value: str) -> tuple[str, str]:
    return ("Set-Cookie",
            f"{THEME_COOKIE}={value}; Path=/; Max-Age=31536000; "
            "SameSite=Lax")


def _page_open(title: str) -> str:
    """Doctype + <html> + <head> for a site page, in the request's
    theme. `title` is the full <title> text."""
    theme = _request_theme()
    return ("<!DOCTYPE html>\n" + html_tag(theme) +
            head_block(title, theme, _CSS) + "\n<body>")


def _theme_form(next_path: str = "") -> str:
    """The light/dark toggle: a plain POST form (works without
    JavaScript); the active choice is outlined. The visitor returns
    to `next_path` when given (the report page passes its canonical
    URL), else to the page being viewed (recorded for GET requests
    by `application`), else to the home page."""
    current = _request_theme()
    on_light = " on" if current == "light" else ""
    on_dark = " on" if current == "dark" else ""
    if not next_path:
        next_path = getattr(_THEME_CTX, "return_to", "") or ""
    hidden = (f"<input type='hidden' name='next' "
              f"value='{html.escape(next_path, quote=True)}'>"
              if next_path else "")
    return ("<form class='themeform' method='post' action='/theme'>"
            f"{hidden}"
            f"<button type='submit' name='theme' value='light' "
            f"class='themebtn{on_light}' aria-label='Light theme'>"
            "&#9728; Light</button>"
            f"<button type='submit' name='theme' value='dark' "
            f"class='themebtn{on_dark}' aria-label='Dark theme'>"
            "&#9790; Dark</button></form>")


def _footer(note_html: str) -> str:
    """The shared site footer: section links + the legal note."""
    return ("<footer class='sitefoot'>"
            "<nav class='footnav'>"
            "<a href='/'>Reports</a>"
            "<a href='/trending'>Trending</a>"
            "<a href='/sectors'>Sectors</a>"
            "<a href='/watchlist'>Watchlist</a>"
            "<a href='/api'>API</a>"
            "</nav>"
            f"<p class='note'>{note_html}</p></footer>")


# ---------------------------------------------------------------------------
# User accounts (signup / login / sessions / recovery)
#
# Accounts live in a SQLite DB (secdiff.db in the cache dir) via
# secdiff.accounts. Being logged in changes two things: the header shows
# the account links, and the daily free-company counter is keyed by
# username instead of the guest cookie, so it follows the user across
# devices. Paid plans exist as a plan field on the account but do NOT
# unlock anything yet — payments are a later phase, and the free limit
# applies to accounts exactly as it does to guests.

_SESSION_COOKIE = "secdiff_session"
_ACCOUNTS: AccountStore | None = None


def get_account_store() -> AccountStore:
    global _ACCOUNTS
    with _STORE_LOCK:
        if _ACCOUNTS is None:
            _ACCOUNTS = AccountStore(_cache_dir() / "secdiff.db")
        return _ACCOUNTS


def _cookies(environ) -> SimpleCookie:
    jar = SimpleCookie()
    raw = environ.get("HTTP_COOKIE", "")
    if raw:
        try:
            jar.load(raw)
        except Exception:
            jar = SimpleCookie()
    return jar


def _session_token(environ) -> str:
    morsel = _cookies(environ).get(_SESSION_COOKIE)
    return morsel.value if morsel is not None else ""


def _current_user(environ) -> dict | None:
    token = _session_token(environ)
    if not token:
        return None
    return get_account_store().user_for_session(token)


def _session_cookie_header(token: str) -> tuple[str, str]:
    return ("Set-Cookie",
            f"{_SESSION_COOKIE}={token}; Path=/; Max-Age=2592000; "
            "HttpOnly; SameSite=Lax")


def _clear_session_cookie_header() -> tuple[str, str]:
    return ("Set-Cookie",
            f"{_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")


def _header(user: dict | None) -> str:
    """Top bar shown on every page: brand + the shared section nav
    (Reports, Trending, Sectors, Watchlist, API), the account links
    and the light/dark theme toggle."""
    sections = ("<a href='/'>Reports</a>"
                "<a href='/trending'>Trending</a>"
                "<a href='/sectors'>Sectors</a>"
                "<a href='/watchlist'>Watchlist</a>"
                "<a href='/api'>API</a>")
    if user:
        account = (
            f"<span>Hi, <strong>{html.escape(user['username'])}</strong>"
            "</span>"
            "<a href='/account'>My account</a>"
            "<form method='post' action='/logout' style='display:inline'>"
            "<button type='submit' class='linkbtn'>Log out</button>"
            "</form>")
    else:
        account = ("<a href='/login'>Log in</a>"
                   "<a class='btn' href='/signup'>Create account</a>")
    return (f"<header class='topbar'><a class='brand' href='/'>secdiff</a>"
            f"<nav>{sections}{account}{_theme_form()}</nav></header>")


# ---------------------------------------------------------------------------
# Traffic stats (owner-only /stats page)
#
# Deliberately minimal counting, stored as daily aggregates in the SAME
# SQLite file as the accounts (so the two are always deployed, backed
# up and wiped together):
#
# - stats_events(kind, day, key, count) — 'pageview' (every home page
#   load), 'visit' (unique visitors: bumped only the first time a
#   visitor cookie is seen that day), 'report' (successful report
#   generations, key = the report's ticker) and 'signup' (new accounts).
# - stats_seen(day, vhash) — one-way hashes used ONLY to settle "have we
#   counted this visitor today?". The hash mixes the day in, so it
#   cannot link a visitor across days; the raw cookie value and any IP
#   address are never stored, and rows older than yesterday are pruned.
#
# Counting is best-effort: every recording call is wrapped so a stats
# failure can never break the page being served.

_STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS stats_events (
    kind TEXT NOT NULL,
    day TEXT NOT NULL,
    key TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, day, key)
);
CREATE TABLE IF NOT EXISTS stats_seen (
    day TEXT NOT NULL,
    vhash TEXT NOT NULL,
    PRIMARY KEY (day, vhash)
);
"""


class StatsStore:
    """Aggregated daily event counts in the accounts SQLite DB."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript(_STATS_SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _bump(db, kind: str, day: str, key: str, n: int = 1) -> None:
        db.execute(
            "INSERT INTO stats_events (kind, day, key, count) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(kind, day, key) "
            "DO UPDATE SET count = count + excluded.count",
            (kind, day, key, n))

    def record(self, kind: str, key: str = "",
               day: str | None = None) -> None:
        """Atomically add one to an event counter."""
        day = day or _today_utc()
        with self._lock, self._db() as db:
            self._bump(db, kind, day, key)
            db.commit()

    def record_visit(self, visitor_id: str,
                     day: str | None = None) -> None:
        """Count a unique daily visitor: bump 'visit' only the first
        time this visitor is seen on `day`."""
        day = day or _today_utc()
        vhash = hashlib.sha256(
            f"{day}|{visitor_id}".encode("utf-8")).hexdigest()
        keep_from = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        with self._lock, self._db() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO stats_seen (day, vhash) "
                "VALUES (?, ?)", (day, vhash))
            if cur.rowcount:
                self._bump(db, "visit", day, "")
            # Yesterday's hashes are kept for safety; anything older can
            # never settle a "first visit today?" question again.
            db.execute("DELETE FROM stats_seen WHERE day < ?", (keep_from,))
            db.commit()

    def daily(self, days: int = 14) -> list:
        """Per-day rows for the last `days` days, newest first:
        [{day, visits, pageviews, reports, signups}, ...]."""
        today = datetime.now(timezone.utc).date()
        since = (today - timedelta(days=days - 1)).isoformat()
        with self._db() as db:
            rows = db.execute(
                "SELECT day, kind, SUM(count) AS n FROM stats_events "
                "WHERE day >= ? GROUP BY day, kind", (since,)).fetchall()
        by_day: dict = {}
        for r in rows:
            by_day.setdefault(r["day"], {})[r["kind"]] = r["n"]
        out = []
        for i in range(days):
            d = (today - timedelta(days=i)).isoformat()
            k = by_day.get(d, {})
            out.append({"day": d, "visits": k.get("visit", 0),
                        "pageviews": k.get("pageview", 0),
                        "reports": k.get("report", 0),
                        "signups": k.get("signup", 0)})
        return out

    def totals(self) -> dict:
        """All-time totals per kind: {visits, pageviews, reports, signups}."""
        with self._db() as db:
            rows = db.execute(
                "SELECT kind, SUM(count) AS n FROM stats_events "
                "GROUP BY kind").fetchall()
        d = {r["kind"]: r["n"] for r in rows}
        return {"visits": d.get("visit", 0),
                "pageviews": d.get("pageview", 0),
                "reports": d.get("report", 0),
                "signups": d.get("signup", 0)}

    def top_companies(self, limit: int = 10) -> list:
        """Most-reported companies: [(ticker, report_count), ...]."""
        with self._db() as db:
            rows = db.execute(
                "SELECT key, SUM(count) AS n FROM stats_events "
                "WHERE kind = 'report' AND key != '' GROUP BY key "
                "ORDER BY n DESC, key ASC LIMIT ?", (limit,)).fetchall()
        return [(r["key"], r["n"]) for r in rows]

    def report_count(self, ticker: str) -> int:
        """Reports generated for one ticker on this site (all time)."""
        with self._db() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(count), 0) AS n FROM stats_events "
                "WHERE kind = 'report' AND key = ?",
                (ticker,)).fetchone()
        return int(row["n"]) if row else 0

    def total_accounts(self) -> int:
        """Number of registered accounts (users table, same DB)."""
        try:
            with self._db() as db:
                row = db.execute(
                    "SELECT COUNT(*) AS n FROM users").fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.Error:
            return 0


_STATS: StatsStore | None = None


def get_stats_store() -> StatsStore:
    global _STATS
    if _STATS is None:
        accounts = get_account_store()      # same DB file, on purpose
        with _STORE_LOCK:
            if _STATS is None:
                _STATS = StatsStore(accounts.path)
    return _STATS


def _record(kind: str, key: str = "",
            visitor_id: str | None = None) -> None:
    """Best-effort stats recording. A counting failure must NEVER break
    the request being served, so every error is swallowed here."""
    try:
        store = get_stats_store()
        if kind == "visit":
            store.record_visit(visitor_id or "")
        else:
            store.record(kind, key)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Major-filer summaries (home "Biggest deletes" + /sectors) and watchlists
#
# A major's summary (latest 10-K deletion counts + score) costs one real
# pipeline run, so results are cached per UTC day in a JSON store and a
# page NEVER blocks on computing one: it renders what is cached, marks
# the rest "computing", and a single background thread refreshes the
# stale/missing entries. A failed refresh changes nothing — the entry
# simply stays absent or stale, and the page says so.

_MAJORS: MajorsStore | None = None
_WATCHLIST: WatchlistStore | None = None
_REFRESH_LOCK = threading.Lock()
_refreshing = False
# Background refreshes are started only by the real server (serve()
# flips this on). A bare WSGI import — tests, other servers — renders
# the cached values and the honest "computing" state instead of
# spawning threads behind the caller's back.
_AUTO_REFRESH_MAJORS = False


def get_majors_store() -> MajorsStore:
    global _MAJORS
    with _STORE_LOCK:
        if _MAJORS is None:
            _MAJORS = MajorsStore(_cache_dir() / "biggest_deletes.json")
        return _MAJORS


def get_watchlist_store() -> WatchlistStore:
    global _WATCHLIST
    if _WATCHLIST is None:
        # Resolve the account store BEFORE taking the lock: it takes
        # the same lock, and nesting the two used to deadlock the
        # first cold call (found in Phase 4 live testing).
        db_path = get_account_store().path      # same DB file, on purpose
        with _STORE_LOCK:
            if _WATCHLIST is None:
                _WATCHLIST = WatchlistStore(db_path)
    return _WATCHLIST


def _major_summary(ticker: str) -> dict:
    """Compute one major filer's latest-10-K summary through the same
    pipeline the reports use. Raises whatever the pipeline raises —
    the refresh worker treats any failure as "leave the cache alone".
    """
    info, diffs, xbrl = _compute_data(ReportParams(ticker=ticker,
                                                   form="10-K"))
    d, m, a = _counts(diffs)
    sc = compute_score(diffs, xbrl)
    return {"ticker": info.ticker, "company": info.company,
            "cik": info.cik, "deletions": d, "modifications": m,
            "additions": a, "score": sc.total, "band": sc.band,
            "new_date": info.new_date, "old_date": info.old_date,
            "checked": _today_utc()}


def _refresh_majors_worker(tickers: list,
                           store: MajorsStore | None = None) -> None:
    """Background refresh of stale/missing major summaries, written
    into the store captured when the refresh was kicked off. Strictly
    best-effort: any failure (EDGAR down, unknown ticker) leaves the
    cached entry untouched."""
    global _refreshing
    store = store or get_majors_store()
    try:
        for ticker in tickers:
            try:
                store.put(_major_summary(ticker))
            except Exception:
                pass
    finally:
        with _REFRESH_LOCK:
            _refreshing = False


def _maybe_refresh_majors(extra_tickers=()) -> dict:
    """Return the cached major summaries, kicking off ONE background
    refresh for entries that are missing or stale (checked before
    today, UTC). `extra_tickers` widens the refresh set (the /sectors
    universe) beyond the MAJORS list."""
    global _refreshing
    store = get_majors_store()
    entries = store.load()
    want = list(MAJORS)
    for t in extra_tickers:
        if t not in want:
            want.append(t)
    stale = [t for t in want if not store.is_fresh(entries.get(t))]
    if stale and _AUTO_REFRESH_MAJORS:
        with _REFRESH_LOCK:
            if not _refreshing:
                _refreshing = True
                threading.Thread(target=_refresh_majors_worker,
                                 args=(stale, store),
                                 daemon=True).start()
    return entries


def _waitlist_count() -> int:
    """Number of entries in waitlist.jsonl (0 when there is no file)."""
    try:
        text = (_cache_dir() / "waitlist.jsonl").read_text("utf-8")
        return sum(1 for line in text.splitlines() if line.strip())
    except OSError:
        return 0


def stats_page(user: dict, daily: list, totals: dict, top: list,
               accounts_count: int, waitlist_count: int) -> str:
    def data_row(cells) -> str:
        return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

    day_rows = "".join(
        data_row([html.escape(r["day"]), r["visits"], r["pageviews"],
                  r["reports"], r["signups"]]) for r in daily)
    if top:
        top_rows = "".join(
            data_row([html.escape(ticker), count])
            for ticker, count in top)
    else:
        top_rows = ("<tr><td colspan='2' class='note'>"
                    "No reports run yet.</td></tr>")
    inner = f"""
<h1>Site stats</h1>
<p class="tagline">Owner-only page. Counts only — no IP addresses and
no raw logs are stored; a unique visitor is a visitor cookie counted
once per day.</p>
<div class="tablewrap"><table class="kv">
  <tr><td>Unique visitors (all time)</td><td>{totals['visits']}</td></tr>
  <tr><td>Home pageviews (all time)</td><td>{totals['pageviews']}</td></tr>
  <tr><td>Reports run (all time)</td><td>{totals['reports']}</td></tr>
  <tr><td>Signups (all time)</td><td>{totals['signups']}</td></tr>
  <tr><td>Accounts</td><td>{accounts_count}</td></tr>
  <tr><td>Waitlist entries</td><td>{waitlist_count}</td></tr>
</table></div>
<h2>Last 14 days</h2>
<div class="tablewrap"><table class="data">
<tr><th>Date (UTC)</th><th>Unique visitors</th><th>Pageviews</th>
<th>Reports</th><th>Signups</th></tr>
{day_rows}
</table></div>
<h2>Top companies by reports</h2>
<div class="tablewrap"><table class="data">
<tr><th>Company</th><th>Reports</th></tr>
{top_rows}
</table></div>
"""
    return _shell("Site stats", user, inner)


def _stats(start_response, method: str, user: dict | None):
    """GET /stats — the owner-only traffic page.

    Visible ONLY to the signed-in site account whose username matches
    the STATS_USERNAME environment variable (case-insensitive). When the
    variable is unset the page exists for no one; everyone else gets the
    plain 404, so the route does not reveal itself."""
    owner = (os.environ.get("STATS_USERNAME") or "").strip()
    if (not owner or user is None
            or user["username"].lower() != owner.lower()):
        return _error(start_response, "404 Not Found", "Page not found",
                      "There is no page at /stats.", user)
    if method not in ("GET", "HEAD"):
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the browser.", user)
    store = get_stats_store()
    return _respond(start_response, "200 OK",
                    stats_page(user, store.daily(14), store.totals(),
                               store.top_companies(10),
                               store.total_accounts(), _waitlist_count()))


def _download(start_response, environ, method: str,
              user: dict | None, visitor_id: str, cookie: list,
              fmt: str):
    """GET /report.csv and /report.xls — download a report's XBRL
    numbers table.

    Same rules as /report itself: the ticker is resolved first (unknown
    tickers error out honestly, no slot consumed), the company counts
    toward the same daily free limit by CIK whatever the form, and the
    file carries exactly the values of the on-page table (export.py
    formats with the same code the report uses).
    """
    if method not in ("GET", "HEAD"):
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the download links on "
                      "a report page.", user)
    params = parse_report_form(environ.get("QUERY_STRING", ""))
    cik, _company = _resolve_company(params)
    if not get_usage_store().check_and_add(
            _usage_key(user, visitor_id), cik):
        return _respond(start_response, "402 Payment Required",
                        paywall_page(user), extra_headers=cookie)
    summary, _page, xbrl = _compute_report(params, user, annotate=False)
    _record("report", _report_key(summary, params))
    stem = (f"secdiff-{params.ticker}-{params.form}"
            .lower().replace(" ", "-").replace("/", "-"))
    if fmt == "csv":
        body, ctype, ext = xbrl_to_csv(xbrl), "text/csv; charset=utf-8", "csv"
    else:
        body, ctype, ext = (xbrl_to_xls(xbrl),
                            "application/vnd.ms-excel; charset=utf-8", "xls")
    return _respond(
        start_response, "200 OK", body, ctype,
        extra_headers=[("Content-Disposition",
                        f'attachment; filename="{stem}-numbers.{ext}"'),
                       *cookie])


def _report_stem(params: ReportParams, info=None) -> str:
    """Filename stem shared by the PDF and card downloads."""
    ticker = info.ticker if info is not None else params.ticker
    return (f"secdiff-{ticker}-{params.form}"
            .lower().replace(" ", "-").replace("/", "-"))


def _pdf_download(start_response, environ, method: str,
                  user: dict | None, visitor_id: str, cookie: list):
    """GET /report.pdf — the report's summary PDF.

    Same accounting as /report itself: resolve first (unknown tickers
    error honestly, no slot consumed), then the same daily free limit
    by CIK. The PDF summarises the whole (option-filtered) report —
    header, counts, score, top deletions (see secdiff.pdf)."""
    if method not in ("GET", "HEAD"):
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the download links on "
                      "a report page.", user)
    params = parse_report_form(environ.get("QUERY_STRING", ""))
    cik, _company = _resolve_company(params)
    if not get_usage_store().check_and_add(
            _usage_key(user, visitor_id), cik):
        return _respond(start_response, "402 Payment Required",
                        paywall_page(user), extra_headers=cookie)
    info, diffs, xbrl = _compute_data(params)
    _record("report", info.ticker)
    body = render_pdf(info, diffs, xbrl)
    filename = f"{_report_stem(params, info)}-{info.new_date}.pdf"
    return _respond(
        start_response, "200 OK", body, "application/pdf",
        extra_headers=[("Content-Disposition",
                        f'attachment; filename="{filename}"'),
                       *cookie])


def _card(start_response, environ, method: str,
          user: dict | None, visitor_id: str, cookie: list):
    """GET /card — the report's share card as a downloadable PNG.

    Same accounting as /report: resolve first, then the daily free
    limit by CIK. An unknown ticker/company is a plain 404 here (the
    card simply does not exist), never a fake image."""
    if method not in ("GET", "HEAD"):
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the share-card link on "
                      "a report page.", user)
    params = parse_report_form(environ.get("QUERY_STRING", ""))
    try:
        cik, _company = _resolve_company(params)
    except SystemExit as exc:
        msg = str(exc.code) if exc.code else "Company not found."
        return _error(start_response, "404 Not Found", "Company not found",
                      msg, user)
    if not get_usage_store().check_and_add(
            _usage_key(user, visitor_id), cik):
        return _respond(start_response, "402 Payment Required",
                        paywall_page(user), extra_headers=cookie)
    info, diffs, xbrl = _compute_data(params)
    _record("report", info.ticker)
    host = environ.get("HTTP_HOST") or "secdiff.onrender.com"
    report_url = (f"https://{host}/report?"
                  f"{_canonical_query(info, params)}")
    body = render_card_png(build_card_data(info, diffs, xbrl, report_url))
    filename = f"{_report_stem(params, info)}-card.png"
    return _respond(
        start_response, "200 OK", body, "image/png",
        extra_headers=[("Content-Disposition",
                        f'attachment; filename="{filename}"'),
                       *cookie])


# ---------------------------------------------------------------------------
# Running a report through the shared CLI pipeline

_RUN_LOCK = threading.Lock()


def _cli_args(params: ReportParams) -> SimpleNamespace:
    """The args namespace the shared pipeline functions expect (mirrors
    the CLI defaults: compare the latest filing against the previous
    one, or the two explicitly chosen filing years)."""
    return SimpleNamespace(
        ticker=params.ticker, form=params.form, years=1,
        vs_year=params.vs_year, base_year=params.base_year,
        risk_only=params.risk_only, deletions_only=params.deletions_only,
        top=params.top, section=None, summarize=False,
        search=params.q or None, search_base_url="",
        email=None,
        cache_dir=os.environ.get("SECDIFF_CACHE_DIR") or None,
        output="secdiff-out")


def _pin_years(q: dict, info) -> None:
    """Pin the compared pair's filing years into a canonical query
    dict — but only when the two years differ. When both filings are
    from the same year (routine for 8-Ks) a year pair cannot express
    the pair (and an equal pair is rejected as a same-year mistake),
    so the link falls back to the default latest-vs-prior pair."""
    if info.new_date[:4] != info.old_date[:4]:
        q["vs_year"] = info.new_date[:4]
        q["base_year"] = info.old_date[:4]


def _numbers_query(info) -> str:
    """Canonical query for the numbers downloads: real ticker + form +
    the compared pair, so the file reproduces THIS report's table."""
    q = {"ticker": info.ticker, "form": info.form}
    _pin_years(q, info)
    return urllib.parse.urlencode(q)


def _canonical_query(info, params: ReportParams) -> str:
    """The canonical report query: the share/PDF/card links' target.

    Real (resolved) ticker + form + the compared filing pair + every
    display option, so opening the URL reproduces the exact same
    report, counts included. The search term is deliberately NOT part
    of it — a share link names the report, not one visitor's search
    view of it."""
    q = {"ticker": info.ticker, "form": info.form}
    _pin_years(q, info)
    if params.risk_only:
        q["risk_only"] = "1"
    if params.deletions_only:
        q["deletions_only"] = "1"
    if params.top is not None:
        q["top"] = str(params.top)
    return urllib.parse.urlencode(q)


def _annotate_patterns(params: ReportParams, info, diffs) -> None:
    """Mark deletion hunks that repeat a deletion from the
    immediately-earlier filing pair ("Also deleted in the previous
    cycle", see secdiff.patterns).

    The earlier pair is the one ending at THIS report's older filing:
    (older, the filing before it). Best-effort by design — any failure
    (fewer than 3 filings, EDGAR hiccup) simply leaves the report
    unannotated; a missing note must never break or falsify a report.
    The link pins the earlier pair's filing years; when both were
    filed in the same year a year pair cannot name the pair, so the
    note is shown without a link rather than with a wrong one.
    """
    try:
        # A real live run always resolves the ticker through EDGAR
        # first, which caches this CIK's submissions file in the
        # shared cache. If it is not there, this report did not come
        # from the live pipeline (a stubbed/offline context) and there
        # is no earlier pair to consult — stay silent, and offline.
        subs_cache = _cache_dir() / f"submissions_{info.cik:010d}.json"
        if not subs_cache.exists():
            return
        client = _edgar_client()
        filings = client.all_filings(info.cik, params.form)
        idx = next((i for i, f in enumerate(filings)
                    if f.accession == info.old_accession), None)
        if idx is None or idx + 1 >= len(filings):
            return
        newer_prev, older_prev = filings[idx], filings[idx + 1]
        earlier_diffs, _conf = build_diffs(
            client.filing_text(info.cik, older_prev),
            client.filing_text(info.cik, newer_prev), params.form)
        url = ""
        if newer_prev.filing_date[:4] != older_prev.filing_date[:4]:
            q = {"ticker": info.ticker, "form": params.form,
                 "vs_year": newer_prev.filing_date[:4],
                 "base_year": older_prev.filing_date[:4]}
            url = f"/report?{urllib.parse.urlencode(q)}"
        annotate_repeats(diffs, earlier_diffs, url)
    except Exception:
        pass


def _compute_report(params: ReportParams,
                    user: dict | None = None,
                    annotate: bool = True):
    """Run the shared pipeline once.

    Returns (one_line_summary, full_html_page, xbrl_result). Pipeline
    failures propagate: SystemExit with a human-readable message
    (unknown ticker, not enough filings, unknown year),
    urllib.error.URLError (EDGAR unreachable), or anything else — the
    HTTP layer turns them into honest error pages.
    """
    args = _cli_args(params)
    notes = io.StringIO()
    with _RUN_LOCK, redirect_stderr(notes):
        info, diffs, xbrl = live_pipeline(args)
        if annotate:
            # Annotate the RAW hunks: the renderer's filters share the
            # same Hunk objects, so notes survive filtering.
            _annotate_patterns(params, info, diffs)
        canonical = _canonical_query(info, params)
        if params.q:
            # The banner's "show the full report" link: this exact
            # report, without the search filter.
            args.search_base_url = f"/report?{canonical}"
        _md, page, counts = render_reports(args, info, diffs, xbrl)
    summary = one_line_summary(info, counts, xbrl)
    downloads = _numbers_query(info) if xbrl.rows else None
    return summary, _wrap_report(page, notes.getvalue().strip(), user,
                                 downloads, canonical, params.q), xbrl


def _compute_data(params: ReportParams):
    """Run the shared pipeline for the non-HTML surfaces (PDF, share
    card): returns (info, filtered_diffs, xbrl_result) — the diffs are
    filtered by exactly the same options logic the HTML report uses
    (cli._filtered_diffs), and the search term is ignored: a PDF or a
    card always summarises the whole (option-filtered) report."""
    args = _cli_args(params)
    args.search = None
    notes = io.StringIO()
    with _RUN_LOCK, redirect_stderr(notes):
        info, diffs, xbrl = live_pipeline(args)
        diffs = _filtered_diffs(args, diffs)
    return info, diffs, xbrl


def run_report(params: ReportParams,
               user: dict | None = None) -> tuple[str, str]:
    """Compute a report with the shared pipeline.

    Returns (one_line_summary, full_html_page)."""
    summary, page, _xbrl = _compute_report(params, user)
    return summary, page


_SHARE_JS = """
<script>
document.getElementById('copy-share').addEventListener('click', function () {
  var input = document.getElementById('share-url');
  var url = input.value;
  if (url.charAt(0) === '/') { url = location.origin + url; }
  function done() {
    var b = document.getElementById('copy-share');
    b.textContent = 'Copied \\u2713';
    setTimeout(function () { b.textContent = 'Copy share link'; }, 2000);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(done, function () {
      input.value = url; input.select(); document.execCommand('copy'); done();
    });
  } else { input.value = url; input.select(); document.execCommand('copy'); done(); }
});
</script>
"""


def _wrap_report(report_html: str, notes: str,
                 user: dict | None = None,
                 downloads: str | None = None,
                 canonical: str = "",
                 search_q: str = "") -> str:
    """Add a way back home (and any honest stderr notes, e.g. the ticker
    resolved to a different EDGAR entity) to the shared report page,
    together with the download links, the share-link box and the
    search-in-report form."""
    account_link = ("<a href='/account'>My account</a>" if user
                    else "<a href='/login'>Log in</a>")
    nav = ("<div class='reporttools'>"
           "<a href='/'>&larr; New secdiff search</a>"
           f" &nbsp;·&nbsp; {account_link}")
    if canonical:
        nav += (f" &nbsp;·&nbsp; <a href='/report.pdf?{canonical}'>"
                "Download PDF</a>"
                f" &nbsp;·&nbsp; <a href='/card?{canonical}'>"
                "Share card (PNG)</a>")
    if downloads:
        nav += (f" &nbsp;·&nbsp; <a href='/report.csv?{downloads}'>"
                "Download numbers (CSV)</a>"
                f" &nbsp;·&nbsp; <a href='/report.xls?{downloads}'>"
                "Download numbers (Excel)</a>")
    # The theme toggle lives here too, returning to this exact
    # report (its canonical URL) after the switch.
    nav += _theme_form(f"/report?{canonical}" if canonical else "")
    nav += "</div>"
    if canonical:
        share_url = f"/report?{canonical}"
        hidden = "".join(
            f"<input type='hidden' name='{html.escape(k, quote=True)}' "
            f"value='{html.escape(v, quote=True)}'>"
            for k, v in urllib.parse.parse_qsl(canonical))
        nav += (
            "<div class='sharebox'>"
            "<label for='share-url' style='font-weight:600'>Share this "
            "report:</label> "
            f"<input type='text' id='share-url' readonly "
            f"value='{html.escape(share_url, quote=True)}'> "
            "<button type='button' id='copy-share'>"
            "Copy share link</button>"
            "<form method='get' action='/report'>"
            f"{hidden}"
            "<label for='q' style='font-weight:600'>Search in this "
            "report:</label> "
            f"<input type='text' class='qinput' id='q' name='q' "
            f"maxlength='80' "
            f"value='{html.escape(search_q, quote=True)}' "
            "placeholder='e.g. lawsuit, going concern'> "
            "<button type='submit'>Search</button>"
            "</form></div>" + _SHARE_JS)
    if notes:
        items = "".join(f"<div>{html.escape(line)}</div>"
                        for line in notes.splitlines() if line.strip())
        nav += f"<div class='notesbox'>{items}</div>"
    # The report page joins the site design system: stamp the
    # visitor's theme on <html> and add the shared head links (theme
    # colours, manifest, icons). Standalone report files (CLI) skip
    # this — they carry no data-theme and follow the OS preference.
    theme = _request_theme()
    if theme:
        report_html = report_html.replace(
            "<html lang='en'>", html_tag(theme), 1)
    extra_head = head_links(theme) + THEME_INIT_JS
    if "</head>" in report_html:
        report_html = report_html.replace(
            "</head>", extra_head + "</head>", 1)
    if "<body>" in report_html:
        return report_html.replace("<body>", "<body>" + nav, 1)
    return nav + report_html


# ---------------------------------------------------------------------------
# Pages

# The site stylesheet is the Phase 5 design system (secdiff.theme):
# one palette, light + dark themes, every colour a custom property.
# Kept under this name — the page templates interpolate it.
_CSS = SITE_CSS

_HOME_JS = """
document.getElementById('diff-form').addEventListener('submit', function () {
  document.getElementById('loading').hidden = false;
  var b = document.getElementById('go');
  b.disabled = true; b.textContent = 'Working\\u2026';
});
"""


def _biggest_section(biggest: list | None, pending: bool,
                       checked: str) -> str:
    """The home page's "Biggest deletes" section: cached latest-10-K
    deletion counts for the major filers (see secdiff.majors). Only
    computed entries are listed — a company whose count has not been
    computed yet is never given a placeholder number; the section says
    it is still computing instead."""
    e = html.escape
    if biggest:
        rows = "".join(
            "<tr>"
            f"<td><a href='/company/{e(en['ticker'], quote=True)}'>"
            f"{e(en['ticker'])}</a> — {e(en['company'])}</td>"
            f"<td><strong>{en['deletions']}</strong></td>"
            f"<td>{en['score']}/100 ({e(en['band'])})</td>"
            f"<td>{e(en['new_date'])} vs {e(en['old_date'])}</td>"
            f"<td><a href='/report?ticker={e(en['ticker'], quote=True)}"
            f"&amp;form=10-K'>Read the report</a></td>"
            "</tr>" for en in biggest)
        table = ("<div class='tablewrap'><table class='data'>"
                 "<tr><th>Company</th>"
                 "<th>Deletions</th><th>Change-intensity score</th>"
                 "<th>Latest 10-K vs the one before</th><th></th></tr>"
                 f"{rows}</table></div>")
        when = (f" Counts computed from live SEC EDGAR filings; last "
                f"checked {e(checked)} (UTC)." if checked else "")
        note = ("<p class='note'>Some companies are still being "
                "computed — refresh in a moment for the full table."
                "</p>" if pending else "")
        return (f"<h2>Biggest deletes — major filers</h2>{table}"
                f"<p class='note'>Deletions in each company's latest "
                f"10-K versus the one before it, biggest first.{when} "
                f"See also <a href='/sectors'>sectors</a>.</p>{note}")
    return ("<h2>Biggest deletes — major filers</h2>"
            "<p class='note'>Computing the latest 10-K deletion counts "
            "for the major filers (AAPL, MSFT, GOOGL, TSLA, XOM, AMZN, "
            "NVDA, META) from live SEC EDGAR filings — refresh in a "
            "moment. Nothing is shown here until it has really been "
            "computed.</p>")


def home_page(user: dict | None = None, notice: str = "",
              biggest: list | None = None, biggest_pending: bool = False,
              biggest_checked: str = "") -> str:
    notice_html = (f"<div class='ok'><p>{html.escape(notice)}</p></div>"
                   if notice else "")
    return f"""{_page_open("secdiff — git diff for SEC filings")}
{_header(user)}
{notice_html}
<h1>secdiff</h1>
<p class="tagline"><strong><code>git diff</code> for SEC filings.</strong>
Type a ticker or company name and secdiff fetches the company's latest filing from SEC
EDGAR, fetches the one before it, and shows you exactly what changed —
words and numbers — with <strong>deleted language first</strong>, because
what a company <em>stops saying</em> is often the story.</p>

<div class="card">
<form method="post" action="/report" id="diff-form">
  <div class="row">
    <div>
      <label for="ticker">Ticker or company name</label>
      <input type="text" id="ticker" name="ticker" required maxlength="40"
             placeholder="AAPL or Microsoft" autocomplete="off">
    </div>
    <div>
      <label for="form">Filing</label>
      <select id="form" name="form">
        <option value="10-K" selected>10-K (annual)</option>
        <option value="10-Q">10-Q (quarterly)</option>
        <option value="8-K">8-K (current / event report)</option>
        <option value="DEF 14A">Proxy — DEF 14A (annual meeting)</option>
      </select>
    </div>
  </div>
  <div class="row">
    <div>
      <label for="vs_year">Compare the filing from year
        <span class="note">(optional — blank = latest)</span></label>
      <input type="number" id="vs_year" name="vs_year" min="1990" max="2100"
             placeholder="latest">
    </div>
    <div>
      <label for="base_year">Against the filing from year
        <span class="note">(optional — blank = the one before)</span></label>
      <input type="number" id="base_year" name="base_year" min="1990"
             max="2100" placeholder="previous">
    </div>
  </div>
  <p class="note">Pick two years to compare those exact filings. If a
  year you pick has no filing of that form, the result page says so
  and lists the years that do exist. Proxies (DEF 14A) are diffed in
  words only — they carry no XBRL numbers table. 8-Ks are event
  filings, so the two compared usually cover different events.</p>
  <label class="check"><input type="checkbox" name="risk_only">
    Risk factors only (Item 1A)</label>
  <label class="check"><input type="checkbox" name="deletions_only">
    Deletions only — just the removed text</label>
  <label for="top">Top N biggest changes
    <span class="note">(optional — leave blank for all)</span></label>
  <input type="number" id="top" name="top" min="1" max="500" placeholder="all">
  <button type="submit" id="go">Compare the last two filings &rarr;</button>
</form>
<div id="loading" hidden>
  <div class="spinner"></div>
  <p><strong>Fetching filings from SEC EDGAR&hellip;</strong><br>
  <span class="note">A company fetched for the first time can take up to a
  minute (two full filings plus XBRL data, paced under the SEC's
  fair-access limit). Repeat visits are served from cache and are much
  faster. Please don't close the tab.</span></p>
</div>
</div>

<p class="note">Every report on this site is generated live from real
SEC EDGAR filings at the moment you ask — there are no sample or
pre-generated reports here.</p>

<p class="note">Explore: <a href="/trending">trending on secdiff</a> ·
<a href="/sectors">sectors</a> · <a href="/api">API</a> ·
<a href="/watchlist">your watchlist</a></p>

{_biggest_section(biggest, biggest_pending, biggest_checked)}

<h2>How it works</h2>
<ol>
<li><strong>Fetch</strong> — secdiff resolves your ticker on SEC EDGAR
(verifying the resolved company actually files the form you asked for),
then downloads the latest filing and the one before it — or the two
filing years you chose — plus the company's XBRL data. Downloads are
cached, and requests are paced well under the SEC's fair-access limit
of 10 requests/second.</li>
<li><strong>Split</strong> — each filing is split into its ITEM sections
(Risk Factors, MD&amp;A, Financial Statements&hellip;). Detection is
heuristic, so every report states its section-detection confidence
instead of guessing silently.</li>
<li><strong>Diff the words</strong> — a deterministic, sentence-level
diff classifies changes as deletions, modifications and additions,
ranked by size, <strong>deletions first</strong>. Same input, same
output, every time — no AI is involved in computing the diff.</li>
<li><strong>Diff the numbers</strong> — XBRL values are matched per
filing by accession number across 100 tracked concepts, so each column
is exactly what that filing reported. Concepts reported by only one
filing (usually retags) are listed separately and never silently
merged. The numbers table can be downloaded as CSV or Excel from the
report page. Forms with no XBRL data (proxy statements) simply have
no numbers table — nothing is invented to fill one.</li>
<li><strong>Verify</strong> — every report links to both source filings
on EDGAR, so any change can be checked against the original document in
one click. Reports can also be searched in place (only the changes
mentioning a word or phrase), shared via a link that reproduces the
exact same report, and downloaded as a summary PDF or a share-card
PNG image.</li>
</ol>

{pricing_section()}
{_footer("<strong>Not investment advice.</strong> secdiff shows "
         "what changed in a public document; it makes no recommendations "
         "and has no buy/sell outputs, price data or predictions. "
         "Data: SEC EDGAR (public filings). The secdiff tool itself is "
         "open-source (MIT); this hosted website is free for 4 companies "
         "a day, with paid plans above that — and <code>secdiff</code> "
         "is a working title.")}
<script>{_HOME_JS}</script>
</body></html>
"""


def pricing_section() -> str:
    """The pricing plans + waitlist. Shared by the home page and the
    paywall page. Payment buttons are honest: payments are not live yet,
    so both paid plans point at the waitlist, never a fake checkout."""
    return """
<h2 id="pricing">Pricing</h2>
<div class="plans">
  <div class="plan">
    <h3>Free</h3>
    <div class="price">$0</div>
    <ul>
      <li><strong>4 companies per day</strong></li>
      <li>Full live reports — deletions first, words and XBRL numbers</li>
      <li>Re-viewing a company the same day doesn't use another slot</li>
    </ul>
    <p class="note">No account needed. Free companies reset every day.</p>
  </div>
  <div class="plan">
    <h3>Weekly</h3>
    <div class="price">$9 <span>/ week</span></div>
    <ul>
      <li><strong>Unlimited companies</strong> while active</li>
      <li>All features — 10-K, 10-Q, 8-K and proxy (DEF 14A)</li>
      <li>For a single earnings week deep-dive</li>
    </ul>
    <a class="btn" href="#waitlist">Payments opening soon — join the waitlist</a>
  </div>
  <div class="plan hot">
    <h3>Monthly</h3>
    <div class="price">$27 <span>/ month</span></div>
    <ul>
      <li><strong>Unlimited companies</strong> while active</li>
      <li>All features — 10-K, 10-Q, 8-K and proxy (DEF 14A)</li>
      <li>Best value for following earnings season</li>
    </ul>
    <a class="btn" href="#waitlist">Payments opening soon — join the waitlist</a>
  </div>
</div>

<div class="card" id="waitlist">
  <h3 style="margin-top:0">Join the paid-plan waitlist</h3>
  <p class="note">Payments are not open yet. Leave your name and email
  and we'll email you when Weekly and Monthly plans go live.</p>
  <form method="post" action="/waitlist">
    <label for="wl-name">Name</label>
    <input type="text" id="wl-name" name="name" required maxlength="80">
    <label for="wl-email">Email</label>
    <input type="text" id="wl-email" name="email" required maxlength="120"
           placeholder="you@example.com">
    <button type="submit">Join the waitlist</button>
  </form>
</div>
"""


def paywall_page(user: dict | None = None) -> str:
    return f"""{_page_open("secdiff — daily free limit reached")}
{_header(user)}
<h1>You've used today's 4 free companies</h1>
<p class="tagline">The free plan covers <strong>4 different companies per
day</strong>, with full reports. You've reached today's limit — companies
you already viewed today still open, and the free four
<strong>reset tomorrow</strong>.</p>
<p>Want to keep going today? The paid plans give you
<strong>unlimited companies</strong>:</p>
{pricing_section()}
<p><a class="btn secondary" href="/">&larr; Back to the search</a></p>
{_footer("<strong>Not investment advice.</strong> "
         "Data: SEC EDGAR (public filings).")}
</body></html>
"""


def waitlist_thanks_page(user: dict | None = None) -> str:
    return f"""{_page_open("secdiff — you're on the waitlist")}
{_header(user)}
<h1>You're on the waitlist ✅</h1>
<p class="tagline">Thanks — we'll email you when the Weekly ($9) and
Monthly ($27) plans open for payment.</p>
<p><a class="btn" href="/">&larr; Back to the search</a></p>
{_footer("<strong>Not investment advice.</strong> "
         "Data: SEC EDGAR (public filings).")}
</body></html>
"""


def error_page(title: str, message: str, user: dict | None = None) -> str:
    return f"""{_page_open(f"secdiff — {title}")}
{_header(user)}
<h1>{html.escape(title)}</h1>
<div class="err"><p>{html.escape(message)}</p></div>
<p class="note">No report was generated and nothing was filled in or
guessed — when EDGAR doesn't give us the filings, we show you this page
instead of a made-up result.</p>
<p><a class="btn" href="/">&larr; Back to the search</a></p>
{_footer("<strong>Not investment advice.</strong> "
         "Data: SEC EDGAR (public filings).")}
</body></html>
"""


# ---------------------------------------------------------------------------
# Account pages

def _shell(title: str, user: dict | None, inner: str) -> str:
    return f"""{_page_open(f"secdiff — {title}")}
{_header(user)}
{inner}
{_footer("<strong>Not investment advice.</strong> "
         "Data: SEC EDGAR (public filings).")}
</body></html>
"""


def _form_error(message: str) -> str:
    return (f"<div class='err'><p>{html.escape(message, quote=False)}</p></div>"
            if message else "")


def signup_page(error: str = "", name: str = "", email: str = "",
                username: str = "") -> str:
    inner = f"""
<h1>Create your account</h1>
<p class="tagline">One account works on every device — your daily free
companies follow you. Paid plans (Weekly $9, Monthly $27) open soon.</p>
{_form_error(error)}
<div class="card">
<form method="post" action="/signup">
  <label for="su-name">Your name <span class="note">(optional)</span></label>
  <input type="text" id="su-name" name="name" maxlength="80"
         value="{html.escape(name, quote=True)}" autocomplete="name">
  <label for="su-email">Email</label>
  <input type="email" id="su-email" name="email" required maxlength="120"
         value="{html.escape(email, quote=True)}" autocomplete="email"
         placeholder="you@example.com">
  <label for="su-username">Username</label>
  <input type="text" id="su-username" name="username" required maxlength="30"
         value="{html.escape(username, quote=True)}" autocomplete="username"
         placeholder="3–30 characters: letters, digits, . _ -">
  <label for="su-password">Password</label>
  <input type="password" id="su-password" name="password" required
         minlength="8" autocomplete="new-password"
         placeholder="At least 8 characters">
  <button type="submit">Create account</button>
</form>
</div>
<p>Already have an account? <a href="/login">Log in</a> ·
<a href="/forgot-username">Forgot your username?</a></p>
"""
    return _shell("Create account", None, inner)


def login_page(error: str = "", identifier: str = "") -> str:
    inner = f"""
<h1>Log in</h1>
{_form_error(error)}
<div class="card">
<form method="post" action="/login">
  <label for="li-id">Username or email</label>
  <input type="text" id="li-id" name="identifier" required maxlength="120"
         value="{html.escape(identifier, quote=True)}"
         autocomplete="username">
  <label for="li-password">Password</label>
  <input type="password" id="li-password" name="password" required
         autocomplete="current-password">
  <button type="submit">Log in</button>
</form>
</div>
<p><a href="/forgot-password">Forgot your password?</a> ·
<a href="/forgot-username">Forgot your username?</a></p>
<p>New here? <a href="/signup">Create an account</a> — free.</p>
"""
    return _shell("Log in", None, inner)


_PLAN_LABELS = {"free": "Free — $0",
                "weekly": "Weekly — $9 / week",
                "monthly": "Monthly — $27 / month"}


def account_page(user: dict, used_today: int, message: str = "",
                 error: str = "") -> str:
    plan = _PLAN_LABELS.get(user.get("plan") or "free", "Free — $0")
    ok = (f"<div class='ok'><p>{html.escape(message)}</p></div>"
          if message else "")
    inner = f"""
<h1>My account</h1>
{ok}{_form_error(error)}
<div class="tablewrap"><table class="kv">
  <tr><td>Username</td><td>{html.escape(user['username'])}</td></tr>
  <tr><td>Email</td><td>{html.escape(user['email'])}</td></tr>
  <tr><td>Plan</td><td>{html.escape(plan)}</td></tr>
  <tr><td>Today</td><td>{used_today} of {FREE_DAILY_COMPANIES} free companies used</td></tr>
</table></div>
<p class="note">Paid plans (Weekly $9, Monthly $27 — unlimited companies)
are <strong>opening soon</strong>; until payments go live, every account
gets the same 4 free companies a day as guests.</p>

<h2>Change password</h2>
<div class="card">
<form method="post" action="/change-password">
  <label for="cp-current">Current password</label>
  <input type="password" id="cp-current" name="current_password" required
         autocomplete="current-password">
  <label for="cp-new">New password</label>
  <input type="password" id="cp-new" name="new_password" required
         minlength="8" autocomplete="new-password"
         placeholder="At least 8 characters">
  <button type="submit">Change password</button>
</form>
</div>
<form method="post" action="/logout">
  <button type="submit" class="btn secondary">Log out</button>
</form>
"""
    return _shell("My account", user, inner)


def forgot_password_page(error: str = "", notice: str = "") -> str:
    note = (f"<div class='ok'><p>{html.escape(notice)}</p></div>"
            if notice else "")
    inner = f"""
<h1>Forgot your password?</h1>
<p class="tagline">Enter your email address or username and we'll email
you a 6-digit code to set a new password.</p>
{note}{_form_error(error)}
<div class="card">
<form method="post" action="/forgot-password">
  <input type="hidden" name="step" value="request">
  <label for="fp-id">Email or username</label>
  <input type="text" id="fp-id" name="identifier" required maxlength="120"
         autocomplete="username">
  <button type="submit">Email me a code</button>
</form>
</div>
<p><a href="/login">&larr; Back to log in</a></p>
"""
    return _shell("Forgot password", None, inner)


def forgot_password_code_page(identifier: str, notice: str,
                                error: str = "") -> str:
    note = (f"<div class='ok'><p>{html.escape(notice)}</p></div>"
            if notice else "")
    inner = f"""
<h1>Enter your reset code</h1>
{note}{_form_error(error)}
<div class="card">
<form method="post" action="/forgot-password">
  <input type="hidden" name="step" value="reset">
  <input type="hidden" name="identifier"
         value="{html.escape(identifier, quote=True)}">
  <label for="fp-code">6-digit code from your email</label>
  <input type="text" id="fp-code" name="code" required maxlength="6"
         inputmode="numeric" autocomplete="one-time-code"
         placeholder="123456">
  <label for="fp-new">New password</label>
  <input type="password" id="fp-new" name="new_password" required
         minlength="8" autocomplete="new-password"
         placeholder="At least 8 characters">
  <button type="submit">Set new password</button>
</form>
</div>
<p class="note">Codes expire after 10 minutes and can be used once.
No code? <a href="/forgot-password">Request a new one</a>.</p>
"""
    return _shell("Enter reset code", None, inner)


def password_reset_done_page() -> str:
    inner = """
<h1>Password changed ✅</h1>
<div class="ok"><p>Your password has been reset and you have been logged
out everywhere. Log in with your new password to continue.</p></div>
<p><a class="btn" href="/login">Log in</a></p>
"""
    return _shell("Password changed", None, inner)


def forgot_username_page(error: str = "", notice: str = "") -> str:
    note = (f"<div class='ok'><p>{html.escape(notice)}</p></div>"
            if notice else "")
    inner = f"""
<h1>Forgot your username?</h1>
<p class="tagline">Enter the email address on your account and we'll
email you a 6-digit code; enter it here and we'll show your username.</p>
{note}{_form_error(error)}
<div class="card">
<form method="post" action="/forgot-username">
  <input type="hidden" name="step" value="request">
  <label for="fu-email">Email</label>
  <input type="email" id="fu-email" name="email" required maxlength="120"
         autocomplete="email" placeholder="you@example.com">
  <button type="submit">Email me a code</button>
</form>
</div>
<p><a href="/login">&larr; Back to log in</a></p>
"""
    return _shell("Forgot username", None, inner)


def forgot_username_code_page(email: str, notice: str,
                                error: str = "") -> str:
    note = (f"<div class='ok'><p>{html.escape(notice)}</p></div>"
            if notice else "")
    inner = f"""
<h1>Enter your code</h1>
{note}{_form_error(error)}
<div class="card">
<form method="post" action="/forgot-username">
  <input type="hidden" name="step" value="verify">
  <input type="hidden" name="email"
         value="{html.escape(email, quote=True)}">
  <label for="fu-code">6-digit code from your email</label>
  <input type="text" id="fu-code" name="code" required maxlength="6"
         inputmode="numeric" autocomplete="one-time-code"
         placeholder="123456">
  <button type="submit">Show my username</button>
</form>
</div>
<p class="note">Codes expire after 10 minutes and can be used once.
No code? <a href="/forgot-username">Request a new one</a>.</p>
"""
    return _shell("Enter code", None, inner)


def username_revealed_page(username: str) -> str:
    inner = f"""
<h1>Your username</h1>
<div class="ok"><p>Your secdiff username is
<strong>{html.escape(username)}</strong>.</p></div>
<p><a class="btn" href="/login">Log in</a></p>
"""
    return _shell("Your username", None, inner)


# ---------------------------------------------------------------------------
# Phase 4 pages: company pages, trending, sectors, watchlist, API docs
# + JSON API. Every number on these pages comes from the same EDGAR
# pipeline or from the site's own stores (stats / majors cache /
# watchlist) — nothing is invented, and every page labels its source.
# There is deliberately no demo or examples page anywhere on the site
# (owner's standing rule); /examples and /demo are plain 404s.

def company_page(user: dict | None, data: dict) -> str:
    e = html.escape
    form_rows = []
    for fr in data["forms"]:
        if fr["can_diff"]:
            action = (f"<a href='/report?ticker="
                      f"{e(data['ticker'], quote=True)}&amp;form="
                      f"{e(fr['form'], quote=True)}'>Run the "
                      f"{e(fr['form'])} report</a>")
        else:
            action = ("<span class='note'>Only one filing on EDGAR — "
                      "a diff needs two.</span>")
        years = ", ".join(str(y) for y in fr["years"])
        if len(fr["years"]) > 15:
            years += ", …"
        form_rows.append(
            f"<tr><td>{e(fr['form'])}</td><td>{fr['count']}</td>"
            f"<td>{e(years) if years else '—'}</td><td>{action}</td></tr>")
    extra = ""
    if data["report_count"]:
        extra += (f"<tr><td>Reports run on this site</td>"
                  f"<td>{data['report_count']}</td></tr>")
    major = data.get("major")
    if major:
        extra += (f"<tr><td>Latest 10-K (this site, checked "
                  f"{e(major['checked'])})</td><td>{major['deletions']} "
                  f"deletions · change-intensity score {major['score']}"
                  f"/100</td></tr>")
    inner = f"""
<h1>{e(data['company'])}</h1>
<p class="tagline">Ticker <strong>{e(data['ticker'])}</strong> ·
SEC EDGAR CIK {data['cik']}</p>
<div class="tablewrap"><table class="kv">
  <tr><td>Company</td><td>{e(data['company'])}</td></tr>
  <tr><td>CIK</td><td>{data['cik']}</td></tr>
  {extra}
</table></div>
<h2>Filings on EDGAR</h2>
<div class="tablewrap"><table class="data">
<tr><th>Form</th><th>Filings</th><th>Filing years</th><th></th></tr>
{''.join(form_rows)}
</table></div>
<p class="note">Filing counts and years are this company's own SEC
EDGAR submission history. Running a report counts toward your daily
free companies, like any report on this site.</p>
"""
    return _shell(data["company"], user, inner)


def _company_route(start_response, method, user, raw_segment: str):
    if method not in ("GET", "HEAD"):
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the browser.", user)
    segment = urllib.parse.unquote(raw_segment).strip()
    ticker_q = " ".join(segment.upper().split())
    if not ticker_q or "/" in segment or not _QUERY_RE.match(ticker_q):
        return _error(start_response, "404 Not Found", "Page not found",
                      "There is no company page at that address.", user)
    client = _edgar_client()
    try:
        with redirect_stderr(io.StringIO()):
            cik, company = client.resolve_ticker(ticker_q, form="10-K",
                                                 min_count=1)
    except SystemExit as exc:
        msg = str(exc.code) if exc.code else "Company not found."
        return _error(start_response, "404 Not Found",
                      "Company not found", msg, user)
    forms = []
    for form in _FORMS:
        filings = client.all_filings(cik, form)
        years = sorted({f.filing_year for f in filings
                        if f.filing_year is not None}, reverse=True)
        forms.append({"form": form, "count": len(filings),
                      "years": years, "can_diff": len(filings) >= 2})
    label = client.name_matched_ticker or ticker_q
    try:
        report_count = get_stats_store().report_count(label)
    except Exception:
        report_count = 0
    major = get_majors_store().get(label)
    data = {"ticker": label, "company": company, "cik": cik,
            "forms": forms, "report_count": report_count,
            "major": major}
    return _respond(start_response, "200 OK", company_page(user, data))


def trending_page(user: dict | None, top: list) -> str:
    e = html.escape
    if top:
        rows = "".join(
            "<tr>"
            f"<td><a href='/company/{e(t, quote=True)}'>{e(t)}</a></td>"
            f"<td>{n}</td>"
            f"<td><a href='/report?ticker={e(t, quote=True)}"
            f"&amp;form=10-K'>Latest 10-K report</a></td>"
            "</tr>" for t, n in top)
        body = ("<div class='tablewrap'><table class='data'>"
                "<tr><th>Company</th>"
                "<th>Reports run</th><th></th></tr>"
                f"{rows}</table></div>")
    else:
        body = ("<p>No reports have been run on this site yet — "
                "this list fills in as visitors generate reports.</p>")
    inner = f"""
<h1>Trending on secdiff</h1>
<p class="tagline">The companies with the most reports generated
<strong>on this site</strong> — all time. These are this site's own
usage counts (they include testing), not market data and not a
ranking of the companies themselves.</p>
{body}
"""
    return _shell("Trending on secdiff", user, inner)


def _trending_route(start_response, method, user):
    if method not in ("GET", "HEAD"):
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the browser.", user)
    return _respond(start_response, "200 OK",
                    trending_page(user, get_stats_store()
                                  .top_companies(25)))


def sectors_page(user: dict | None, groups: list,
                 pending: list) -> str:
    """groups: [(sector, [entry, ...]), ...] in display order;
    pending: tickers in the universe whose summary is not computed
    yet (named honestly, never given numbers)."""
    e = html.escape
    parts = []
    for sector, entries in groups:
        rows = "".join(
            "<tr>"
            f"<td><a href='/company/{e(en['ticker'], quote=True)}'>"
            f"{e(en['ticker'])}</a> — {e(en['company'])}</td>"
            f"<td><strong>{en['deletions']}</strong></td>"
            f"<td>{en['score']}/100 ({e(en['band'])})</td>"
            f"<td>{e(en['new_date'])} vs {e(en['old_date'])}</td>"
            f"<td><a href='/report?ticker={e(en['ticker'], quote=True)}"
            f"&amp;form=10-K'>Read the report</a></td>"
            "</tr>" for en in entries)
        parts.append(
            f"<h2>{e(sector)}</h2>"
            "<div class='tablewrap'><table class='data'>"
            "<tr><th>Company</th><th>Deletions "
            "(latest 10-K)</th><th>Change-intensity score</th>"
            "<th>Filings</th><th></th></tr>"
            f"{rows}</table></div>")
    if not parts:
        parts.append("<p>No sector summaries have been computed yet "
                     "— check back in a moment.</p>")
    pending_note = ""
    if pending:
        pending_note = ("<p class='note'>Still computing: "
                        + ", ".join(e(t) for t in pending)
                        + " — refresh in a moment; no numbers are "
                        "shown for a company until its report has "
                        "really been computed.</p>")
    inner = f"""
<h1>Sectors</h1>
<p class="tagline">Well-known companies grouped by sector, ranked by
the deletions in their latest 10-K. This is a <strong>curated list of
well-known companies only — not full-market coverage</strong>: the
universe is the major filers plus mapped companies visitors have run
reports on.</p>
{''.join(parts)}
{pending_note}
"""
    return _shell("Sectors", user, inner)


def _sectors_route(start_response, method, user):
    if method not in ("GET", "HEAD"):
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the browser.", user)
    trending = [t for t, _n in get_stats_store().top_companies(25)]
    universe = list(MAJORS)
    for t in trending:
        if t in SECTOR_MAP and t not in universe:
            universe.append(t)
    entries_map = _maybe_refresh_majors(
        [t for t in universe if t in SECTOR_MAP])
    by_sector: dict[str, list] = {}
    pending = []
    for t in universe:
        sector = sector_of(t)
        if sector is None:
            continue
        entry = entries_map.get(t)
        if not isinstance(entry, dict):
            entry = None
        if entry is None:
            pending.append(t)
        else:
            by_sector.setdefault(sector, []).append(entry)
    order = {name: i for i, name in enumerate(SECTOR_ORDER)}
    groups = [(sector, sort_entries(entries))
              for sector, entries in sorted(
                  by_sector.items(),
                  key=lambda kv: (order.get(kv[0], len(order)), kv[0]))]
    return _respond(start_response, "200 OK",
                    sectors_page(user, groups, pending))


def watchlist_page(user: dict, entries: list, majors: dict,
                   message: str = "", error: str = "") -> str:
    e = html.escape
    ok = (f"<div class='ok'><p>{e(message)}</p></div>" if message else "")
    rows = []
    for en in entries:
        major = majors.get(en["ticker"].upper()) \
            if isinstance(majors, dict) else None
        if major:
            known = (f"{major['deletions']} deletions · score "
                     f"{major['score']}/100 (checked "
                     f"{e(major['checked'])})")
        else:
            known = ("<span class='note'>not computed yet</span>")
        rows.append(
            "<tr>"
            f"<td><a href='/company/"
            f"{e(en['ticker'], quote=True)}'>{e(en['ticker'])}</a></td>"
            f"<td>{e(en['company'])}</td>"
            f"<td>{known}</td>"
            f"<td><a href='/report?ticker="
            f"{e(en['ticker'], quote=True)}&amp;form=10-K'>"
            f"Latest 10-K report</a></td>"
            "<td><form method='post' action='/watchlist/remove' "
            "style='display:inline'>"
            f"<input type='hidden' name='cik' value='{en['cik']}'>"
            "<button type='submit' class='linkbtn'>Remove</button>"
            "</form></td>"
            "</tr>")
    if rows:
        table = ("<div class='tablewrap'><table class='data'>"
                 "<tr><th>Ticker</th><th>Company"
                 "</th><th>Last-known latest-10-K</th><th></th>"
                 "<th></th></tr>" + "".join(rows) + "</table></div>")
    else:
        table = ("<p>Your watchlist is empty — add a company below "
                 "and it will stay here, on your account, on every "
                 "device.</p>")
    inner = f"""
<h1>Your watchlist</h1>
{ok}{_form_error(error)}
{table}
<div class="card">
  <h3 style="margin-top:0">Add a company</h3>
  <form method="post" action="/watchlist/add">
    <label for="wl-ticker">Ticker or company name</label>
    <input type="text" id="wl-ticker" name="ticker" required
           maxlength="40" placeholder="AAPL or Microsoft"
           autocomplete="off">
    <button type="submit">Add to watchlist</button>
  </form>
</div>
<p class="note"><strong>Email alerts are not switched on yet.</strong>
When email delivery is enabled for this site, your watchlist companies
will be the ones you are alerted about when a new filing lands.
Nothing is emailed today — the list above is saved on your account
only.</p>
"""
    return _shell("Your watchlist", user, inner)


def _watchlist_view(environ, start_response, method, user):
    if user is None:
        return _redirect(start_response, "/login")
    if method not in ("GET", "HEAD"):
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the form.", user)
    qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    message = (qs.get("msg") or [""])[0]
    error = (qs.get("err") or [""])[0]
    entries = get_watchlist_store().list(user["id"])
    return _respond(start_response, "200 OK",
                    watchlist_page(user, entries,
                                   get_majors_store().load(),
                                   message, error))


def _watchlist_redirect(start_response, msg: str = "", err: str = ""):
    q = {}
    if msg:
        q["msg"] = msg
    if err:
        q["err"] = err
    suffix = f"?{urllib.parse.urlencode(q)}" if q else ""
    return _redirect(start_response, f"/watchlist{suffix}")


def _watchlist_add(environ, start_response, method, user):
    if user is None:
        return _redirect(start_response, "/login")
    if method != "POST":
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the form.", user)
    f = _form_fields(environ)
    try:
        params = parse_report_form(
            urllib.parse.urlencode({"ticker": f.get("ticker", "")}))
    except ValueError as exc:
        return _watchlist_redirect(start_response, err=str(exc))
    try:
        cik, company = _resolve_company(params)
    except SystemExit as exc:
        msg = str(exc.code) if exc.code else "Company not found."
        return _watchlist_redirect(start_response, err=msg)
    added = get_watchlist_store().add(user["id"], cik, params.ticker,
                                      company)
    if added:
        return _watchlist_redirect(
            start_response,
            msg=f"{params.ticker} ({company}) added to your watchlist.")
    return _watchlist_redirect(
        start_response,
        msg=f"{company} is already on your watchlist.")


def _watchlist_remove(environ, start_response, method, user):
    if user is None:
        return _redirect(start_response, "/login")
    if method != "POST":
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the form.", user)
    f = _form_fields(environ)
    try:
        cik = int(f.get("cik", ""))
    except ValueError:
        return _watchlist_redirect(start_response,
                                   err="That removal didn't parse.")
    get_watchlist_store().remove(user["id"], cik)
    return _watchlist_redirect(start_response,
                               msg="Removed from your watchlist.")


def api_page(user: dict | None = None) -> str:
    inner = """
<h1>API</h1>
<p class="tagline">A small read-only JSON API over the same live
pipeline the website uses. Every response is computed from real SEC
EDGAR filings at request time — there is no cached or sample data
behind it.</p>

<h2>Endpoint</h2>
<p><code>GET /api/report?ticker={ticker}&amp;form={form}</code></p>
<div class="tablewrap"><table class="data">
<tr><th>Parameter</th><th>Required</th><th>Notes</th></tr>
<tr><td><code>ticker</code></td><td>yes</td><td>A ticker symbol
(including class-share spellings with a dot) or the company's
name — resolved against EDGAR exactly like the website.</td></tr>
<tr><td><code>form</code></td><td>no</td><td>10-K (default), 10-Q,
8-K or DEF 14A.</td></tr>
<tr><td><code>vs_year</code> / <code>base_year</code></td><td>no</td>
<td>Compare those filing years instead of the latest two.</td></tr>
</table></div>

<h2>Response</h2>
<p>A JSON object with: <code>company</code>, <code>ticker</code>,
<code>cik</code>, <code>form</code>, <code>filings</code>
(<code>newer</code> / <code>older</code>, each with accession, date
and EDGAR URLs), <code>counts</code> (deletions, modifications,
additions, xbrl_changed), <code>score</code> (the change-intensity
score: total, band and the published breakdown), <code>top_deletions</code>
(the report's biggest deleted sentences, biggest first) and
<code>report_url</code> (the same report as a web page).</p>

<h2>Limits — read this before building on it</h2>
<ul>
<li><strong>No API key, no signup</strong> — for now.</li>
<li>The API shares the website's free limit: <strong>4 distinct
companies per day</strong> per visitor (per account when logged in),
counted by company, across the API and the website together.
Re-running a company you already viewed today is free.</li>
<li>Beyond the limit the API answers <code>402</code> with a JSON
error. Paid plans (Weekly $9, Monthly $27) will raise this when
payments open.</li>
<li>Errors are JSON too: <code>400</code> for invalid input,
<code>404</code> when the company cannot be found on EDGAR,
<code>502</code> when EDGAR itself cannot be reached.</li>
<li>Not investment advice; data is SEC EDGAR public filings.</li>
</ul>
"""
    return _shell("API", user, inner)


def _api_docs_route(start_response, method, user):
    if method not in ("GET", "HEAD"):
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the browser.", user)
    return _respond(start_response, "200 OK", api_page(user))


def _api_report_route(start_response, environ, method, user,
                      visitor_id, cookie):
    """GET /api/report — the JSON report. Same accounting as /report:
    parse (400 JSON) → resolve (404 JSON, no slot) → daily limit by
    CIK (402 JSON) → compute through the shared pipeline."""
    def jerr(status: str, message: str):
        return _respond(start_response, status,
                        json.dumps({"error": message}),
                        "application/json; charset=utf-8",
                        extra_headers=cookie)

    if method not in ("GET", "HEAD"):
        return jerr("405 Method Not Allowed",
                    "Use GET for the API.")
    try:
        params = parse_report_form(environ.get("QUERY_STRING", ""))
    except ValueError as exc:
        return jerr("400 Bad Request", str(exc))
    try:
        cik, _company = _resolve_company(params)
    except SystemExit as exc:
        msg = str(exc.code) if exc.code else "Company not found."
        return jerr("404 Not Found", msg)
    if not get_usage_store().check_and_add(
            _usage_key(user, visitor_id), cik):
        return jerr("402 Payment Required",
                    "Daily free limit reached: 4 distinct companies "
                    "per day across the website and the API. The "
                    "limit resets tomorrow (UTC).")
    try:
        info, diffs, xbrl = _compute_data(params)
    except SystemExit as exc:
        msg = str(exc.code) if exc.code else "No report could be built."
        return jerr("400 Bad Request", msg)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return jerr("502 Bad Gateway",
                    f"SEC EDGAR could not be reached ({reason}).")
    _record("report", info.ticker)
    d, m, a = _counts(diffs)
    sc = compute_score(diffs, xbrl)
    deletions = sorted(
        (h for s in diffs for h in s.hunks if h.kind == "deletion"),
        key=lambda h: -h.size)
    payload = {
        "company": info.company,
        "ticker": info.ticker,
        "cik": info.cik,
        "form": info.form,
        "filings": {
            "newer": {"accession": info.new_accession,
                      "date": info.new_date,
                      "url": info.new_url,
                      "index_url": info.new_index_url},
            "older": {"accession": info.old_accession,
                      "date": info.old_date,
                      "url": info.old_url,
                      "index_url": info.old_index_url},
        },
        "counts": {"deletions": d, "modifications": m,
                   "additions": a, "xbrl_changed": len(xbrl.rows)},
        "score": {"total": sc.total, "band": sc.band,
                  "breakdown": sc.breakdown()},
        "top_deletions": [h.old for h in deletions[:10]],
        "report_url": f"/report?{_canonical_query(info, params)}",
    }
    return _respond(start_response, "200 OK", json.dumps(payload),
                    "application/json; charset=utf-8",
                    extra_headers=cookie)


# ---------------------------------------------------------------------------
# WSGI application (also the core the stdlib server dispatches to)

def _respond(start_response, status: str, body,
             content_type: str = "text/html; charset=utf-8",
             extra_headers=()):
    data = body.encode("utf-8") if isinstance(body, str) else body
    start_response(status, [("Content-Type", content_type),
                            ("Content-Length", str(len(data))),
                            *extra_headers])
    return [data]


def _error(start_response, status: str, title: str, message: str,
           user: dict | None = None):
    return _respond(start_response, status, error_page(title, message, user))


def _redirect(start_response, location: str, extra_headers=()):
    return _respond(start_response, "303 See Other", "",
                    extra_headers=[("Location", location), *extra_headers])


def _form_fields(environ) -> dict:
    qs = urllib.parse.parse_qs(_read_body(environ), keep_blank_values=True)
    return {k: (v[0].strip() if v else "") for k, v in qs.items()}


def _report_key(summary: str, params: ReportParams) -> str:
    """Stats key for a finished report: the real ticker out of the
    pipeline's one-line summary ("AAPL 10-K: …"), falling back to what
    the visitor typed."""
    tokens = (summary or "").split()
    return tokens[0] if tokens else params.ticker


def _usage_key(user: dict | None, visitor_id: str) -> str:
    """Daily-limit identity: the account (by username) when logged in —
    so the count follows the user across devices — else the guest
    visitor cookie."""
    return f"user:{user['username'].lower()}" if user else visitor_id


def _start_recovery(store: AccountStore, user: dict | None,
                    purpose: str) -> tuple[str, bool]:
    """Issue + email a recovery code. Returns (notice_for_visitor,
    code_page_expected). Anti-enumeration: an unknown identifier gets
    the same generic notice as a delivered code. When the account
    exists but mail is unconfigured/failed, the notice honestly says
    email delivery is not switched on yet; the code is never shown."""
    generic = ("If an account matches, a 6-digit code is on its way to "
               "that email address. It expires in 10 minutes.")
    if user is None:
        return generic, True
    code = store.issue_code(user["id"], purpose)
    if _accounts.deliver_code(user, code, purpose):
        return generic, True
    return ("Email delivery is not switched on for this site yet, so "
            "your code could not be sent. Please try again later — the "
            "account itself is fine, and no code is shown here for "
            "security reasons."), True


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _join_waitlist(raw: str) -> None:
    """Append one waitlist signup (name + email) to waitlist.jsonl in the
    cache dir. Raises ValueError with a friendly message on bad input."""
    qs = urllib.parse.parse_qs(raw, keep_blank_values=True)
    name = (qs.get("name") or [""])[0].strip()
    email = (qs.get("email") or [""])[0].strip()
    if not name:
        raise ValueError("Please enter your name for the waitlist.")
    if not _EMAIL_RE.match(email):
        raise ValueError("That email address doesn't look right — "
                         "please check it and try again.")
    cache = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now(timezone.utc).isoformat(),
              "name": name, "email": email}
    with open(cache / "waitlist.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _read_body(environ) -> str:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    return environ["wsgi.input"].read(length).decode("utf-8", "replace")


def _theme_route(environ, start_response, method, user, cookie):
    """POST /theme — store the visitor's light/dark choice in the
    theme cookie and send them back where they were. Presentation
    only: no account, limit or content state is touched. The `next`
    field is honoured only for local paths (never `//host` or
    absolute URLs), so the toggle can't become an open redirect."""
    if method != "POST":
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the theme buttons.",
                      user)
    fields = _form_fields(environ)
    value = clean_theme(fields.get("theme", ""))
    if value is None:
        return _error(start_response, "400 Bad Request",
                      "Unknown theme",
                      "The theme must be “light” or “dark”.", user)
    target = fields.get("next", "")
    if not target.startswith("/") or target.startswith("//"):
        target = "/"
    return _redirect(start_response, target,
                     [_theme_cookie_header(value), *cookie])


def application(environ, start_response):
    """WSGI app: / (search form + pricing), /report (POST form or GET
    query, with ?q= for search-in-report), the report downloads
    (/report.csv, /report.xls, /report.pdf) and the /card share image —
    all behind the same resolve-then-limit accounting as /report —
    /waitlist (POST), the account pages (/signup, /login, /logout,
    /account, /change-password, /forgot-password, /forgot-username),
    the owner-only /stats (404 for everyone but the STATS_USERNAME
    account), /healthz, and the Phase 5 presentation routes:
    /theme (POST, sets the light/dark cookie), /manifest.webmanifest
    and the generated icons (/icon-192.png, /icon-512.png,
    /apple-touch-icon.png). All other paths 404; all failures are
    honest error pages."""
    method = (environ.get("REQUEST_METHOD") or "GET").upper()
    path = environ.get("PATH_INFO") or "/"
    visitor_id, visitor_is_new = _visitor_id(environ)
    cookie = [_cookie_header(visitor_id)] if visitor_is_new else []
    # The request's theme choice, read by every page renderer via
    # _request_theme(). Set unconditionally so a pooled thread never
    # leaks the previous request's theme. `return_to` (GET requests
    # only) is where the theme toggle sends the visitor back to.
    _THEME_CTX.value = _theme_pref(environ)
    if method in ("GET", "HEAD"):
        qs = environ.get("QUERY_STRING", "")
        _THEME_CTX.return_to = path + (f"?{qs}" if qs else "")
    else:
        _THEME_CTX.return_to = ""
    try:
        user = _current_user(environ)
    except Exception:                       # accounts must never break the site
        user = None
    try:
        if path in ("/", "/index.html"):
            if method not in ("GET", "HEAD"):
                return _error(start_response, "405 Method Not Allowed",
                              "Method not allowed", "Use the search form.",
                              user)
            notice = ""
            qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
            if (qs.get("welcome") or [""])[0] == "1" and user:
                notice = (f"Welcome, {user['username']} — your account is "
                          "ready. Your daily free companies now follow "
                          "this account on any device.")
            _record("pageview")
            _record("visit", visitor_id=visitor_id)
            # Biggest deletes: render the cached major summaries and
            # let a background thread refresh anything stale — the
            # home page never waits on EDGAR.
            _maybe_refresh_majors()
            majors_store = get_majors_store()
            cached = [majors_store.get(t) for t in MAJORS]
            biggest = sort_entries([en for en in cached if en])
            biggest_pending = any(
                not MajorsStore.is_fresh(en) for en in cached) \
                or len(biggest) < len(MAJORS)
            biggest_checked = biggest[0]["checked"] if biggest else ""
            return _respond(start_response, "200 OK",
                            home_page(user, notice, biggest,
                                      biggest_pending, biggest_checked),
                            extra_headers=cookie)
        if path == "/healthz":
            return _respond(start_response, "200 OK", "ok\n",
                            "text/plain; charset=utf-8")
        if path == "/manifest.webmanifest":
            if method not in ("GET", "HEAD"):
                return _error(start_response, "405 Method Not Allowed",
                              "Method not allowed", "Use the browser.",
                              user)
            return _respond(start_response, "200 OK", manifest_bytes(),
                            "application/manifest+json",
                            extra_headers=[("Cache-Control",
                                            "public, max-age=86400")])
        if path in ("/icon-192.png", "/icon-512.png",
                    "/apple-touch-icon.png"):
            if method not in ("GET", "HEAD"):
                return _error(start_response, "405 Method Not Allowed",
                              "Method not allowed", "Use the browser.",
                              user)
            size = 512 if path == "/icon-512.png" else 192
            return _respond(start_response, "200 OK",
                            render_icon_png(size), "image/png",
                            extra_headers=[("Cache-Control",
                                            "public, max-age=604800")])
        if path == "/theme":
            return _theme_route(environ, start_response, method, user,
                                cookie)
        if path == "/waitlist":
            if method != "POST":
                return _redirect(start_response, "/#waitlist")
            _join_waitlist(_read_body(environ))
            return _respond(start_response, "200 OK",
                            waitlist_thanks_page(user),
                            extra_headers=cookie)
        if path == "/signup":
            return _signup(environ, start_response, method, user, cookie)
        if path == "/login":
            return _login(environ, start_response, method, user, cookie)
        if path == "/logout":
            if method == "POST":
                token = _session_token(environ)
                if token:
                    get_account_store().delete_session(token)
            return _redirect(start_response, "/",
                             [_clear_session_cookie_header()])
        if path == "/account":
            if user is None:
                return _redirect(start_response, "/login")
            if method not in ("GET", "HEAD"):
                return _error(start_response, "405 Method Not Allowed",
                              "Method not allowed", "Use the form.", user)
            used = len(get_usage_store().companies_today(
                _usage_key(user, visitor_id)))
            return _respond(start_response, "200 OK",
                            account_page(user, used))
        if path == "/change-password":
            return _change_password(environ, start_response, method, user,
                                    visitor_id)
        if path == "/forgot-password":
            return _forgot_password(environ, start_response, method)
        if path == "/forgot-username":
            return _forgot_username(environ, start_response, method)
        if path == "/stats":
            return _stats(start_response, method, user)
        if path == "/trending":
            return _trending_route(start_response, method, user)
        if path == "/sectors":
            return _sectors_route(start_response, method, user)
        if path == "/watchlist":
            return _watchlist_view(environ, start_response, method, user)
        if path == "/watchlist/add":
            return _watchlist_add(environ, start_response, method, user)
        if path == "/watchlist/remove":
            return _watchlist_remove(environ, start_response, method,
                                     user)
        if path == "/api":
            return _api_docs_route(start_response, method, user)
        if path == "/api/report":
            return _api_report_route(start_response, environ, method,
                                     user, visitor_id, cookie)
        if path.startswith("/company/"):
            return _company_route(start_response, method, user,
                                  path[len("/company/"):])
        if path in ("/report.csv", "/report.xls"):
            return _download(start_response, environ, method, user,
                             visitor_id, cookie,
                             "csv" if path.endswith(".csv") else "xls")
        if path == "/report.pdf":
            return _pdf_download(start_response, environ, method, user,
                                 visitor_id, cookie)
        if path == "/card":
            return _card(start_response, environ, method, user,
                         visitor_id, cookie)
        if path == "/report":
            if method == "POST":
                raw = _read_body(environ)
            elif method in ("GET", "HEAD"):
                raw = environ.get("QUERY_STRING", "")
            else:
                return _error(start_response, "405 Method Not Allowed",
                              "Method not allowed", "Use the search form.",
                              user)
            params = parse_report_form(raw)
            # Resolve FIRST: an unknown ticker errors out here (the usual
            # honest error page) and never consumes a free slot.
            cik, _company = _resolve_company(params)
            if not get_usage_store().check_and_add(
                    _usage_key(user, visitor_id), cik):
                return _respond(start_response, "402 Payment Required",
                                paywall_page(user), extra_headers=cookie)
            summary, page = run_report(params, user)
            _record("report", _report_key(summary, params))
            return _respond(start_response, "200 OK", page,
                            extra_headers=cookie)
        return _error(start_response, "404 Not Found", "Page not found",
                      f"There is no page at {path}.", user)
    except ValueError as exc:                       # bad form input
        return _error(start_response, "400 Bad Request",
                      "Check the form", str(exc), user)
    except SystemExit as exc:                       # pipeline's honest errors
        msg = str(exc.code) if exc.code else "The request could not be completed."
        return _error(start_response, "400 Bad Request",
                      "No report could be built", msg, user)
    except urllib.error.URLError as exc:           # EDGAR unreachable
        reason = getattr(exc, "reason", exc)
        return _error(start_response, "502 Bad Gateway",
                      "SEC EDGAR could not be reached",
                      f"EDGAR did not answer ({reason}). Nothing was fetched, "
                      "so there is no report to show — please try again in "
                      "a moment.", user)
    except Exception as exc:                        # never leak a stack trace
        return _error(start_response, "500 Internal Server Error",
                      "Something went wrong",
                      f"Building the report failed: {exc}", user)


# -- account route handlers ---------------------------------------------------

def _signup(environ, start_response, method, user, cookie):
    if user is not None:
        return _redirect(start_response, "/account")
    if method in ("GET", "HEAD"):
        return _respond(start_response, "200 OK", signup_page(),
                        extra_headers=cookie)
    if method != "POST":
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the form.")
    f = _form_fields(environ)
    name, email = f.get("name", ""), f.get("email", "")
    username, password = f.get("username", ""), f.get("password", "")

    def fail(msg):
        return _respond(start_response, "400 Bad Request",
                        signup_page(msg, name, email, username),
                        extra_headers=cookie)

    if not _accounts.USERNAME_RE.match(username):
        return fail("Usernames are 3–30 characters: letters, digits, "
                    "'.', '_' or '-'.")
    if not _accounts.EMAIL_RE.match(email):
        return fail("That email address doesn't look right — please "
                    "check it and try again.")
    if len(password) < 8:
        return fail("Please choose a password of at least 8 characters.")
    store = get_account_store()
    try:
        new_user = store.create_user(username, email, password, name)
    except ValueError as exc:
        return fail(str(exc))
    token = store.create_session(new_user["id"])
    _record("signup")
    return _redirect(start_response, "/?welcome=1",
                     [_session_cookie_header(token), *cookie])


def _login(environ, start_response, method, user, cookie):
    if user is not None:
        return _redirect(start_response, "/account")
    if method in ("GET", "HEAD"):
        return _respond(start_response, "200 OK", login_page(),
                        extra_headers=cookie)
    if method != "POST":
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the form.")
    f = _form_fields(environ)
    identifier, password = f.get("identifier", ""), f.get("password", "")
    store = get_account_store()
    found = store.find_for_login(identifier) if identifier else None
    if found is None or not store.check_password(found, password):
        # Never reveal which part failed.
        return _respond(start_response, "401 Unauthorized",
                        login_page("That username/email and password "
                                   "don't match our records — please try "
                                   "again.", identifier),
                        extra_headers=cookie)
    token = store.create_session(found["id"])
    return _redirect(start_response, "/",
                     [_session_cookie_header(token), *cookie])


def _change_password(environ, start_response, method, user, visitor_id):
    if user is None:
        return _redirect(start_response, "/login")
    if method != "POST":
        return _redirect(start_response, "/account")
    store = get_account_store()
    used = len(get_usage_store().companies_today(
        _usage_key(user, visitor_id)))

    def show(message="", error=""):
        return _respond(start_response,
                        "400 Bad Request" if error else "200 OK",
                        account_page(user, used, message, error))

    f = _form_fields(environ)
    if not store.check_password(user, f.get("current_password", "")):
        return show(error="Your current password doesn't match — the "
                          "password was not changed.")
    new_pw = f.get("new_password", "")
    if len(new_pw) < 8:
        return show(error="Please choose a new password of at least 8 "
                          "characters.")
    store.set_password(user["id"], new_pw)
    # Other devices are logged out; this session stays.
    store.delete_user_sessions(user["id"],
                               except_token=_session_token(environ))
    return show(message="Your password has been changed.")


def _forgot_password(environ, start_response, method):
    if method in ("GET", "HEAD"):
        return _respond(start_response, "200 OK", forgot_password_page())
    if method != "POST":
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the form.")
    f = _form_fields(environ)
    store = get_account_store()
    identifier = f.get("identifier", "")
    if f.get("step") == "reset":
        found = store.find_for_recovery(identifier) if identifier else None
        new_pw = f.get("new_password", "")
        if len(new_pw) < 8:
            return _respond(start_response, "400 Bad Request",
                            forgot_password_code_page(
                                identifier, "",
                                "Please choose a new password of at "
                                "least 8 characters."))
        if found is None or not store.verify_code(
                found["id"], _accounts.PURPOSE_PASSWORD,
                f.get("code", "")):
            return _respond(start_response, "400 Bad Request",
                            forgot_password_code_page(
                                identifier, "",
                                "That code isn't right, or it has "
                                "expired — please check it or request a "
                                "new one."))
        store.set_password(found["id"], new_pw)
        store.delete_user_sessions(found["id"])   # log out everywhere
        return _respond(start_response, "200 OK", password_reset_done_page())
    found = store.find_for_recovery(identifier) if identifier else None
    notice, _ = _start_recovery(store, found, _accounts.PURPOSE_PASSWORD)
    return _respond(start_response, "200 OK",
                    forgot_password_code_page(identifier, notice))


def _forgot_username(environ, start_response, method):
    if method in ("GET", "HEAD"):
        return _respond(start_response, "200 OK", forgot_username_page())
    if method != "POST":
        return _error(start_response, "405 Method Not Allowed",
                      "Method not allowed", "Use the form.")
    f = _form_fields(environ)
    store = get_account_store()
    email = f.get("email", "")
    if f.get("step") == "verify":
        found = store.get_by_email(email) if email else None
        if found is None or not store.verify_code(
                found["id"], _accounts.PURPOSE_USERNAME, f.get("code", "")):
            return _respond(start_response, "400 Bad Request",
                            forgot_username_code_page(
                                email, "",
                                "That code isn't right, or it has "
                                "expired — please check it or request a "
                                "new one."))
        return _respond(start_response, "200 OK",
                        username_revealed_page(found["username"]))
    found = store.get_by_email(email) if email else None
    notice, _ = _start_recovery(store, found, _accounts.PURPOSE_USERNAME)
    return _respond(start_response, "200 OK",
                    forgot_username_code_page(email, notice))


# ---------------------------------------------------------------------------
# stdlib HTTP server

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "secdiff-web/0.1"

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "CONTENT_LENGTH": str(length),
            "HTTP_COOKIE": self.headers.get("Cookie", ""),
            "wsgi.input": io.BytesIO(body),
        }
        captured: dict = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers

        chunks = application(environ, start_response)
        data = b"".join(chunks)
        self.send_response(int(str(captured["status"]).split()[0]))
        for name, value in captured["headers"]:
            self.send_header(name, value)
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)

    def do_GET(self):   # noqa: N802 (http.server naming)
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_HEAD(self):  # noqa: N802
        self._dispatch("HEAD")

    def log_message(self, fmt, *args):  # keep deploy logs, one line each
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def serve(host: str, port: int) -> None:
    global _AUTO_REFRESH_MAJORS
    _AUTO_REFRESH_MAJORS = True   # the real server refreshes the majors
    server = ThreadingHTTPServer((host, port), _Handler)
    print(f"secdiff web: http://{host}:{port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    env_port = os.environ.get("PORT")
    p = argparse.ArgumentParser(
        prog="secdiff-web",
        description="Web front end for secdiff — git diff for SEC filings. "
                    "Not investment advice.")
    p.add_argument("--host", default=None,
                   help="Bind host (default: $HOST, else 0.0.0.0 when $PORT "
                        "is set, else 127.0.0.1)")
    p.add_argument("--port", type=int, default=None,
                   help="Bind port (default: $PORT, else 8000)")
    a = p.parse_args(argv)
    host = a.host or os.environ.get("HOST") or \
        ("0.0.0.0" if env_port else "127.0.0.1")
    port = a.port or (int(env_port) if env_port else 8000)
    serve(host, port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
