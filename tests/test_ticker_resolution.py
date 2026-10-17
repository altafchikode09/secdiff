"""Offline tests for ticker resolution (edgar.resolve_ticker).

Mirrors the real XOM case found in Oct 2026: EDGAR's JSON ticker files map
the ticker to a related entity that files no 10-Ks, while EDGAR's classic
browse-edgar lookup resolves it to the operating company that does. All
network access is replaced by canned EDGAR-shaped data routed through the
client's own fetch methods.
"""
import pytest

from secdiff.edgar import EdgarClient


def subs(forms):
    """A submissions payload whose recent list contains exactly `forms`."""
    n = len(forms)
    return {"filings": {"recent": {
        "form": forms,
        "accessionNumber": [f"0000000000-26-{i:06d}" for i in range(n)],
        "filingDate": ["2026-01-01"] * n,
        "reportDate": ["2025-12-31"] * n,
        "primaryDocument": ["doc.htm"] * n,
    }}}


BROWSE_ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <company-info>
    <cik>0000000222</cik>
    <conformed-name>REAL OPERATING CO</conformed-name>
  </company-info>
</feed>"""


class FakeClient(EdgarClient):
    """EdgarClient with canned EDGAR responses; records what was fetched."""

    def __init__(self, json_routes, browse_atom=b""):
        super().__init__(email="test@example.com", cache_dir="/tmp/secdiff-test-x")
        self._routes = json_routes
        self._browse_atom = browse_atom
        self.fetched = []

    def _fetch_json(self, url, cache_name):
        self.fetched.append(cache_name)
        return self._routes[cache_name]

    def _fetch(self, url, cache_name):
        self.fetched.append(cache_name)
        return self._browse_atom


def xom_like_client():
    """Ticker files -> CIK 111 'Holdings' (no 10-K); browse -> CIK 222 (10-Ks)."""
    return FakeClient({
        "company_tickers.json": {
            "0": {"cik_str": 111, "ticker": "FOO", "title": "Foo Holdings Corp"}},
        "company_tickers_exchange.json": {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[111, "Foo Holdings Corp", "FOO", "NYSE"]]},
        "submissions_0000000111.json": subs(["8-K", "DEFA14A", "8-K"]),
        "submissions_0000000222.json": subs(["10-K", "8-K", "10-K", "10-K"]),
    }, browse_atom=BROWSE_ATOM.encode())


def test_skips_entity_that_files_no_10k_and_uses_browse_edgar():
    client = xom_like_client()
    cik, name = client.resolve_ticker("FOO", form="10-K", min_count=2)
    assert cik == 222
    assert name == "Real Operating Co"
    # the holdings entity, the exchange file and browse-edgar were all tried
    assert "company_tickers_exchange.json" in client.fetched
    assert "browse_ticker_FOO.xml" in client.fetched


def test_primary_candidate_accepted_without_fallbacks():
    client = FakeClient({
        "company_tickers.json": {
            "0": {"cik_str": 333, "ticker": "BAR", "title": "Bar Inc"}},
        "submissions_0000000333.json": subs(["10-K", "10-K"]),
    })
    cik, name = client.resolve_ticker("BAR", form="10-K", min_count=2)
    assert (cik, name) == (333, "Bar Inc")
    # no fallback sources are fetched when the primary candidate files 10-Ks
    assert "company_tickers_exchange.json" not in client.fetched
    assert not any(f.startswith("browse_ticker_") for f in client.fetched)


def test_exchange_file_candidate_used_before_browse():
    client = FakeClient({
        "company_tickers.json": {
            "0": {"cik_str": 111, "ticker": "BAZ", "title": "Baz Holdings"}},
        "company_tickers_exchange.json": {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[111, "Baz Holdings", "BAZ", "NYSE"],
                     [444, "Baz Operating Co", "BAZ", "NYSE"]]},
        "submissions_0000000111.json": subs(["8-K"]),
        "submissions_0000000444.json": subs(["10-Q", "10-Q", "10-Q"]),
    })
    cik, name = client.resolve_ticker("BAZ", form="10-Q", min_count=2)
    assert (cik, name) == (444, "Baz Operating Co")
    assert not any(f.startswith("browse_ticker_") for f in client.fetched)


def test_no_entity_files_form_fails_naming_what_was_found():
    client = xom_like_client()
    with pytest.raises(SystemExit) as exc:
        client.resolve_ticker("FOO", form="10-Q", min_count=2)
    msg = str(exc.value)
    assert "Foo Holdings Corp" in msg and "111" in msg
    assert "Real Operating Co" in msg and "222" in msg
    assert "10-Q" in msg


def test_dot_class_ticker_resolves_via_dash_variant():
    """BRK.B typed in market format must find EDGAR's BRK-B listing."""
    client = FakeClient({
        "company_tickers.json": {
            "0": {"cik_str": 555, "ticker": "BRK-B", "title": "Berkshire-like Inc"}},
        "company_tickers_exchange.json": {"fields": [], "data": []},
        "submissions_0000000555.json": subs(["10-K", "10-K", "10-K"]),
    }, browse_atom=b"<feed></feed>")
    cik, name = client.resolve_ticker("BRK.B", form="10-K", min_count=2)
    assert (cik, name) == (555, "Berkshire-like Inc")


def test_variants_still_require_form_filing_check():
    """A dash variant that files no 10-K must not win over a later variant
    whose entity actually files the form."""
    client = FakeClient({
        "company_tickers.json": {
            "0": {"cik_str": 111, "ticker": "BRK-B", "title": "Dash Holdings"},
            "1": {"cik_str": 666, "ticker": "BRK/B", "title": "Slash Operating Co"}},
        "company_tickers_exchange.json": {"fields": [], "data": []},
        "submissions_0000000111.json": subs(["8-K", "8-K"]),
        "submissions_0000000666.json": subs(["10-K", "10-K"]),
    }, browse_atom=b"<feed></feed>")
    cik, name = client.resolve_ticker("BRK.B", form="10-K", min_count=2)
    assert (cik, name) == (666, "Slash Operating Co")


def test_unknown_class_ticker_errors_with_hint():
    client = FakeClient({
        "company_tickers.json": {},
        "company_tickers_exchange.json": {"fields": [], "data": []},
    }, browse_atom=b"<feed></feed>")
    with pytest.raises(SystemExit) as exc:
        client.resolve_ticker("ZZ.Z")
    msg = str(exc.value)
    assert "not found" in msg.lower()
    assert "BRK-B" in msg  # class-share hint is included


def test_unknown_ticker_fails_as_not_found():
    client = FakeClient({
        "company_tickers.json": {},
        "company_tickers_exchange.json": {"fields": [], "data": []},
    }, browse_atom=b"<feed></feed>")
    with pytest.raises(SystemExit) as exc:
        client.resolve_ticker("NOPE")
    assert "not found" in str(exc.value).lower()
