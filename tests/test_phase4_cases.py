"""The owner's 100-case suite for Phase 4 (no-demo/no-examples
audit, company pages, trending, biggest deletes on the home page,
watchlist, API + JSON endpoint, repeat-deletion patterns, sectors).
Same rule as Phases 1–3: run 100 cases, fix the small bugs, run
again, fix the big bugs, run again — go live only at 100/100.

Owner correction applied mid-phase: the /examples page was dropped
entirely ("kuch bhi demo or example nahi hona chahiye") — category A
now verifies that removal across the whole UI instead of testing
the page.

Every case is offline and deterministic: EDGAR is stubbed at the
web seams (live_pipeline / _resolve_company / EdgarClient), the
stores live in tmp dirs, and the pattern cases run the REAL diff
engine on crafted filing texts. Case numbers (P001–P100) are stable
so cycle reports can cite them.

Categories:
  A  No demo / no examples P001–P010
  B  Company pages         P011–P024
  C  Trending              P025–P032
  D  Biggest deletes       P033–P046
  E  Watchlist             P047–P066
  F  API                   P067–P084
  G  Patterns              P085–P094
  H  Sectors               P095–P100
"""
import io
import json
import re
import threading
import urllib.parse
from types import SimpleNamespace

import pytest

import secdiff.web as web
from secdiff.diffing import Hunk, SectionDiff
from secdiff.edgar import Filing
from secdiff.majors import MAJORS, SECTOR_MAP, MajorsStore, \
    sector_of, sort_entries
from secdiff.patterns import annotate_repeats, deleted_keys
from secdiff.report import FilingInfo
from secdiff.score import compute_score
from secdiff.watchlist import WatchlistStore
from secdiff.web import application
from secdiff.xbrl import DeltaRow, XbrlResult


# ---------------------------------------------------------------------------
# Harness (mirrors the earlier suites: same WSGI call + stub seams)

def _call(method, path, query="", body=b"", cookie=""):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path,
               "QUERY_STRING": query, "CONTENT_LENGTH": str(len(body)),
               "HTTP_COOKIE": cookie, "wsgi.input": io.BytesIO(body)}
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = headers

    chunks = application(environ, start_response)
    return captured["status"], captured["headers"], b"".join(chunks)


def _call_text(*a, **kw):
    status, headers, raw = _call(*a, **kw)
    return status, headers, raw.decode("utf-8")


def _info(form="10-K", new_date="2025-10-31", old_date="2024-11-01",
          ticker="EXMP", company="Example Corp", cik=12345):
    return FilingInfo(
        ticker=ticker, company=company, form=form, cik=cik,
        new_accession="0000012345-25-000111", new_date=new_date,
        new_url="https://www.sec.gov/new",
        new_index_url="https://www.sec.gov/new-idx",
        old_accession="0000012345-24-000111", old_date=old_date,
        old_url="https://www.sec.gov/old",
        old_index_url="https://www.sec.gov/old-idx",
        section_confidence="high")


_EMPTY_XBRL = XbrlResult(rows=[], unmapped=[], matched=0, considered=100)
_VID = "secdiff_vid=" + "cd" * 16


def _result(ticker="EXMP", form="10-K", diffs=None, rows=(),
            new_date="2025-10-31", old_date="2024-11-01", cik=12345):
    info = _info(form=form, ticker=ticker, new_date=new_date,
                 old_date=old_date, cik=cik)
    if diffs is None:
        diffs = [SectionDiff(item="1A", title="Risk Factors", hunks=[
            Hunk(kind="deletion",
                 old="A lawsuit was filed against the company.",
                 size=40),
            Hunk(kind="modification", old="Sales could fall slightly.",
                 new="Sales could fall sharply this year.", size=20),
            Hunk(kind="addition",
                 new="A brand new opportunity appeared.", size=30)])]
    x = XbrlResult(rows=list(rows), unmapped=[], matched=len(rows),
                   considered=100)
    return info, diffs, x


def _stub_pipeline(monkeypatch, resolve_map, results):
    """resolve_map: typed ticker -> cik or exception.
    results: (ticker, form) or ticker -> (info, diffs, xbrl)."""
    def fake_resolve(params):
        hit = resolve_map[params.ticker]
        if isinstance(hit, BaseException):
            raise hit
        return hit, "Fake Co"

    monkeypatch.setattr(web, "_resolve_company", fake_resolve)

    def fake_live(args):
        key = (args.ticker.upper(), args.form)
        if key in results:
            return results[key]
        return results[args.ticker.upper()]

    monkeypatch.setattr(web, "live_pipeline", fake_live)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """Every store on tmp files; background refresh OFF."""
    accounts = web.AccountStore(tmp_path / "secdiff.db")
    monkeypatch.setattr(web, "_ACCOUNTS", accounts)
    monkeypatch.setattr(web, "_STATS",
                        web.StatsStore(tmp_path / "secdiff.db"))
    monkeypatch.setattr(web, "_STORE",
                        web.UsageStore(tmp_path / "usage.json"))
    monkeypatch.setattr(web, "_WATCHLIST",
                        WatchlistStore(tmp_path / "secdiff.db"))
    monkeypatch.setattr(web, "_MAJORS",
                        MajorsStore(tmp_path / "biggest.json"))
    monkeypatch.setattr(web, "_refreshing", False)
    monkeypatch.setattr(web, "_AUTO_REFRESH_MAJORS", False)
    monkeypatch.setenv("SECDIFF_CACHE_DIR", str(tmp_path))
    return SimpleNamespace(accounts=accounts, tmp=tmp_path)


def _signup(username, email):
    body = (f"name=X&email={email}&username={username}"
            f"&password=password123").encode()
    status, headers, _raw = _call("POST", "/signup", body=body)
    assert status.startswith("303"), status
    token = None
    for name, value in headers:
        if name == "Set-Cookie" and value.startswith("secdiff_session="):
            token = value.split(";", 1)[0].split("=", 1)[1]
    assert token
    return "secdiff_session=" + token


def _major_entry(ticker, deletions, score=50, checked=None, cik=999):
    return {"ticker": ticker, "company": f"{ticker} Co", "cik": cik,
            "deletions": deletions, "modifications": 3, "additions": 2,
            "score": score, "band": "High", "new_date": "2026-02-01",
            "old_date": "2025-02-01",
            "checked": checked or web._today_utc()}


# ---------------------------------------------------------------------------
# A. No demo / no examples — owner correction (P001–P010)

def test_p001_examples_path_is_404(env):
    status, _h, text = _call_text("GET", "/examples", cookie=_VID)
    assert status.startswith("404")
    assert "Page not found" in text


def test_p002_examples_post_is_404_too(env):
    status, _h, _t = _call_text("POST", "/examples", cookie=_VID)
    assert status.startswith("404")


def test_p003_demo_path_still_404(env):
    status, _h, _t = _call_text("GET", "/demo", cookie=_VID)
    assert status.startswith("404")


def test_p004_home_has_no_examples_reference(env):
    _s, _h, text = _call_text("GET", "/", cookie=_VID)
    assert "/examples" not in text
    assert "Verified examples" not in text
    assert "verified examples" not in text


def test_p005_biggest_section_has_no_examples_link(env):
    store = web.get_majors_store()
    for t in MAJORS:
        store.put(_major_entry(t, 100))
    _s, _h, text = _call_text("GET", "/", cookie=_VID)
    assert "Biggest deletes" in text
    assert "/examples" not in text


def test_p006_api_docs_have_no_worked_response(env):
    _s, _h, text = _call_text("GET", "/api", cookie=_VID)
    # The endpoint is documented as a value-free schema and the
    # response fields are described in words — no JSON block with
    # made-up values anywhere on the page.
    assert "ticker={ticker}" in text
    assert '"company":' not in text
    assert '"deletions":' not in text
    assert '"top_deletions":' not in text


def test_p007_trending_has_no_examples_reference(env):
    _seed_reports("AAPL", 1)
    _s, _h, text = _call_text("GET", "/trending", cookie=_VID)
    assert "/examples" not in text


def test_p008_sectors_has_no_examples_reference(env):
    _s, _h, text = _call_text("GET", "/sectors", cookie=_VID)
    assert "/examples" not in text


def test_p009_company_page_has_no_examples_reference(env, monkeypatch):
    _use_fake_client(monkeypatch)
    _s, _h, text = _call_text("GET", "/company/FAKE", cookie=_VID)
    assert "/examples" not in text


def test_p010_report_page_has_no_examples_reference(env, monkeypatch):
    _stub_pipeline(monkeypatch, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, text = _call_text("POST", "/report", body=b"ticker=EXMP",
                                  cookie=_VID)
    assert status == "200 OK"
    assert "/examples" not in text


# ---------------------------------------------------------------------------
# B. Company pages (P011–P024)

def _filing(form, accession, date):
    return Filing(form=form, accession=accession, filing_date=date,
                  report_date="", primary_document="doc.htm")


_FAKE_CIK = 424242
_FAKE_FILINGS = {
    (_FAKE_CIK, "10-K"): [
        _filing("10-K", "0000424242-26-000001", "2026-02-01"),
        _filing("10-K", "0000424242-25-000001", "2025-02-01"),
        _filing("10-K", "0000424242-24-000001", "2024-02-01")],
    (_FAKE_CIK, "10-Q"): [
        _filing("10-Q", "0000424242-26-000101", "2026-08-01"),
        _filing("10-Q", "0000424242-26-000102", "2026-05-01")],
    (_FAKE_CIK, "8-K"): [
        _filing("8-K", "0000424242-26-000201", "2026-03-01")],
    (_FAKE_CIK, "DEF 14A"): [
        _filing("DEF 14A", "0000424242-26-000301", "2026-04-01"),
        _filing("DEF 14A", "0000424242-25-000301", "2025-04-01")],
}


class _FakeClient:
    name_matched_ticker = None

    def resolve_ticker(self, q, form="10-K", min_count=2):
        if q.upper() in ("FAKE", "BRK.B"):
            return _FAKE_CIK, "Fake Holdings Inc."
        raise SystemExit(f"Ticker not found on EDGAR: {q.upper()}")

    def all_filings(self, cik, form, count=None):
        return list(_FAKE_FILINGS.get((cik, form), []))


def _use_fake_client(monkeypatch, client=None):
    client = client or _FakeClient()
    monkeypatch.setattr(
        web, "EdgarClient",
        lambda email=None, cache_dir=None: client)
    return client


def test_p011_company_page_200(env, monkeypatch):
    _use_fake_client(monkeypatch)
    status, _h, _t = _call_text("GET", "/company/FAKE", cookie=_VID)
    assert status == "200 OK"


def test_p012_company_page_shows_name_and_cik(env, monkeypatch):
    _use_fake_client(monkeypatch)
    _s, _h, text = _call_text("GET", "/company/FAKE", cookie=_VID)
    assert "Fake Holdings Inc." in text
    assert "424242" in text
    assert "FAKE" in text


def test_p013_company_page_links_diffable_forms(env, monkeypatch):
    _use_fake_client(monkeypatch)
    _s, _h, text = _call_text("GET", "/company/FAKE", cookie=_VID)
    assert "/report?ticker=FAKE&amp;form=10-K" in text
    assert "/report?ticker=FAKE&amp;form=10-Q" in text
    assert "form=DEF+14A" in text or "form=DEF%2014A" in text \
        or "DEF 14A" in text


def test_p014_company_page_single_filing_form_not_linked(env, monkeypatch):
    _use_fake_client(monkeypatch)
    _s, _h, text = _call_text("GET", "/company/FAKE", cookie=_VID)
    assert "Only one filing on EDGAR" in text      # the 8-K row
    assert "form=8-K" not in text                  # no report link


def test_p015_company_page_lists_filing_years(env, monkeypatch):
    _use_fake_client(monkeypatch)
    _s, _h, text = _call_text("GET", "/company/FAKE", cookie=_VID)
    assert "2026, 2025, 2024" in text              # 10-K years, desc


def test_p016_company_unknown_ticker_404(env, monkeypatch):
    _use_fake_client(monkeypatch)
    status, _h, text = _call_text("GET", "/company/NOPE", cookie=_VID)
    assert status.startswith("404")
    assert "Company not found" in text


def test_p017_company_bad_segment_404(env, monkeypatch):
    _use_fake_client(monkeypatch)
    status, _h, _t = _call_text("GET", "/company/!!!", cookie=_VID)
    assert status.startswith("404")


def test_p018_company_nested_path_404(env, monkeypatch):
    _use_fake_client(monkeypatch)
    status, _h, _t = _call_text("GET", "/company/FAKE/EXTRA",
                                cookie=_VID)
    assert status.startswith("404")


def test_p019_company_post_405(env, monkeypatch):
    _use_fake_client(monkeypatch)
    status, _h, _t = _call_text("POST", "/company/FAKE", cookie=_VID)
    assert status.startswith("405")


def test_p020_company_report_count_shown_when_positive(env, monkeypatch):
    _use_fake_client(monkeypatch)
    store = web.get_stats_store()
    for _ in range(3):
        store.record("report", "FAKE")
    _s, _h, text = _call_text("GET", "/company/FAKE", cookie=_VID)
    assert "Reports run on this site" in text
    assert "<td>3</td>" in text


def test_p021_company_report_count_hidden_when_zero(env, monkeypatch):
    _use_fake_client(monkeypatch)
    _s, _h, text = _call_text("GET", "/company/FAKE", cookie=_VID)
    assert "Reports run on this site" not in text


def test_p022_company_major_summary_shown(env, monkeypatch):
    _use_fake_client(monkeypatch)
    web.get_majors_store().put(_major_entry("FAKE", 42, score=77))
    _s, _h, text = _call_text("GET", "/company/FAKE", cookie=_VID)
    assert "42 deletions" in text and "77/100" in text


def test_p023_company_name_match_label(env, monkeypatch):
    class NameClient(_FakeClient):
        name_matched_ticker = "FAKE"

        def resolve_ticker(self, q, form="10-K", min_count=2):
            if q.upper() == "FAKE HOLDINGS":
                return _FAKE_CIK, "Fake Holdings Inc."
            return super().resolve_ticker(q, form, min_count)

    _use_fake_client(monkeypatch, NameClient())
    status, _h, text = _call_text("GET", "/company/Fake%20Holdings",
                                  cookie=_VID)
    assert status == "200 OK"
    assert "Ticker <strong>FAKE</strong>" in text


def test_p024_company_dot_ticker_works(env, monkeypatch):
    _use_fake_client(monkeypatch)
    status, _h, text = _call_text("GET", "/company/BRK.B", cookie=_VID)
    assert status == "200 OK"
    assert "Fake Holdings Inc." in text


# ---------------------------------------------------------------------------
# C. Trending (P025–P032)

def _seed_reports(ticker, n):
    store = web.get_stats_store()
    for _ in range(n):
        store.record("report", ticker)


def test_p025_trending_orders_by_count_then_ticker(env):
    _seed_reports("MSFT", 3)
    _seed_reports("AAPL", 3)
    _seed_reports("TSLA", 1)
    _s, _h, text = _call_text("GET", "/trending", cookie=_VID)
    assert text.index("AAPL") < text.index("MSFT") < text.index("TSLA")


def test_p026_trending_links_company_and_report(env):
    _seed_reports("AAPL", 2)
    _s, _h, text = _call_text("GET", "/trending", cookie=_VID)
    assert "/company/AAPL" in text
    assert "/report?ticker=AAPL" in text


def test_p027_trending_shows_counts(env):
    _seed_reports("AAPL", 7)
    _s, _h, text = _call_text("GET", "/trending", cookie=_VID)
    assert "<td>7</td>" in text


def test_p028_trending_labels_its_window(env):
    _s, _h, text = _call_text("GET", "/trending", cookie=_VID)
    assert "all time" in text
    assert "this site" in text


def test_p029_trending_empty_state(env):
    status, _h, text = _call_text("GET", "/trending", cookie=_VID)
    assert status == "200 OK"
    assert "No reports have been run on this site yet" in text


def test_p030_trending_post_405(env):
    status, _h, _t = _call_text("POST", "/trending", cookie=_VID)
    assert status.startswith("405")


def test_p031_trending_consumes_no_slot(env):
    _seed_reports("AAPL", 2)
    _call("GET", "/trending", cookie=_VID)
    store = web.get_usage_store()
    vid = _VID.split("=", 1)[1]
    assert store.companies_today(vid) == []


def test_p032_trending_caps_at_25(env):
    for i in range(30):
        _seed_reports(f"T{i:02d}", 1)
    _s, _h, text = _call_text("GET", "/trending", cookie=_VID)
    assert text.count("/company/T") == 25


# ---------------------------------------------------------------------------
# D. Biggest deletes + majors store (P033–P046)

def test_p033_majors_store_roundtrip(tmp_path):
    store = MajorsStore(tmp_path / "m.json")
    entry = _major_entry("AAPL", 155, score=80)
    store.put(entry)
    got = MajorsStore(tmp_path / "m.json").get("AAPL")
    assert got == entry
    assert store.get("aapl") == entry          # case-insensitive key
    assert store.get("MSFT") is None


def test_p034_majors_store_corrupt_file_is_empty(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{not json", "utf-8")
    store = MajorsStore(path)
    assert store.load() == {}
    assert store.get("AAPL") is None


def test_p035_majors_fresh_today():
    entry = _major_entry("AAPL", 1, checked=web._today_utc())
    assert MajorsStore.is_fresh(entry)


def test_p036_majors_stale_and_missing_not_fresh():
    from datetime import datetime, timedelta, timezone
    yesterday = (datetime.now(timezone.utc).date()
                 - timedelta(days=1)).isoformat()
    entry = _major_entry("AAPL", 1, checked=yesterday)
    assert not MajorsStore.is_fresh(entry)
    assert not MajorsStore.is_fresh(None)
    assert not MajorsStore.is_fresh({})


def test_p037_sort_entries_ranking():
    entries = [_major_entry("B", 10, score=90),
               _major_entry("A", 10, score=90),
               _major_entry("C", 10, score=95),
               _major_entry("D", 20, score=1)]
    out = [e["ticker"] for e in sort_entries(entries)]
    assert out == ["D", "C", "A", "B"]


def test_p038_major_summary_from_pipeline(env, monkeypatch):
    monkeypatch.setattr(web, "_compute_data",
                        lambda params: _result(ticker="EXMP"))
    entry = web._major_summary("EXMP")
    assert entry["ticker"] == "EXMP"
    assert entry["deletions"] == 1
    assert entry["modifications"] == 1
    assert entry["additions"] == 1
    assert entry["score"] == compute_score(
        _result()[1], _result()[2]).total
    assert entry["checked"] == web._today_utc()
    assert entry["new_date"] == "2025-10-31"


def test_p039_refresh_worker_populates_and_skips_failures(
        env, monkeypatch):
    def fake_summary(ticker):
        if ticker == "BAD":
            raise RuntimeError("EDGAR down")
        return _major_entry(ticker, 5)

    monkeypatch.setattr(web, "_major_summary", fake_summary)
    store = web.get_majors_store()
    web._refresh_majors_worker(["GOOD", "BAD"], store)
    assert store.get("GOOD") is not None
    assert store.get("BAD") is None


def test_p040_refresh_worker_always_resets_flag(env, monkeypatch):
    monkeypatch.setattr(web, "_major_summary",
                        lambda t: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(web, "_refreshing", True)
    web._refresh_majors_worker(["X"], web.get_majors_store())
    assert web._refreshing is False


def test_p041_maybe_refresh_no_spawn_when_disabled(env):
    entries = web._maybe_refresh_majors()
    assert entries == {}
    assert web._refreshing is False
    assert web.get_majors_store().load() == {}


def test_p042_maybe_refresh_spawns_for_stale_when_enabled(
        env, monkeypatch):
    store = web.get_majors_store()
    store.put(_major_entry("AAPL", 155))       # fresh; others missing
    seen = {}
    done = threading.Event()

    def fake_worker(tickers, store_arg=None):
        seen["tickers"] = list(tickers)
        done.set()

    monkeypatch.setattr(web, "_refresh_majors_worker", fake_worker)
    monkeypatch.setattr(web, "_AUTO_REFRESH_MAJORS", True)
    web._maybe_refresh_majors()
    assert done.wait(5)
    assert set(seen["tickers"]) == set(MAJORS) - {"AAPL"}


def test_p043_home_biggest_table_sorted(env):
    store = web.get_majors_store()
    for t in MAJORS:
        store.put(_major_entry(t, 100))
    store.put(_major_entry("NVDA", 300, score=90))
    store.put(_major_entry("AAPL", 155, score=80))
    status, _h, text = _call_text("GET", "/", cookie=_VID)
    assert status == "200 OK"
    assert "Biggest deletes" in text
    # Order by the table's company links (the search form's placeholder
    # mentions AAPL long before the table does).
    assert text.index("/company/NVDA") < text.index("/company/AAPL") \
        < text.index("/company/MSFT")
    assert "last checked " + web._today_utc() in text
    assert "/report?ticker=AAPL" in text


def test_p044_home_biggest_computing_state(env):
    status, _h, text = _call_text("GET", "/", cookie=_VID)
    assert status == "200 OK"
    assert "Biggest deletes" in text
    assert "Computing the latest 10-K deletion counts" in text
    assert "/company/NVDA" not in text         # no fabricated rows


def test_p045_home_biggest_partial_shows_pending_note(env):
    store = web.get_majors_store()
    store.put(_major_entry("AAPL", 155, score=80))
    store.put(_major_entry("MSFT", 227, score=85))
    _s, _h, text = _call_text("GET", "/", cookie=_VID)
    assert "155" in text and "227" in text
    assert "still being" in text and "computed" in text


def test_p046_home_biggest_stale_entries_still_shown(env):
    from datetime import datetime, timedelta, timezone
    yesterday = (datetime.now(timezone.utc).date()
                 - timedelta(days=1)).isoformat()
    store = web.get_majors_store()
    for t in MAJORS:
        store.put(_major_entry(t, 100, checked=yesterday))
    _s, _h, text = _call_text("GET", "/", cookie=_VID)
    assert "last checked " + yesterday in text
    assert "still being" in text               # refresh is pending


# ---------------------------------------------------------------------------
# E. Watchlist (P047–P066)

_WL_RESOLVE = {"AAPL": 320193, "GOOGL": 1652044, "ALPHABET": 1652044,
               "MSFT": 789019}
_WL_NAMES = {320193: "Apple Inc.", 1652044: "Alphabet Inc.",
             789019: "MICROSOFT CORP"}


def _stub_watchlist_resolve(monkeypatch):
    def fake_resolve(params):
        cik = _WL_RESOLVE.get(params.ticker)
        if cik is None:
            raise SystemExit(f"Ticker not found on EDGAR: {params.ticker}")
        return cik, _WL_NAMES[cik]

    monkeypatch.setattr(web, "_resolve_company", fake_resolve)


def _add(cookie, ticker):
    body = urllib.parse.urlencode({"ticker": ticker}).encode()
    return _call("POST", "/watchlist/add", body=body, cookie=cookie)


def test_p047_watchlist_guest_get_redirects_to_login(env):
    status, headers, _t = _call_text("GET", "/watchlist", cookie=_VID)
    assert status.startswith("303")
    assert ("Location", "/login") in headers


def test_p048_watchlist_guest_add_redirects_to_login(env):
    status, headers, _t = _call_text(
        "POST", "/watchlist/add", body=b"ticker=AAPL", cookie=_VID)
    assert status.startswith("303")
    assert ("Location", "/login") in headers


def test_p049_watchlist_guest_remove_redirects_to_login(env):
    status, headers, _t = _call_text(
        "POST", "/watchlist/remove", body=b"cik=320193", cookie=_VID)
    assert status.startswith("303")
    assert ("Location", "/login") in headers


def test_p050_watchlist_empty_page_and_alerts_note(env):
    cookie = _signup("wlone", "wlone%40example.com")
    status, _h, text = _call_text("GET", "/watchlist", cookie=cookie)
    assert status == "200 OK"
    assert "Your watchlist is empty" in text
    assert "Email alerts are not switched on yet" in text
    assert "Nothing is emailed today" in text


def test_p051_watchlist_add_valid(env, monkeypatch):
    # Regression for the cold-start deadlock found in live testing:
    # the FIRST-EVER get_watchlist_store() call on a cold server used
    # to hang forever (it nested the account-store getter inside the
    # shared store lock). Recreate that cold state and prove the
    # getter returns — in a thread, so a regression fails fast
    # instead of hanging the suite.
    fixture_store = web._WATCHLIST
    web._WATCHLIST = None
    try:
        import threading as _th
        got = []
        t = _th.Thread(
            target=lambda: got.append(web.get_watchlist_store()),
            daemon=True)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive(), "get_watchlist_store deadlocked cold"
        assert isinstance(got[0], WatchlistStore)
        assert got[0].path == web.get_account_store().path
    finally:
        web._WATCHLIST = fixture_store
    _stub_watchlist_resolve(monkeypatch)
    cookie = _signup("wlone", "wlone%40example.com")
    status, headers, _t = _call_text(
        "POST", "/watchlist/add", body=b"ticker=AAPL", cookie=cookie)
    assert status.startswith("303")
    loc = dict(headers)["Location"]
    assert loc.startswith("/watchlist?msg=")
    user = env.accounts.get_by_username("wlone")
    entries = web.get_watchlist_store().list(user["id"])
    assert [e["cik"] for e in entries] == [320193]
    assert entries[0]["company"] == "Apple Inc."


def test_p052_watchlist_page_lists_entry_with_links(env, monkeypatch):
    _stub_watchlist_resolve(monkeypatch)
    cookie = _signup("wlone", "wlone%40example.com")
    _add(cookie, "AAPL")
    _s, _h, text = _call_text("GET", "/watchlist", cookie=cookie)
    assert "Apple Inc." in text
    assert "/company/AAPL" in text
    assert "/report?ticker=AAPL" in text


def test_p053_watchlist_duplicate_add_single_row(env, monkeypatch):
    _stub_watchlist_resolve(monkeypatch)
    cookie = _signup("wlone", "wlone%40example.com")
    _add(cookie, "AAPL")
    status, headers, _t = _call_text(
        "POST", "/watchlist/add", body=b"ticker=AAPL", cookie=cookie)
    loc = dict(headers)["Location"]
    assert "already" in urllib.parse.unquote(loc)
    user = env.accounts.get_by_username("wlone")
    assert len(web.get_watchlist_store().list(user["id"])) == 1


def test_p054_watchlist_duplicate_via_name_same_cik(env, monkeypatch):
    _stub_watchlist_resolve(monkeypatch)
    cookie = _signup("wlone", "wlone%40example.com")
    _add(cookie, "GOOGL")
    _add(cookie, "Alphabet")
    user = env.accounts.get_by_username("wlone")
    entries = web.get_watchlist_store().list(user["id"])
    assert len(entries) == 1 and entries[0]["cik"] == 1652044


def test_p055_watchlist_add_unknown_ticker_errors(env, monkeypatch):
    _stub_watchlist_resolve(monkeypatch)
    cookie = _signup("wlone", "wlone%40example.com")
    status, headers, _t = _call_text(
        "POST", "/watchlist/add", body=b"ticker=ZZZZZ", cookie=cookie)
    assert status.startswith("303")
    loc = dict(headers)["Location"]
    assert "err=" in loc
    assert "Ticker not found" in urllib.parse.unquote_plus(loc)
    user = env.accounts.get_by_username("wlone")
    assert web.get_watchlist_store().list(user["id"]) == []


def test_p056_watchlist_add_invalid_input_errors(env, monkeypatch):
    _stub_watchlist_resolve(monkeypatch)
    cookie = _signup("wlone", "wlone%40example.com")
    status, headers, _t = _call_text(
        "POST", "/watchlist/add", body=b"ticker=%21%21%21",
        cookie=cookie)
    assert status.startswith("303")
    assert "err=" in dict(headers)["Location"]
    user = env.accounts.get_by_username("wlone")
    assert web.get_watchlist_store().list(user["id"]) == []


def test_p057_watchlist_remove(env, monkeypatch):
    _stub_watchlist_resolve(monkeypatch)
    cookie = _signup("wlone", "wlone%40example.com")
    _add(cookie, "AAPL")
    status, _h, _t = _call_text(
        "POST", "/watchlist/remove", body=b"cik=320193", cookie=cookie)
    assert status.startswith("303")
    user = env.accounts.get_by_username("wlone")
    assert web.get_watchlist_store().list(user["id"]) == []
    _s, _h, text = _call_text("GET", "/watchlist", cookie=cookie)
    assert "Your watchlist is empty" in text


def test_p058_watchlist_remove_missing_is_harmless(env, monkeypatch):
    _stub_watchlist_resolve(monkeypatch)
    cookie = _signup("wlone", "wlone%40example.com")
    status, _h, _t = _call_text(
        "POST", "/watchlist/remove", body=b"cik=111", cookie=cookie)
    assert status.startswith("303")


def test_p059_watchlist_isolation_between_accounts(env, monkeypatch):
    _stub_watchlist_resolve(monkeypatch)
    c1 = _signup("wlone", "wlone%40example.com")
    c2 = _signup("wltwo", "wltwo%40example.com")
    _add(c1, "AAPL")
    _s, _h, text2 = _call_text("GET", "/watchlist", cookie=c2)
    assert "Your watchlist is empty" in text2
    assert "Apple Inc." not in text2


def test_p060_watchlist_remove_cannot_touch_other_account(
        env, monkeypatch):
    _stub_watchlist_resolve(monkeypatch)
    c1 = _signup("wlone", "wlone%40example.com")
    c2 = _signup("wltwo", "wltwo%40example.com")
    _add(c1, "AAPL")
    _call("POST", "/watchlist/remove", body=b"cik=320193", cookie=c2)
    user1 = env.accounts.get_by_username("wlone")
    assert len(web.get_watchlist_store().list(user1["id"])) == 1


def test_p061_watchlist_same_company_two_accounts(env, monkeypatch):
    _stub_watchlist_resolve(monkeypatch)
    c1 = _signup("wlone", "wlone%40example.com")
    c2 = _signup("wltwo", "wltwo%40example.com")
    _add(c1, "MSFT")
    _add(c2, "MSFT")
    store = web.get_watchlist_store()
    u1 = env.accounts.get_by_username("wlone")
    u2 = env.accounts.get_by_username("wltwo")
    assert len(store.list(u1["id"])) == 1
    assert len(store.list(u2["id"])) == 1


def test_p062_watchlist_last_known_score_shown(env, monkeypatch):
    _stub_watchlist_resolve(monkeypatch)
    web.get_majors_store().put(_major_entry("AAPL", 155, score=80))
    cookie = _signup("wlone", "wlone%40example.com")
    _add(cookie, "AAPL")
    _s, _h, text = _call_text("GET", "/watchlist", cookie=cookie)
    assert "155 deletions" in text and "score 80/100" in text


def test_p063_watchlist_score_not_computed_note(env, monkeypatch):
    _stub_watchlist_resolve(monkeypatch)
    cookie = _signup("wlone", "wlone%40example.com")
    _add(cookie, "MSFT")
    _s, _h, text = _call_text("GET", "/watchlist", cookie=cookie)
    assert "not computed yet" in text


def test_p064_watchlist_add_get_is_405(env):
    cookie = _signup("wlone", "wlone%40example.com")
    status, _h, _t = _call_text("GET", "/watchlist/add", cookie=cookie)
    assert status.startswith("405")


def test_p065_watchlist_store_add_return_values(tmp_path):
    store = WatchlistStore(tmp_path / "db.sqlite")
    assert store.add(1, 100, "AAA", "A Co") is True
    assert store.add(1, 100, "AAA", "A Co") is False   # same CIK again
    assert store.add(2, 100, "AAA", "A Co") is True    # other account
    assert store.ciks(1) == {100}


def test_p066_watchlist_store_remove_and_order(tmp_path):
    store = WatchlistStore(tmp_path / "db.sqlite")
    store.add(1, 300, "CCC", "C Co")
    store.add(1, 100, "AAA", "A Co")
    store.add(1, 200, "BBB", "B Co")
    assert [e["ticker"] for e in store.list(1)] == ["CCC", "AAA", "BBB"]
    assert store.remove(1, 100) is True
    assert store.remove(1, 100) is False
    assert [e["ticker"] for e in store.list(1)] == ["CCC", "BBB"]
    assert store.list(2) == []


# ---------------------------------------------------------------------------
# F. API (P067–P084)

_API_TICKERS = {"EXMP": 1001, "C2CO": 1002, "C3CO": 1003,
                "C4CO": 1004, "C5CO": 1005}


def _api_results():
    out = {}
    for t, cik in _API_TICKERS.items():
        out[t] = _result(ticker=t, cik=cik)
    return out


def _stub_api(monkeypatch):
    _stub_pipeline(monkeypatch, dict(_API_TICKERS), _api_results())


def test_p067_api_docs_page(env):
    status, _h, text = _call_text("GET", "/api", cookie=_VID)
    assert status == "200 OK"
    assert "/api/report?ticker={ticker}" in text
    assert "No API key" in text
    assert "4 distinct" in text
    assert "402" in text


def test_p068_api_docs_consumes_no_slot(env):
    _call("GET", "/api", cookie=_VID)
    vid = _VID.split("=", 1)[1]
    assert web.get_usage_store().companies_today(vid) == []


def test_p069_api_report_json_shape(env, monkeypatch):
    _stub_api(monkeypatch)
    status, headers, raw = _call("GET", "/api/report",
                                 query="ticker=EXMP", cookie=_VID)
    assert status == "200 OK"
    assert dict(headers)["Content-Type"].startswith("application/json")
    payload = json.loads(raw)
    for key in ("company", "ticker", "cik", "form", "filings",
                "counts", "score", "top_deletions", "report_url"):
        assert key in payload, key
    assert payload["ticker"] == "EXMP"
    assert payload["form"] == "10-K"
    assert payload["filings"]["newer"]["accession"] \
        == "0000012345-25-000111"
    assert payload["filings"]["older"]["date"] == "2024-11-01"
    assert payload["report_url"].startswith("/report?ticker=EXMP")


def test_p070_api_report_counts_match_pipeline(env, monkeypatch):
    _stub_api(monkeypatch)
    _s, _h, raw = _call("GET", "/api/report", query="ticker=EXMP",
                        cookie=_VID)
    payload = json.loads(raw)
    assert payload["counts"] == {"deletions": 1, "modifications": 1,
                                 "additions": 1, "xbrl_changed": 0}


def test_p071_api_report_score_matches(env, monkeypatch):
    _stub_api(monkeypatch)
    _s, _h, raw = _call("GET", "/api/report", query="ticker=EXMP",
                        cookie=_VID)
    payload = json.loads(raw)
    _info_, diffs, xbrl = _result(ticker="EXMP", cik=1001)
    expected = compute_score(diffs, xbrl)
    assert payload["score"]["total"] == expected.total
    assert payload["score"]["band"] == expected.band
    assert payload["score"]["breakdown"] == expected.breakdown()


def test_p072_api_top_deletions_biggest_first(env, monkeypatch):
    diffs = [SectionDiff(item="1A", title="Risk Factors", hunks=[
        Hunk(kind="deletion", old="Small deletion here.", size=10),
        Hunk(kind="deletion", old="The biggest deletion of them all.",
             size=90),
        Hunk(kind="deletion", old="A medium sized deletion.", size=50),
        Hunk(kind="addition", new="An addition, not a deletion.",
             size=99)])]
    _stub_pipeline(monkeypatch, {"EXMP": 1001},
                   {"EXMP": _result(diffs=diffs)})
    _s, _h, raw = _call("GET", "/api/report", query="ticker=EXMP",
                        cookie=_VID)
    payload = json.loads(raw)
    assert payload["top_deletions"] == [
        "The biggest deletion of them all.",
        "A medium sized deletion.",
        "Small deletion here."]


def test_p073_api_top_deletions_capped_at_10(env, monkeypatch):
    hunks = [Hunk(kind="deletion", old=f"Deletion number {i} here.",
                  size=100 - i) for i in range(12)]
    diffs = [SectionDiff(item="1A", title="Risk Factors", hunks=hunks)]
    _stub_pipeline(monkeypatch, {"EXMP": 1001},
                   {"EXMP": _result(diffs=diffs)})
    _s, _h, raw = _call("GET", "/api/report", query="ticker=EXMP",
                        cookie=_VID)
    payload = json.loads(raw)
    assert len(payload["top_deletions"]) == 10
    assert payload["counts"]["deletions"] == 12


def test_p074_api_missing_ticker_is_400_json(env, monkeypatch):
    _stub_api(monkeypatch)
    status, headers, raw = _call("GET", "/api/report", query="",
                                 cookie=_VID)
    assert status.startswith("400")
    assert dict(headers)["Content-Type"].startswith("application/json")
    assert "error" in json.loads(raw)


def test_p075_api_bad_form_is_400_json(env, monkeypatch):
    _stub_api(monkeypatch)
    status, _h, raw = _call("GET", "/api/report",
                            query="ticker=EXMP&form=NOPE", cookie=_VID)
    assert status.startswith("400")
    assert "error" in json.loads(raw)


def test_p076_api_unknown_ticker_is_404_json(env, monkeypatch):
    _stub_pipeline(monkeypatch,
                   {"ZZZZZ": SystemExit("Ticker not found: ZZZZZ")},
                   {})
    status, _h, raw = _call("GET", "/api/report",
                            query="ticker=ZZZZZ", cookie=_VID)
    assert status.startswith("404")
    assert "Ticker not found" in json.loads(raw)["error"]


def test_p077_api_fifth_company_is_402_json(env, monkeypatch):
    _stub_api(monkeypatch)
    for t in ("EXMP", "C2CO", "C3CO", "C4CO"):
        status, _h, _r = _call("GET", "/api/report",
                               query=f"ticker={t}", cookie=_VID)
        assert status == "200 OK", t
    status, _h, raw = _call("GET", "/api/report",
                            query="ticker=C5CO", cookie=_VID)
    assert status.startswith("402")
    assert "error" in json.loads(raw)


def test_p078_api_limit_shared_with_html(env, monkeypatch):
    _stub_api(monkeypatch)
    for t in ("EXMP", "C2CO", "C3CO", "C4CO"):
        status, _h, _r = _call_text("POST", "/report",
                                    body=f"ticker={t}".encode(),
                                    cookie=_VID)
        assert status == "200 OK", t
    status, _h, raw = _call("GET", "/api/report",
                            query="ticker=C5CO", cookie=_VID)
    assert status.startswith("402")


def test_p079_html_limit_shared_with_api(env, monkeypatch):
    _stub_api(monkeypatch)
    for t in ("EXMP", "C2CO", "C3CO", "C4CO"):
        status, _h, _r = _call("GET", "/api/report",
                               query=f"ticker={t}", cookie=_VID)
        assert status == "200 OK", t
    status, _h, _t = _call_text("POST", "/report", body=b"ticker=C5CO",
                                cookie=_VID)
    assert status.startswith("402")


def test_p080_api_same_company_repeat_is_free(env, monkeypatch):
    _stub_api(monkeypatch)
    for _ in range(3):
        status, _h, _r = _call("GET", "/api/report",
                               query="ticker=EXMP", cookie=_VID)
        assert status == "200 OK"
    vid = _VID.split("=", 1)[1]
    assert web.get_usage_store().companies_today(vid) == [1001]


def test_p081_api_post_is_405_json(env, monkeypatch):
    _stub_api(monkeypatch)
    status, _h, raw = _call("POST", "/api/report",
                            body=b"ticker=EXMP", cookie=_VID)
    assert status.startswith("405")
    assert "error" in json.loads(raw)


def test_p082_api_report_recorded_in_stats(env, monkeypatch):
    _stub_api(monkeypatch)
    _call("GET", "/api/report", query="ticker=EXMP", cookie=_VID)
    assert web.get_stats_store().top_companies() == [("EXMP", 1)]


def test_p083_api_form_parameter_honoured(env, monkeypatch):
    q_diffs = [SectionDiff(item="1A", title="Risk Factors", hunks=[
        Hunk(kind="deletion", old="Quarterly deletion one.", size=30),
        Hunk(kind="deletion", old="Quarterly deletion two.", size=30)])]
    results = {("EXMP", "10-K"): _result(ticker="EXMP", cik=1001),
               ("EXMP", "10-Q"): _result(ticker="EXMP", form="10-Q",
                                         diffs=q_diffs, cik=1001)}
    _stub_pipeline(monkeypatch, {"EXMP": 1001}, results)
    _s, _h, raw = _call("GET", "/api/report",
                        query="ticker=EXMP&form=10-Q", cookie=_VID)
    payload = json.loads(raw)
    assert payload["form"] == "10-Q"
    assert payload["counts"]["deletions"] == 2


def test_p084_api_head_works(env, monkeypatch):
    _stub_api(monkeypatch)
    status, _h, _r = _call("HEAD", "/api/report",
                           query="ticker=EXMP", cookie=_VID)
    assert status == "200 OK"


# ---------------------------------------------------------------------------
# G. Patterns (P085–P094)

_REPEAT = ("The company faces significant risks from supply chain "
           "disruptions in Asia.")


def _sd(*hunks, item="1A"):
    return [SectionDiff(item=item, title="Risk Factors",
                        hunks=list(hunks))]


def test_p085_pattern_true_positive():
    current = _sd(Hunk("deletion", old=_REPEAT, size=80))
    earlier = _sd(Hunk("deletion", old=_REPEAT, size=80))
    n = annotate_repeats(current, earlier, "/report?ticker=X&vs_year=1")
    assert n == 1
    h = current[0].hunks[0]
    assert h.note == "Also deleted in the previous cycle"
    assert h.note_url == "/report?ticker=X&vs_year=1"


def test_p086_pattern_unique_phrase_no_note():
    current = _sd(Hunk("deletion", old=_REPEAT, size=80))
    earlier = _sd(Hunk(
        "deletion",
        old="A completely different sentence was removed last year.",
        size=60))
    assert annotate_repeats(current, earlier) == 0
    assert current[0].hunks[0].note == ""
    assert annotate_repeats(current, []) == 0


def test_p087_pattern_short_phrase_never_matches():
    short = "Risks remain elevated this year."     # 5 words
    current = _sd(Hunk("deletion", old=short, size=30))
    earlier = _sd(Hunk("deletion", old=short, size=30))
    assert annotate_repeats(current, earlier) == 0
    assert deleted_keys(earlier) == set()


def test_p088_pattern_earlier_modification_does_not_count():
    current = _sd(Hunk("deletion", old=_REPEAT, size=80))
    earlier = _sd(Hunk("modification", old=_REPEAT,
                       new="Reworded but still present this year.",
                       size=40))
    assert annotate_repeats(current, earlier) == 0


def test_p089_pattern_normalisation_case_and_spacing():
    messy = ("THE COMPANY   FACES SIGNIFICANT RISKS FROM SUPPLY "
             "CHAIN DISRUPTIONS IN ASIA.")
    current = _sd(Hunk("deletion", old=messy, size=80))
    earlier = _sd(Hunk("deletion", old=_REPEAT, size=80))
    assert annotate_repeats(current, earlier) == 1


def test_p090_pattern_only_deletions_annotated_and_counted():
    current = _sd(
        Hunk("deletion", old=_REPEAT, size=80),
        Hunk("modification", old=_REPEAT, new="Still here, reworded.",
             size=30),
        Hunk("addition", new=_REPEAT, size=80))
    earlier = _sd(Hunk("deletion", old=_REPEAT, size=80))
    n = annotate_repeats(current, earlier, "/r")
    assert n == 1
    assert current[0].hunks[0].note != ""
    assert current[0].hunks[1].note == ""
    assert current[0].hunks[2].note == ""


# -- web integration: real diff engine over crafted filing texts ------

_S = _REPEAT
_X1 = "Revenue grew steadily across all reportable segments this year."
_X2 = "The board approved a new share repurchase program in November."
_DOC3 = f"{_S} {_X1} {_S} {_X2}"        # oldest: the sentence twice
_DOC2 = f"{_S} {_X1} {_X2}"            # middle: once (one was deleted)
_DOC1 = f"{_X1} {_X2}"                 # newest: gone (deleted again)


class _PatternClient:
    name_matched_ticker = None
    FILINGS = [
        Filing("10-K", "0000012345-25-000111", "2025-11-01", "",
               "doc.htm"),
        Filing("10-K", "0000012345-24-000111", "2024-11-01", "",
               "doc.htm"),
        Filing("10-K", "0000012345-23-000111", "2023-11-01", "",
               "doc.htm")]
    TEXTS = {"0000012345-25-000111": _DOC1,
             "0000012345-24-000111": _DOC2,
             "0000012345-23-000111": _DOC3}

    def __init__(self, filings=None, texts=None):
        self._filings = filings if filings is not None else self.FILINGS
        self._texts = texts if texts is not None else self.TEXTS

    def all_filings(self, cik, form, count=None):
        return list(self._filings)

    def filing_text(self, cik, filing):
        return self._texts[filing.accession]


def _pattern_report(env, monkeypatch, client, subs=True):
    """Run POST /report for EXMP with the pipeline stubbed to the
    crafted current pair and the pattern client behind _edgar_client."""
    from secdiff.cli import build_diffs
    diffs, _conf = build_diffs(_DOC2, _DOC1, "10-K")
    info = _info(ticker="EXMP", cik=12345)
    results = {"EXMP": (info, diffs, _EMPTY_XBRL)}
    _stub_pipeline(monkeypatch, {"EXMP": 12345}, results)
    monkeypatch.setattr(web, "EdgarClient",
                        lambda email=None, cache_dir=None: client)
    if subs:
        (env.tmp / "submissions_0000012345.json").write_text("{}",
                                                             "utf-8")
    return _call_text("POST", "/report", body=b"ticker=EXMP",
                      cookie=_VID)


def test_p091_pattern_note_rendered_with_link(env, monkeypatch):
    status, _h, text = _pattern_report(env, monkeypatch,
                                       _PatternClient())
    assert status == "200 OK"
    assert "Also deleted in the previous cycle" in text
    assert "vs_year=2024" in text and "base_year=2023" in text


def test_p092_pattern_absent_without_repeat(env, monkeypatch):
    texts = dict(_PatternClient.TEXTS)
    texts["0000012345-23-000111"] = f"{_X1} {_X2}"   # no earlier copy
    status, _h, text = _pattern_report(
        env, monkeypatch, _PatternClient(texts=texts))
    assert status == "200 OK"
    assert "Also deleted in the previous cycle" not in text


def test_p093_pattern_absent_without_earlier_pair(env, monkeypatch):
    client = _PatternClient(filings=_PatternClient.FILINGS[:2])
    status, _h, text = _pattern_report(env, monkeypatch, client)
    assert status == "200 OK"
    assert "Also deleted in the previous cycle" not in text


def test_p094_pattern_skipped_without_cached_submissions(
        env, monkeypatch):
    status, _h, text = _pattern_report(env, monkeypatch,
                                       _PatternClient(), subs=False)
    assert status == "200 OK"
    assert "Also deleted in the previous cycle" not in text


# ---------------------------------------------------------------------------
# H. Sectors (P095–P100)

def test_p095_sector_map_covers_all_majors():
    for t in MAJORS:
        assert sector_of(t), t


def test_p096_sectors_grouping_order_and_ranking(env):
    store = web.get_majors_store()
    store.put(_major_entry("AAPL", 155, score=80))
    store.put(_major_entry("MSFT", 227, score=85))
    store.put(_major_entry("XOM", 158, score=70))
    _s, _h, text = _call_text("GET", "/sectors", cookie=_VID)
    tech = text.index("Technology")
    energy = text.index("Energy")
    assert tech < energy                       # SECTOR_ORDER respected
    assert text.index("MSFT") < text.index("AAPL")   # 227 before 155


def test_p097_sectors_curated_scope_label(env):
    _s, _h, text = _call_text("GET", "/sectors", cookie=_VID)
    assert "curated list" in text
    assert "not full-market coverage" in text


def test_p098_sectors_include_trending_mapped_company(env):
    _seed_reports("JPM", 2)
    web.get_majors_store().put(_major_entry("JPM", 50, score=40))
    _s, _h, text = _call_text("GET", "/sectors", cookie=_VID)
    assert "Financials" in text
    assert "JPM" in text


def test_p099_sectors_exclude_unmapped_trending(env):
    _seed_reports("ZZZZ", 5)
    status, _h, text = _call_text("GET", "/sectors", cookie=_VID)
    assert status == "200 OK"
    assert "ZZZZ" not in text


def test_p100_sectors_empty_state_and_post_405(env):
    status, _h, text = _call_text("GET", "/sectors", cookie=_VID)
    assert status == "200 OK"
    assert "No sector summaries have been computed yet" in text
    assert "Still computing:" in text and "AAPL" in text
    status, _h, _t = _call_text("POST", "/sectors", cookie=_VID)
    assert status.startswith("405")
