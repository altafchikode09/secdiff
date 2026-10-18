"""Web layer tests — all offline.

The live route is exercised by monkeypatching the shared pipeline entry
point the web module calls (secdiff.web.live_pipeline); the real EDGAR
path is verified live against the running server, not by unit tests.
There is no demo route or demo content on the website by design: every
report the site produces is generated live from EDGAR at request time.
"""
import io

import pytest

import secdiff.web as web
from secdiff.web import application, parse_report_form


# -- form parsing -----------------------------------------------------------

def test_parse_minimal_form():
    p = parse_report_form("ticker=aapl")
    assert p.ticker == "AAPL"
    assert p.form == "10-K"
    assert p.risk_only is False and p.deletions_only is False
    assert p.top is None


def test_parse_full_form():
    p = parse_report_form(
        "ticker=brk.b&form=10-Q&risk_only=on&deletions_only=on&top=25")
    assert p.ticker == "BRK.B"
    assert p.form == "10-Q"
    assert p.risk_only is True and p.deletions_only is True
    assert p.top == 25


def test_parse_demo_word_is_an_ordinary_ticker():
    # "demo" has no special meaning on the website: it is parsed as an
    # ordinary ticker string and would be looked up on EDGAR like any
    # other (and rejected there if no such ticker exists).
    p = parse_report_form("ticker=demo")
    assert p.ticker == "DEMO"


def test_parse_company_name_is_accepted():
    # Company names are valid input now (resolution maps them to a ticker
    # and says so); spaces are collapsed and the value is upper-cased.
    p = parse_report_form("ticker=Berkshire++Hathaway")
    assert p.ticker == "BERKSHIRE HATHAWAY"


@pytest.mark.parametrize("body", [
    "",                     # missing ticker
    "ticker=",              # blank ticker
    "ticker=AAPL&form=8-K",  # unsupported form
    "ticker=AAPL&top=abc",  # non-numeric top
    "ticker=AAPL&top=0",    # out-of-range top
    "ticker=AAPL&top=9999",  # out-of-range top
])
def test_parse_rejects_bad_input(body):
    with pytest.raises(ValueError):
        parse_report_form(body)


# -- WSGI application ---------------------------------------------------------

def call_app(method, path, query="", body=b"", cookie=""):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "HTTP_COOKIE": cookie,
        "wsgi.input": io.BytesIO(body),
    }
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = dict(headers)

    chunks = application(environ, start_response)
    text = b"".join(chunks).decode("utf-8")
    return captured["status"], text


def call_app_h(method, path, query="", body=b"", cookie=""):
    """Like call_app but also returns the response headers dict."""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "HTTP_COOKIE": cookie,
        "wsgi.input": io.BytesIO(body),
    }
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = dict(headers)

    chunks = application(environ, start_response)
    text = b"".join(chunks).decode("utf-8")
    return captured["status"], captured["headers"], text


def stub_resolution(monkeypatch, tmp_path, mapping):
    """Stub EDGAR resolution + usage store for /report tests.

    mapping: ticker -> CIK int, or ticker -> exception instance to raise.
    Returns the UsageStore in use so tests can inspect consumption."""
    def fake_resolve(params):
        hit = mapping[params.ticker]
        if isinstance(hit, BaseException):   # SystemExit is a BaseException
            raise hit
        return hit, f"Company {params.ticker}"

    store = web.UsageStore(tmp_path / "usage.json")
    monkeypatch.setattr(web, "_resolve_company", fake_resolve)
    monkeypatch.setattr(web, "_STORE", store)
    return store


def test_home_page():
    status, text = call_app("GET", "/")
    assert status == "200 OK"
    assert 'name="ticker"' in text
    assert 'name="form"' in text and "10-Q" in text
    assert 'name="risk_only"' in text and 'name="deletions_only"' in text
    assert 'name="top"' in text
    assert 'id="loading"' in text            # loading state
    assert "How it works" in text
    assert "Not investment advice" in text   # footer disclaimer


def test_home_page_has_no_demo_anywhere():
    _status, text = call_app("GET", "/")
    assert "/demo" not in text
    assert "demo" not in text.lower()


def test_demo_path_does_not_exist():
    status, _text = call_app("GET", "/demo")
    assert status.startswith("404")


def test_healthz():
    status, text = call_app("GET", "/healthz")
    assert status == "200 OK" and text.strip() == "ok"


def _fake_pipeline_result(ticker):
    """A pipeline result shaped exactly like a real live_pipeline return,
    so render_reports/render_html run for real in this test."""
    from secdiff.diffing import Hunk, SectionDiff
    from secdiff.report import FilingInfo
    from secdiff.xbrl import XbrlResult

    info = FilingInfo(
        ticker=ticker, company="Example Corp", form="10-K", cik=12345,
        new_accession="0000012345-26-000001", new_date="2026-02-01",
        new_url="https://www.sec.gov/example-new",
        new_index_url="https://www.sec.gov/example-new-index",
        old_accession="0000012345-25-000001", old_date="2025-02-01",
        old_url="https://www.sec.gov/example-old",
        old_index_url="https://www.sec.gov/example-old-index",
        section_confidence="high")
    diffs = [SectionDiff(item="1A", title="Risk Factors", hunks=[
        Hunk(kind="deletion", old="A sentence that was removed.", size=10),
        Hunk(kind="addition", new="A sentence that was added.", size=10),
    ])]
    xbrl = XbrlResult(rows=[], unmapped=[], matched=0, considered=100)
    return info, diffs, xbrl


def test_report_route_uses_live_pipeline(monkeypatch, tmp_path):
    calls = []

    def fake_live(args):
        calls.append(args.ticker)
        return _fake_pipeline_result(args.ticker.upper())

    monkeypatch.setattr(web, "live_pipeline", fake_live)
    stub_resolution(monkeypatch, tmp_path, {"EXMP": 12345})
    status, text = call_app("POST", "/report",
                            body=b"ticker=EXMP&form=10-K&risk_only=on")
    assert status == "200 OK"
    assert calls == ["EXMP"]                  # the live pipeline was used
    assert "1 deletions" in text and "1 additions" in text
    assert "0000012345-26-000001" in text     # accession from the pipeline
    assert "New secdiff search" in text       # web chrome added


def test_report_unknown_ticker_shows_honest_error(monkeypatch, tmp_path):
    store = stub_resolution(
        monkeypatch, tmp_path,
        {"ZZZZZ": SystemExit("Ticker not found on EDGAR: ZZZZZ")})
    status, text = call_app("POST", "/report", body=b"ticker=ZZZZZ")
    assert status.startswith("400")
    assert "Ticker not found on EDGAR: ZZZZZ" in text
    assert "No report was generated" in text
    # A failed resolution must not consume a free slot.
    assert store._load() == {}


def test_report_edgar_unreachable_shows_honest_error(monkeypatch, tmp_path):
    import urllib.error

    def fake_live(args):
        raise urllib.error.URLError("Name or service not known")

    monkeypatch.setattr(web, "live_pipeline", fake_live)
    stub_resolution(monkeypatch, tmp_path, {"AAPL": 320193})
    status, text = call_app("POST", "/report", body=b"ticker=AAPL")
    assert status.startswith("502")
    assert "SEC EDGAR could not be reached" in text


def test_report_bad_input_is_400():
    status, text = call_app("POST", "/report", body=b"ticker=")
    assert status.startswith("400")
    assert "Please enter a ticker symbol" in text


def test_unknown_path_is_404():
    status, text = call_app("GET", "/nope")
    assert status.startswith("404")
    assert "Page not found" in text


# -- free daily limit + pricing ----------------------------------------------

def test_usage_store_counts_distinct_companies(tmp_path):
    store = web.UsageStore(tmp_path / "u.json")
    for cik in (1, 2, 3, 4):
        assert store.check_and_add("v1", cik, day="2026-10-17") is True
    assert store.check_and_add("v1", 5, day="2026-10-17") is False
    # A different visitor has their own allowance.
    assert store.check_and_add("v2", 5, day="2026-10-17") is True


def test_usage_store_repeat_company_is_free(tmp_path):
    store = web.UsageStore(tmp_path / "u.json")
    for _ in range(6):
        assert store.check_and_add("v1", 42, day="2026-10-17") is True
    assert store.companies_today("v1", day="2026-10-17") == [42]


def test_usage_store_day_rollover_resets(tmp_path):
    store = web.UsageStore(tmp_path / "u.json")
    for cik in (1, 2, 3, 4):
        assert store.check_and_add("v1", cik, day="2026-10-17") is True
    assert store.check_and_add("v1", 5, day="2026-10-17") is False
    assert store.check_and_add("v1", 5, day="2026-10-18") is True
    assert store.companies_today("v1", day="2026-10-18") == [5]


def test_usage_store_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "u.json"
    path.write_text("not json {{{", "utf-8")
    store = web.UsageStore(path)
    assert store.check_and_add("v1", 7, day="2026-10-17") is True
    assert store.companies_today("v1", day="2026-10-17") == [7]


def test_home_page_shows_pricing_and_waitlist():
    status, text = call_app("GET", "/")
    assert status == "200 OK"
    assert "$9" in text and "$27" in text
    assert "4 companies per day" in text
    assert 'action="/waitlist"' in text
    assert "Payments opening soon" in text


def test_visitor_cookie_is_set_and_reused():
    status, headers, _text = call_app_h("GET", "/")
    assert status == "200 OK"
    assert headers["Set-Cookie"].startswith("secdiff_vid=")
    assert "HttpOnly" in headers["Set-Cookie"]
    vid = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    # Presenting the cookie back must not mint a new one.
    _s, headers2, _t = call_app_h("GET", "/", cookie=f"secdiff_vid={vid}")
    assert "Set-Cookie" not in headers2


def test_fifth_distinct_company_gets_paywall(monkeypatch, tmp_path):
    def fake_live(args):
        return _fake_pipeline_result(args.ticker.upper())

    monkeypatch.setattr(web, "live_pipeline", fake_live)
    mapping = {t: 1000 + i for i, t in
               enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"])}
    stub_resolution(monkeypatch, tmp_path, mapping)
    cookie = "secdiff_vid=" + "ab" * 16
    for t in ("AAA", "BBB", "CCC", "DDD"):
        status, _text = call_app("POST", "/report",
                                 body=f"ticker={t}".encode(), cookie=cookie)
        assert status == "200 OK", t
    # 5th distinct company -> paywall with both paid plans
    status, text = call_app("POST", "/report", body=b"ticker=EEE",
                            cookie=cookie)
    assert status.startswith("402")
    assert "$9" in text and "$27" in text
    assert "4 free companies" in text and "reset tomorrow" in text
    # Re-viewing an already-counted company still works at the limit.
    status, _text = call_app("POST", "/report", body=b"ticker=AAA",
                             cookie=cookie)
    assert status == "200 OK"


def test_waitlist_signup_and_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("SECDIFF_CACHE_DIR", str(tmp_path))
    status, text = call_app("POST", "/waitlist",
                            body=b"name=Test+User&email=test%40example.com")
    assert status == "200 OK"
    assert "on the waitlist" in text
    lines = (tmp_path / "waitlist.jsonl").read_text("utf-8").strip().splitlines()
    assert len(lines) == 1 and "test@example.com" in lines[0]
    status, _text = call_app("POST", "/waitlist",
                             body=b"name=X&email=not-an-email")
    assert status.startswith("400")
