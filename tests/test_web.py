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


@pytest.mark.parametrize("body", [
    "",                     # missing ticker
    "ticker=",              # blank ticker
    "ticker=AA%20PL",       # space in ticker
    "ticker=AAPL&form=8-K",  # unsupported form
    "ticker=AAPL&top=abc",  # non-numeric top
    "ticker=AAPL&top=0",    # out-of-range top
    "ticker=AAPL&top=9999",  # out-of-range top
])
def test_parse_rejects_bad_input(body):
    with pytest.raises(ValueError):
        parse_report_form(body)


# -- WSGI application ---------------------------------------------------------

def call_app(method, path, query="", body=b""):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = dict(headers)

    chunks = application(environ, start_response)
    text = b"".join(chunks).decode("utf-8")
    return captured["status"], text


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


def test_report_route_uses_live_pipeline(monkeypatch):
    calls = []

    def fake_live(args):
        calls.append(args.ticker)
        return _fake_pipeline_result(args.ticker.upper())

    monkeypatch.setattr(web, "live_pipeline", fake_live)
    status, text = call_app("POST", "/report",
                            body=b"ticker=EXMP&form=10-K&risk_only=on")
    assert status == "200 OK"
    assert calls == ["EXMP"]                  # the live pipeline was used
    assert "1 deletions" in text and "1 additions" in text
    assert "0000012345-26-000001" in text     # accession from the pipeline
    assert "New secdiff search" in text       # web chrome added


def test_report_unknown_ticker_shows_honest_error(monkeypatch):
    def fake_live(args):
        raise SystemExit("Ticker not found on EDGAR: ZZZZZ")

    monkeypatch.setattr(web, "live_pipeline", fake_live)
    status, text = call_app("POST", "/report", body=b"ticker=ZZZZZ")
    assert status.startswith("400")
    assert "Ticker not found on EDGAR: ZZZZZ" in text
    assert "No report was generated" in text


def test_report_edgar_unreachable_shows_honest_error(monkeypatch):
    import urllib.error

    def fake_live(args):
        raise urllib.error.URLError("Name or service not known")

    monkeypatch.setattr(web, "live_pipeline", fake_live)
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
