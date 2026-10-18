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
from .cli import live_pipeline, one_line_summary, render_reports
from .edgar import EdgarClient

__all__ = ["application", "main", "serve", "run_report",
           "parse_report_form", "ReportParams", "home_page", "error_page",
           "paywall_page", "UsageStore", "FREE_DAILY_COMPANIES",
           "AccountStore", "get_account_store",
           "StatsStore", "get_stats_store", "stats_page"]


# ---------------------------------------------------------------------------
# Form parameters (pure, unit-tested offline)

_QUERY_RE = re.compile(r"^[A-Z][A-Z0-9 .,&\-]{0,59}$")
_FORMS = ("10-K", "10-Q")


@dataclass
class ReportParams:
    ticker: str                      # upper-cased ticker or company name
    form: str = "10-K"
    risk_only: bool = False
    deletions_only: bool = False
    top: int | None = None


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
        top=top)


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
_STORE_LOCK = threading.Lock()


def get_usage_store() -> UsageStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = UsageStore(_cache_dir() / "web_usage.json")
        return _STORE


def _resolve_company(params: ReportParams) -> tuple[int, str]:
    """Resolve the visitor's ticker/name to (CIK, company) using the same
    verified EDGAR resolution the CLI uses. Raises SystemExit with the
    usual honest message when nothing matches — the caller turns that
    into the normal error page, and no free slot is consumed."""
    email = os.environ.get("EDGAR_EMAIL") or os.environ.get("SECDIFF_EMAIL") \
        or "you@example.com"
    client = EdgarClient(email=email, cache_dir=_cache_dir())
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
    """Top bar shown on every page: brand + account links."""
    if user:
        nav = (f"<span>Hi, <strong>{html.escape(user['username'])}</strong>"
               "</span>"
               "<a href='/account'>My account</a>"
               "<form method='post' action='/logout' style='display:inline'>"
               "<button type='submit' class='linkbtn'>Log out</button>"
               "</form>")
    else:
        nav = ("<a href='/login'>Log in</a>"
               "<a class='btn' href='/signup'>Create account</a>")
    return (f"<header class='topbar'><a class='brand' href='/'>secdiff</a>"
            f"<nav>{nav}</nav></header>")


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
<table class="kv">
  <tr><td>Unique visitors (all time)</td><td>{totals['visits']}</td></tr>
  <tr><td>Home pageviews (all time)</td><td>{totals['pageviews']}</td></tr>
  <tr><td>Reports run (all time)</td><td>{totals['reports']}</td></tr>
  <tr><td>Signups (all time)</td><td>{totals['signups']}</td></tr>
  <tr><td>Accounts</td><td>{accounts_count}</td></tr>
  <tr><td>Waitlist entries</td><td>{waitlist_count}</td></tr>
</table>
<h2>Last 14 days</h2>
<table class="data">
<tr><th>Date (UTC)</th><th>Unique visitors</th><th>Pageviews</th>
<th>Reports</th><th>Signups</th></tr>
{day_rows}
</table>
<h2>Top companies by reports</h2>
<table class="data">
<tr><th>Company</th><th>Reports</th></tr>
{top_rows}
</table>
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


# ---------------------------------------------------------------------------
# Running a report through the shared CLI pipeline

_RUN_LOCK = threading.Lock()


def _cli_args(params: ReportParams) -> SimpleNamespace:
    """The args namespace the shared pipeline functions expect (mirrors
    the CLI defaults: compare the latest filing against the previous one)."""
    return SimpleNamespace(
        ticker=params.ticker, form=params.form, years=1,
        risk_only=params.risk_only, deletions_only=params.deletions_only,
        top=params.top, section=None, summarize=False,
        email=None,
        cache_dir=os.environ.get("SECDIFF_CACHE_DIR") or None,
        output="secdiff-out")


def run_report(params: ReportParams,
               user: dict | None = None) -> tuple[str, str]:
    """Compute a report with the shared pipeline.

    Returns (one_line_summary, full_html_page). Pipeline failures
    propagate: SystemExit with a human-readable message (unknown ticker,
    not enough filings), urllib.error.URLError (EDGAR unreachable), or
    anything else — the HTTP layer turns them into honest error pages.
    """
    args = _cli_args(params)
    notes = io.StringIO()
    with _RUN_LOCK, redirect_stderr(notes):
        info, diffs, xbrl = live_pipeline(args)
        _md, page, counts = render_reports(args, info, diffs, xbrl)
    summary = one_line_summary(info, counts, xbrl)
    return summary, _wrap_report(page, notes.getvalue().strip(), user)


def _wrap_report(report_html: str, notes: str,
                 user: dict | None = None) -> str:
    """Add a way back home (and any honest stderr notes, e.g. the ticker
    resolved to a different EDGAR entity) to the shared report page."""
    account_link = ("<a href='/account'>My account</a>" if user
                    else "<a href='/login'>Log in</a>")
    nav = ("<div style='max-width:920px;margin:1rem auto 0;padding:0 1rem'>"
           "<a href='/'>&larr; New secdiff search</a>"
           f" &nbsp;·&nbsp; {account_link}</div>")
    if notes:
        items = "".join(f"<div>{html.escape(line)}</div>"
                        for line in notes.splitlines() if line.strip())
        nav += (f"<div style='max-width:920px;margin:.6rem auto 0;"
                f"padding:.6rem 1rem;background:#fffbeb;"
                f"border-left:4px solid #d97706;border-radius:4px;"
                f"font-size:.9rem'>{items}</div>")
    if "<body>" in report_html:
        return report_html.replace("<body>", "<body>" + nav, 1)
    return nav + report_html


# ---------------------------------------------------------------------------
# Pages

_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
margin:0 auto;max-width:920px;padding:2rem 1rem 3rem;color:#1a1a2e;line-height:1.55}
h1{font-size:1.9rem;margin:.2rem 0}
h2{margin-top:2.2rem;border-bottom:2px solid #e5e7eb;padding-bottom:.3rem}
.tagline{font-size:1.1rem;color:#374151}
.card{background:#f3f4f6;border-radius:10px;padding:1.2rem 1.4rem;margin:1.2rem 0}
label{display:block;font-weight:600;margin:.7rem 0 .2rem}
label.check{font-weight:400}
input[type=text],input[type=number],input[type=email],input[type=password],select{font-size:1rem;padding:.5rem .6rem;
border:1px solid #d1d5db;border-radius:6px;width:100%;box-sizing:border-box}
.topbar{display:flex;justify-content:space-between;align-items:center;
gap:1rem;margin-bottom:1.4rem}
.topbar .brand{font-weight:800;font-size:1.3rem;text-decoration:none;color:#1a1a2e}
.topbar nav{display:flex;gap:.9rem;align-items:center}
.topbar nav .btn{margin-top:0;padding:.45rem .95rem}
.linkbtn{background:none;border:0;color:#2563eb;cursor:pointer;
font-size:1rem;padding:0;text-decoration:underline;margin-top:0;font-weight:400}
.ok{background:#f0fdf4;border-left:4px solid #16a34a;border-radius:6px;
padding:1rem 1.2rem;margin:1.2rem 0}
table.kv{border-collapse:collapse;margin:1rem 0}
table.kv td{padding:.35rem 1.2rem .35rem 0;vertical-align:top}
table.kv td:first-child{font-weight:600;color:#374151}
table.data{border-collapse:collapse;margin:1rem 0}
table.data th,table.data td{padding:.35rem 1rem .35rem 0;text-align:left}
table.data th{border-bottom:2px solid #e5e7eb;color:#374151}
table.data td{border-bottom:1px solid #f3f4f6}
button,.btn{display:inline-block;background:#2563eb;color:#fff;border:0;
font-size:1rem;font-weight:600;padding:.65rem 1.3rem;border-radius:8px;
cursor:pointer;text-decoration:none;margin-top:1rem}
button:disabled{opacity:.55;cursor:wait}
.btn.secondary{background:#111827}
.row{display:grid;grid-template-columns:1fr 160px;gap:1rem}
@media (max-width:560px){.row{grid-template-columns:1fr}}
.spinner{width:34px;height:34px;border-radius:50%;border:4px solid #dbeafe;
border-top-color:#2563eb;animation:spin 0.9s linear infinite;margin:.4rem 0}
@keyframes spin{to{transform:rotate(360deg)}}
.note{color:#6b7280;font-size:.92rem}
.plans{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;margin:1.2rem 0}
@media (max-width:700px){.plans{grid-template-columns:1fr}}
.plan{background:#fff;border:2px solid #e5e7eb;border-radius:10px;
padding:1.2rem 1.3rem}
.plan.hot{border-color:#2563eb}
.plan h3{margin:.1rem 0 .4rem}
.price{font-size:1.7rem;font-weight:700}
.price span{font-size:.95rem;font-weight:400;color:#6b7280}
.plan ul{margin:.6rem 0;padding-left:1.15rem}
.plan .btn{margin-top:.6rem}
.err{background:#fef2f2;border-left:4px solid #dc2626;border-radius:6px;
padding:1rem 1.2rem;margin:1.2rem 0}
footer{margin-top:3rem;border-top:2px solid #e5e7eb;padding-top:1rem}
code{background:#eef2ff;padding:.1rem .35rem;border-radius:4px}
"""

_HOME_JS = """
document.getElementById('diff-form').addEventListener('submit', function () {
  document.getElementById('loading').hidden = false;
  var b = document.getElementById('go');
  b.disabled = true; b.textContent = 'Working\\u2026';
});
"""


def home_page(user: dict | None = None, notice: str = "") -> str:
    notice_html = (f"<div class='ok'><p>{html.escape(notice)}</p></div>"
                   if notice else "")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>secdiff — git diff for SEC filings</title><style>{_CSS}</style></head>
<body>
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
      </select>
    </div>
  </div>
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

<h2>How it works</h2>
<ol>
<li><strong>Fetch</strong> — secdiff resolves your ticker on SEC EDGAR
(verifying the resolved company actually files the form you asked for),
then downloads the latest filing and the one before it, plus the
company's XBRL data. Downloads are cached, and requests are paced well
under the SEC's fair-access limit of 10 requests/second.</li>
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
merged.</li>
<li><strong>Verify</strong> — every report links to both source filings
on EDGAR, so any change can be checked against the original document in
one click.</li>
</ol>

{pricing_section()}
<footer>
<p class="note"><strong>Not investment advice.</strong> secdiff shows
what changed in a public document; it makes no recommendations and has
no buy/sell outputs, price data or predictions. Data: SEC EDGAR (public
filings). The secdiff tool itself is open-source (MIT); this hosted
website is free for 4 companies a day, with paid plans above that — and
<code>secdiff</code> is a working title.</p>
</footer>
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
      <li>All features, 10-K and 10-Q</li>
      <li>For a single earnings week deep-dive</li>
    </ul>
    <a class="btn" href="#waitlist">Payments opening soon — join the waitlist</a>
  </div>
  <div class="plan hot">
    <h3>Monthly</h3>
    <div class="price">$27 <span>/ month</span></div>
    <ul>
      <li><strong>Unlimited companies</strong> while active</li>
      <li>All features, 10-K and 10-Q</li>
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
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>secdiff — daily free limit reached</title><style>{_CSS}</style></head>
<body>
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
<footer><p class="note"><strong>Not investment advice.</strong>
Data: SEC EDGAR (public filings).</p></footer>
</body></html>
"""


def waitlist_thanks_page(user: dict | None = None) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>secdiff — you're on the waitlist</title><style>{_CSS}</style></head>
<body>
{_header(user)}
<h1>You're on the waitlist ✅</h1>
<p class="tagline">Thanks — we'll email you when the Weekly ($9) and
Monthly ($27) plans open for payment.</p>
<p><a class="btn" href="/">&larr; Back to the search</a></p>
<footer><p class="note"><strong>Not investment advice.</strong>
Data: SEC EDGAR (public filings).</p></footer>
</body></html>
"""


def error_page(title: str, message: str, user: dict | None = None) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>secdiff — {html.escape(title)}</title><style>{_CSS}</style></head>
<body>
{_header(user)}
<h1>{html.escape(title)}</h1>
<div class="err"><p>{html.escape(message)}</p></div>
<p class="note">No report was generated and nothing was filled in or
guessed — when EDGAR doesn't give us the filings, we show you this page
instead of a made-up result.</p>
<p><a class="btn" href="/">&larr; Back to the search</a></p>
<footer><p class="note"><strong>Not investment advice.</strong>
Data: SEC EDGAR (public filings).</p></footer>
</body></html>
"""


# ---------------------------------------------------------------------------
# Account pages

def _shell(title: str, user: dict | None, inner: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>secdiff — {html.escape(title)}</title><style>{_CSS}</style></head>
<body>
{_header(user)}
{inner}
<footer><p class="note"><strong>Not investment advice.</strong>
Data: SEC EDGAR (public filings).</p></footer>
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
<table class="kv">
  <tr><td>Username</td><td>{html.escape(user['username'])}</td></tr>
  <tr><td>Email</td><td>{html.escape(user['email'])}</td></tr>
  <tr><td>Plan</td><td>{html.escape(plan)}</td></tr>
  <tr><td>Today</td><td>{used_today} of {FREE_DAILY_COMPANIES} free companies used</td></tr>
</table>
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


def application(environ, start_response):
    """WSGI app: / (search form + pricing), /report (POST form or GET
    query), /waitlist (POST), the account pages (/signup, /login,
    /logout, /account, /change-password, /forgot-password,
    /forgot-username), the owner-only /stats (404 for everyone but the
    STATS_USERNAME account), /healthz. All other paths 404; all failures
    are honest error pages."""
    method = (environ.get("REQUEST_METHOD") or "GET").upper()
    path = environ.get("PATH_INFO") or "/"
    visitor_id, visitor_is_new = _visitor_id(environ)
    cookie = [_cookie_header(visitor_id)] if visitor_is_new else []
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
            return _respond(start_response, "200 OK",
                            home_page(user, notice), extra_headers=cookie)
        if path == "/healthz":
            return _respond(start_response, "200 OK", "ok\n",
                            "text/plain; charset=utf-8")
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
