"""The owner's 100-case suite for Phase 1 (and the behaviours it must
never regress). His rule: run 100 cases, fix the small bugs, run again,
fix the big bugs, run again — go live only at 100/100.

Every case is offline and deterministic: EDGAR is stubbed at the client
boundary, the web pipeline is stubbed at the same seam the other web
tests use, and XBRL cases run on crafted company-facts dicts. Case
numbers (C001–C100) are stable so cycle reports can cite them.

Categories:
  A  Resolution & filer sizes        C001–C012
  B  Forms coverage                  C013–C024
  C  XBRL edge cases                 C025–C040
  D  Compare modes                   C041–C052
  E  Report options & exports        C053–C065
  F  Web behaviour                   C066–C092
  G  Accounts & store robustness     C093–C100
"""
import csv
import io
import re

import pytest

import secdiff.web as web
from secdiff import cli
from secdiff.diffing import Hunk, SectionDiff
from secdiff.edgar import EdgarClient, Filing
from secdiff.export import table_rows, xbrl_to_csv, xbrl_to_xls
from secdiff.report import FilingInfo, render_html, render_markdown
from secdiff.sections import split_sections
from secdiff.web import application, parse_report_form
from secdiff.xbrl import DeltaRow, XbrlResult, compute_delta, fmt_money


# ---------------------------------------------------------------------------
# Harness: EDGAR stubbed at the client boundary

def _subs(rows):
    """Submissions 'recent' block from (form, accession, filed, report, doc)."""
    return {"filings": {"recent": {
        "form": [r[0] for r in rows],
        "accessionNumber": [r[1] for r in rows],
        "filingDate": [r[2] for r in rows],
        "reportDate": [r[3] for r in rows],
        "primaryDocument": [r[4] for r in rows]}}}


def _two(form, cik):
    return _subs([(form, f"000000{ cik }-25-000001", "2025-10-31",
                   "2025-09-30", "a.htm"),
                  (form, f"000000{cik}-24-000001", "2024-11-01",
                   "2024-09-30", "b.htm")])


def _client(tmp_path, ticker_rows, subs_by_cik, exchange_rows=()):
    client = EdgarClient(email="t@e.com", cache_dir=tmp_path)

    def fake_fetch_json(url, cache_name):
        if url.endswith("company_tickers_exchange.json"):
            return {"fields": ["cik", "name", "ticker", "exchange"],
                    "data": list(exchange_rows)}
        if url.endswith("company_tickers.json"):
            return {str(i): r for i, r in enumerate(ticker_rows)}
        raise AssertionError(url)

    client._fetch_json = fake_fetch_json
    client._fetch = lambda url, name: b"<feed></feed>"  # browse-edgar: no hit
    client._submissions = lambda cik: subs_by_cik[cik]
    return client


def _row(ticker, cik, title):
    return {"cik_str": cik, "ticker": ticker, "title": title}


# ---------------------------------------------------------------------------
# A. Resolution & filer sizes (C001–C012)

def test_c001_megacap_aapl_resolves(tmp_path):
    c = _client(tmp_path, [_row("AAPL", 320193, "Apple Inc.")],
                {320193: _two("10-K", 320193)})
    assert c.resolve_ticker("AAPL", form="10-K") == (320193, "Apple Inc.")


def test_c002_megacap_msft_resolves(tmp_path):
    c = _client(tmp_path, [_row("MSFT", 789019, "MICROSOFT CORP")],
                {789019: _two("10-K", 789019)})
    assert c.resolve_ticker("MSFT", form="10-K")[0] == 789019


def test_c003_dot_ticker_brk_b_resolves_via_dash(tmp_path):
    c = _client(tmp_path, [_row("BRK-B", 1067983, "Berkshire Hathaway Inc")],
                {1067983: _two("10-K", 1067983)})
    cik, _name = c.resolve_ticker("BRK.B", form="10-K")
    assert cik == 1067983


def test_c004_dash_ticker_brk_b_resolves_directly(tmp_path):
    c = _client(tmp_path, [_row("BRK-B", 1067983, "Berkshire Hathaway Inc")],
                {1067983: _two("10-K", 1067983)})
    assert c.resolve_ticker("BRK-B", form="10-K")[0] == 1067983


def test_c005_name_alphabet_resolves_to_goog(tmp_path):
    rows = [_row("GOOG", 1652044, "Alphabet Inc."),
            _row("GOOGL", 1652044, "Alphabet Inc.")]
    c = _client(tmp_path, rows, {1652044: _two("10-K", 1652044)})
    cik, _name = c.resolve_ticker("Alphabet", form="10-K")
    assert cik == 1652044
    assert c.name_matched_ticker == "GOOG"      # first ticker alphabetically


def test_c006_name_berkshire_hathaway_resolves(tmp_path):
    rows = [_row("BRK-A", 1067983, "Berkshire Hathaway Inc"),
            _row("BRK-B", 1067983, "Berkshire Hathaway Inc")]
    c = _client(tmp_path, rows, {1067983: _two("10-K", 1067983)})
    cik, _name = c.resolve_ticker("Berkshire Hathaway", form="10-K")
    assert cik == 1067983
    assert c.name_matched_ticker == "BRK-A"


def test_c007_ambiguous_name_lists_candidates(tmp_path):
    rows = [_row("DAL", 27904, "DELTA AIR LINES INC"),
            _row("DLA", 1101396, "Delta Apparel, Inc.")]
    c = _client(tmp_path, rows, {})
    with pytest.raises(SystemExit) as exc:
        c.resolve_ticker("Delta", form="10-K")
    assert "matches more than one listed company" in str(exc.value)
    assert "DAL" in str(exc.value) and "DLA" in str(exc.value)


def test_c008_unknown_ticker_honest_error(tmp_path):
    c = _client(tmp_path, [_row("AAPL", 320193, "Apple Inc.")], {})
    with pytest.raises(SystemExit) as exc:
        c.resolve_ticker("ZZZZZ", form="10-K")
    assert "Ticker not found on EDGAR: ZZZZZ" in str(exc.value)


def test_c009_unknown_company_name_honest_error(tmp_path):
    c = _client(tmp_path, [_row("AAPL", 320193, "Apple Inc.")], {})
    with pytest.raises(SystemExit) as exc:
        c.resolve_ticker("Nonexistent Holdings", form="10-K")
    msg = str(exc.value)
    assert "Ticker not found on EDGAR" in msg
    assert "subsidiar" in msg          # the YouTube-is-Alphabet hint


def test_c010_lowercase_ticker_resolves(tmp_path):
    c = _client(tmp_path, [_row("AAPL", 320193, "Apple Inc.")],
                {320193: _two("10-K", 320193)})
    assert c.resolve_ticker("aapl", form="10-K")[0] == 320193


def test_c011_xom_trap_wrong_entity_skipped(tmp_path):
    # Ticker file points at a related entity filing no 10-Ks; the
    # exchange file's entity actually files them and must win.
    c = _client(
        tmp_path,
        [_row("XOM", 2115436, "ExxonMobil Holdings Corp")],
        {2115436: _subs([("10-Q", "x", "2025-08-01", "2025-06-30", "q.htm")]),
         34088: _two("10-K", 34088)},
        exchange_rows=[[34088, "Exxon Mobil Corp", "XOM", "NYSE"]])
    cik, name = c.resolve_ticker("XOM", form="10-K")
    assert cik == 34088 and "Exxon Mobil" in name


def test_c012_tiny_filer_too_few_filings(tmp_path):
    c = _client(
        tmp_path, [_row("TINY", 555001, "Tiny Co")],
        {555001: _subs([("10-K", "t-25", "2025-05-01", "2024-12-31", "t.htm"),
                        ("10-Q", "t-q", "2025-08-01", "2025-06-30", "q.htm")])})
    with pytest.raises(SystemExit) as exc:
        c.resolve_ticker("TINY", form="10-K", min_count=2)
    assert "need at least 2" in str(exc.value)


# ---------------------------------------------------------------------------
# B. Forms coverage (C013–C024)

def _form_client(tmp_path):
    rows = [
        ("DEF 14A", "p-25", "2025-04-10", "2025-06-05", "proxy25.htm"),
        ("10-K", "k-25", "2025-10-31", "2025-09-30", "k25.htm"),
        ("8-K", "e-25b", "2025-05-02", "2025-05-01", "e2.htm"),
        ("8-K", "e-25a", "2025-03-03", "2025-03-01", "e1.htm"),
        ("DEF 14A", "p-24", "2024-04-11", "2024-06-06", "proxy24.htm"),
        ("8-K", "e-24", "2024-06-01", "2024-05-30", "e0.htm"),
        ("10-K", "k-24", "2024-11-01", "2024-09-30", "k24.htm"),
    ]
    c = EdgarClient(email="t@e.com", cache_dir=tmp_path)
    c._submissions = lambda cik: _subs(rows)
    return c


def test_c013_all_filings_def14a_listing(tmp_path):
    got = _form_client(tmp_path).all_filings(1, "DEF 14A")
    assert [f.accession for f in got] == ["p-25", "p-24"]   # newest first
    assert all(f.form == "DEF 14A" for f in got)


def test_c014_all_filings_8k_keeps_same_year_multiples(tmp_path):
    got = _form_client(tmp_path).all_filings(1, "8-K")
    assert [f.accession for f in got] == ["e-25b", "e-25a", "e-24"]


def test_c015_all_filings_count_cap(tmp_path):
    got = _form_client(tmp_path).all_filings(1, "8-K", count=2)
    assert [f.accession for f in got] == ["e-25b", "e-25a"]


def test_c016_recent_filings_shortfall_raises(tmp_path):
    c = _form_client(tmp_path)
    with pytest.raises(SystemExit) as exc:
        c.recent_filings(1, "DEF 14A", count=3)
    assert "Only 2 DEF 14A" in str(exc.value)


def test_c017_filing_year_property():
    f = Filing(form="10-K", accession="a", filing_date="2025-10-31",
               report_date="", primary_document="x.htm")
    assert f.filing_year == 2025
    bad = Filing(form="10-K", accession="a", filing_date="",
                 report_date="", primary_document="x.htm")
    assert bad.filing_year is None


_EIGHT_K_TEXT = """Item 1.01 Entry into a Material Definitive Agreement
On October 1, 2026, the company entered into a new revolving credit
agreement with several lenders, replacing the prior facility in full.

Item 2.02 Results of Operations and Financial Condition
On October 30, 2026, the company issued a press release announcing its
financial results for the quarter ended September 30, 2026.

Item 9.01 Financial Statements and Exhibits
The following exhibits are furnished as part of this current report on
Form 8-K and are listed here for reference by all readers today.
"""


def test_c018_split_8k_decimal_items_and_titles():
    split = split_sections(_EIGHT_K_TEXT, "8-K")
    assert set(split.sections) == {"1.01", "2.02", "9.01"}
    assert split.sections["2.02"].title == \
        "Results of Operations and Financial Condition"
    assert split.sections["1.01"].title.startswith("Entry into a Material")


def test_c019_split_8k_nothing_is_missing():
    split = split_sections(_EIGHT_K_TEXT, "8-K")
    assert split.missing == []


def test_c020_split_def14a_uses_proposal_headings():
    text = """Item 1. Election of Directors
The board of directors recommends that shareholders vote for the
election of each of the named director nominees listed in this proxy
statement for the coming year at the annual meeting of shareholders.

Item 2. Ratification of Independent Auditors
The audit committee has appointed the independent registered public
accounting firm named here to audit the consolidated financial
statements of the company for the fiscal year ending this December.
"""
    split = split_sections(text, "DEF 14A")
    assert "1" in split.sections and "2" in split.sections
    assert split.sections["1"].title == "Election Of Directors"  # NOT "Business"
    assert split.missing == []


def _padded(sentence, n=6):
    return " ".join([sentence] * n)


def test_c021_split_10k_core_items_high_confidence():
    parts = []
    for item, title in (("1", "Business"), ("1A", "Risk Factors"),
                        ("7", "Management's Discussion and Analysis"),
                        ("8", "Financial Statements")):
        parts.append(f"ITEM {item}. {title}\n" + _padded(
            f"The company describes its {title.lower()} in considerable "
            "detail for the fiscal year covered by this annual report."))
    split = split_sections("\n\n".join(parts), "10-K")
    assert set(split.sections) >= {"1", "1A", "7", "8"}
    assert split.missing == [] and split.confidence == "high"


def _info(form="10-K", new_date="2025-10-31", old_date="2024-11-01",
          ticker="EXMP", cik=12345):
    return FilingInfo(
        ticker=ticker, company="Example Corp", form=form, cik=cik,
        new_accession="0000012345-25-000111", new_date=new_date,
        new_url="https://www.sec.gov/new",
        new_index_url="https://www.sec.gov/new-idx",
        old_accession="0000012345-24-000111", old_date=old_date,
        old_url="https://www.sec.gov/old",
        old_index_url="https://www.sec.gov/old-idx",
        section_confidence="high")


_EMPTY_XBRL = XbrlResult(rows=[], unmapped=[], matched=0, considered=100)


def test_c022_report_def14a_note_and_no_numbers_table():
    html_out = render_html(_info(form="DEF 14A"), [], _EMPTY_XBRL)
    assert "Proxy statements (DEF 14A) are filed once a year" in html_out
    assert "no numbers table for this report" in html_out


def test_c023_report_8k_event_note_markdown():
    md = render_markdown(_info(form="8-K"), [], _EMPTY_XBRL)
    assert "8-Ks are event filings" in md
    assert "DIFFERENT events" in md


def test_c024_report_10k_has_no_form_note():
    html_out = render_html(_info(form="10-K"), [], _EMPTY_XBRL)
    assert "About this form" not in html_out


# ---------------------------------------------------------------------------
# C. XBRL edge cases (C025–C040)

OLD_ACC, NEW_ACC = "0000000001-24-000001", "0000000001-25-000001"


def _e(val, accn, start="2023-10-01", end="2024-09-30", fp="FY"):
    return {"start": start, "end": end, "val": val, "accn": accn, "fp": fp}


def _facts(alias_units, taxonomy="us-gaap"):
    return {"facts": {taxonomy:
                      {a: {"units": u} for a, u in alias_units.items()}}}


def _both(alias, unit, old_v, new_v):
    return _facts({alias: {unit: [_e(old_v, OLD_ACC), _e(new_v, NEW_ACC)]}})


def _row_for(result, label):
    rows = [r for r in result.rows if r.label == label]
    assert len(rows) == 1, f"expected one row for {label}: {result.rows}"
    return rows[0]


def test_c025_xbrl_trillions():
    res = compute_delta(_both("Revenues", "USD", 3.5e12, 3.91e12),
                        OLD_ACC, NEW_ACC)
    r = _row_for(res, "Revenue")
    assert r.old == 3.5e12 and r.new == 3.91e12
    assert fmt_money(r.old, r.unit) == "$3,500.00B"
    assert fmt_money(r.new, r.unit) == "$3,910.00B"


def test_c026_xbrl_negative_values():
    res = compute_delta(_both("NetIncomeLoss", "USD", -5e9, 3e9),
                        OLD_ACC, NEW_ACC)
    r = _row_for(res, "Net income")
    assert r.change == 8e9
    assert r.pct == pytest.approx(160.0)
    assert fmt_money(r.old, r.unit) == "-$5.00B"


def test_c027_xbrl_zero_old_value_pct_unknown():
    res = compute_delta(_both("Revenues", "USD", 0, 100.0), OLD_ACC, NEW_ACC)
    r = _row_for(res, "Revenue")
    assert r.old == 0 and r.pct is None          # never a fake percentage


def test_c028_xbrl_zero_new_value():
    res = compute_delta(_both("Revenues", "USD", 100.0, 0), OLD_ACC, NEW_ACC)
    r = _row_for(res, "Revenue")
    assert r.change == -100.0 and r.pct == pytest.approx(-100.0)


def test_c029_xbrl_per_share_decimals():
    res = compute_delta(_both("EarningsPerShareDiluted", "USD/shares",
                              6.03, 6.11), OLD_ACC, NEW_ACC)
    r = _row_for(res, "EPS (diluted)")
    assert r.unit == "USD/shares"
    assert fmt_money(r.old, r.unit) == "$6.03"
    assert fmt_money(r.new, r.unit) == "$6.11"


def test_c030_xbrl_pure_ratio_as_percent():
    res = compute_delta(
        _both("EffectiveIncomeTaxRateContinuingOperations", "pure",
              0.21, 0.24), OLD_ACC, NEW_ACC)
    r = _row_for(res, "Effective tax rate")
    assert fmt_money(r.old, r.unit) == "21.0%"
    assert fmt_money(r.new, r.unit) == "24.0%"


def test_c031_xbrl_shares_unit():
    res = compute_delta(
        _both("WeightedAverageNumberOfSharesOutstandingBasic", "shares",
              15.1e9, 14.8e9), OLD_ACC, NEW_ACC)
    r = _row_for(res, "Weighted avg shares (basic)")
    assert r.unit == "shares"
    assert fmt_money(r.old, r.unit) == "15,100,000,000"


def test_c032_xbrl_concept_only_in_newer_is_unmapped():
    res = compute_delta(_facts({"Revenues": {"USD": [_e(5.0, NEW_ACC)]}}),
                        OLD_ACC, NEW_ACC)
    assert res.rows == []
    assert any("Revenue" in u and "newer filing only" in u
               for u in res.unmapped)


def test_c033_xbrl_concept_only_in_older_is_unmapped():
    res = compute_delta(_facts({"Revenues": {"USD": [_e(5.0, OLD_ACC)]}}),
                        OLD_ACC, NEW_ACC)
    assert res.rows == []
    assert any("Revenue" in u and "older filing only" in u
               for u in res.unmapped)


def test_c034_xbrl_nothing_reported():
    res = compute_delta({"facts": {}}, OLD_ACC, NEW_ACC)
    assert res.rows == [] and res.unmapped == []
    assert res.matched == 0 and res.considered == 100


def test_c035_xbrl_retag_is_never_merged():
    # Old filing tags revenue "Revenues"; new filing retagged it
    # "SalesRevenueNet". Same concept, different alias per side: the
    # matcher must list it, never silently merge the two numbers.
    facts = _facts({
        "Revenues": {"USD": [_e(100.0, OLD_ACC)]},
        "SalesRevenueNet": {"USD": [_e(150.0, NEW_ACC)]}})
    res = compute_delta(facts, OLD_ACC, NEW_ACC)
    assert res.rows == []
    assert any("Revenue" in u for u in res.unmapped)


def test_c036_xbrl_alias_in_both_filings_wins():
    facts = _facts({
        "Revenues": {"USD": [_e(100.0, OLD_ACC), _e(150.0, NEW_ACC)]},
        "SalesRevenueNet": {"USD": [_e(999.0, OLD_ACC), _e(888.0, NEW_ACC)]}})
    res = compute_delta(facts, OLD_ACC, NEW_ACC)
    r = _row_for(res, "Revenue")
    assert r.old == 100.0 and r.new == 150.0    # NOT the 999/888 alias


def test_c037_xbrl_ambiguous_values_refused():
    facts = _facts({"Revenues": {"USD": [
        _e(100.0, OLD_ACC), _e(200.0, OLD_ACC),     # two different FY values
        _e(150.0, NEW_ACC)]}})
    res = compute_delta(facts, OLD_ACC, NEW_ACC)
    assert res.rows == []
    assert any("not safely matchable" in u for u in res.unmapped)


def test_c038_xbrl_quarterly_only_facts_not_called_annual():
    q = ("2024-07-01", "2024-09-30")
    facts = _facts({"Revenues": {"USD": [
        _e(25.0, OLD_ACC, start=q[0], end=q[1], fp="Q3"),
        _e(30.0, NEW_ACC, start=q[0], end=q[1], fp="Q3")]}})
    res = compute_delta(facts, OLD_ACC, NEW_ACC)
    assert res.rows == []                        # a quarter is not a year
    assert any("not safely matchable" in u for u in res.unmapped)


def test_c039_xbrl_preferred_unit_wins():
    facts = _facts({"Revenues": {
        "EUR": [_e(90.0, OLD_ACC), _e(95.0, NEW_ACC)],
        "USD": [_e(100.0, OLD_ACC), _e(150.0, NEW_ACC)]}})
    res = compute_delta(facts, OLD_ACC, NEW_ACC)
    r = _row_for(res, "Revenue")
    assert r.unit == "USD" and r.old == 100.0


def test_c040_fmt_money_boundaries():
    assert fmt_money(None) == "—"
    assert fmt_money(999) == "$999"
    assert fmt_money(1500) == "$1.5K"
    assert fmt_money(2_500_000) == "$2.5M"
    assert fmt_money(3_250_000_000) == "$3.25B"
    assert fmt_money(-1_500_000) == "-$1.5M"
    assert fmt_money(1_234_567, "shares") == "1,234,567"
    assert fmt_money(6.1, "USD/shares") == "$6.10"
    assert fmt_money(0.215, "pure") == "21.5%"


# ---------------------------------------------------------------------------
# D. Compare modes (C041–C052)

_K = [("10-K", "K25", "2025-10-31", "2025-09-30", "k25.htm"),
      ("10-K", "K24", "2024-11-01", "2024-09-30", "k24.htm"),
      ("10-K", "K23", "2023-10-27", "2023-09-30", "k23.htm"),
      ("10-K", "K22", "2022-10-28", "2022-09-30", "k22.htm")]
_P = [("DEF 14A", "P25", "2025-04-10", "2025-06-05", "p25.htm"),
      ("DEF 14A", "P24", "2024-04-11", "2024-06-06", "p24.htm"),
      ("DEF 14A", "P23", "2023-04-12", "2023-06-07", "p23.htm")]
_E = [("8-K", "E25B", "2025-05-02", "2025-05-01", "e25b.htm"),
      ("8-K", "E25A", "2025-03-03", "2025-03-01", "e25a.htm"),
      ("8-K", "E24", "2024-06-01", "2024-05-30", "e24.htm")]
_ALL = _K + _P + _E


def _filing(t):
    return Filing(form=t[0], accession=t[1], filing_date=t[2],
                  report_date=t[3], primary_document=t[4])


class _FakeClient:
    name_matched_ticker = None

    def __init__(self, email=None, cache_dir=None):
        pass

    def resolve_ticker(self, ticker, form="10-K", min_count=2):
        return 999, "Fake Co"

    def all_filings(self, cik, form):
        return [_filing(t) for t in _ALL if t[0] == form]

    def recent_filings(self, cik, form, count=2):
        got = self.all_filings(cik, form)
        if len(got) < count:
            raise SystemExit(f"Only {len(got)} {form} filing(s)")
        return got[:count]

    def filing_text(self, cik, filing):
        return (f"The company reports its results for the period in this "
                f"filing {filing.accession} with full detail included.")

    def company_facts(self, cik):
        return {"facts": {}}


@pytest.fixture()
def fake_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "EdgarClient", _FakeClient)

    def run(form="10-K", years=1, vs_year=None, base_year=None):
        from types import SimpleNamespace
        args = SimpleNamespace(
            ticker="FAKE", form=form, years=years, vs_year=vs_year,
            base_year=base_year, email="t@e.com",
            cache_dir=str(tmp_path))
        return cli.live_pipeline(args)

    return run


def test_c041_default_latest_vs_prior(fake_cli):
    info, _d, _x = fake_cli()
    assert (info.new_accession, info.old_accession) == ("K25", "K24")


def test_c042_years_back(fake_cli):
    info, _d, _x = fake_cli(years=2)
    assert (info.new_accession, info.old_accession) == ("K25", "K23")


def test_c043_vs_and_base_year_pair(fake_cli):
    info, _d, _x = fake_cli(vs_year=2024, base_year=2022)
    assert (info.new_accession, info.old_accession) == ("K24", "K22")
    assert (info.new_date, info.old_date) == ("2024-11-01", "2022-10-28")


def test_c044_base_year_only(fake_cli):
    info, _d, _x = fake_cli(base_year=2022)
    assert (info.new_accession, info.old_accession) == ("K25", "K22")


def test_c045_vs_year_only_compares_with_next_older(fake_cli):
    info, _d, _x = fake_cli(vs_year=2023)
    assert (info.new_accession, info.old_accession) == ("K23", "K22")


def test_c046_same_year_is_a_mistake(fake_cli):
    with pytest.raises(SystemExit) as exc:
        fake_cli(vs_year=2024, base_year=2024)
    assert "cannot be compared with itself" in str(exc.value)


def test_c047_reversed_years_rejected(fake_cli):
    with pytest.raises(SystemExit) as exc:
        fake_cli(vs_year=2022, base_year=2025)
    assert "not older" in str(exc.value)


def test_c048_unknown_vs_year_lists_available(fake_cli):
    with pytest.raises(SystemExit) as exc:
        fake_cli(vs_year=1999)
    msg = str(exc.value)
    assert "No 10-K filing from 1999" in msg
    assert "2025" in msg and "2022" in msg        # the years that DO exist


def test_c049_unknown_base_year_lists_available(fake_cli):
    with pytest.raises(SystemExit) as exc:
        fake_cli(base_year=1999)
    assert "Filing years available" in str(exc.value)


def test_c050_oldest_filing_has_no_earlier_partner(fake_cli):
    with pytest.raises(SystemExit) as exc:
        fake_cli(vs_year=2022)
    assert "no earlier filing" in str(exc.value)


def test_c051_def14a_year_pair(fake_cli):
    info, _d, _x = fake_cli(form="DEF 14A", vs_year=2025, base_year=2023)
    assert info.form == "DEF 14A"
    assert (info.new_accession, info.old_accession) == ("P25", "P23")


def test_c052_8k_default_pair_same_year_allowed(fake_cli):
    info, _d, _x = fake_cli(form="8-K")
    assert (info.new_accession, info.old_accession) == ("E25B", "E25A")
    assert info.new_date != info.old_date


# ---------------------------------------------------------------------------
# E. Report options & exports (C053–C065)

from types import SimpleNamespace

from secdiff.cli import finish, one_line_summary, render_reports


def _diffs():
    return [
        SectionDiff(item="1A", title="Risk Factors", hunks=[
            Hunk(kind="deletion", old="A very large removed risk sentence.",
                 size=500),
            Hunk(kind="addition", new="A small added note.", size=10)]),
        SectionDiff(item="7", title="MD&A", hunks=[
            Hunk(kind="modification", old="Old mdna sentence here.",
                 new="New mdna sentence here.", size=50)]),
        SectionDiff(item="1", title="Business", hunks=[
            Hunk(kind="addition", new="A brand new business line.",
                 size=20)]),
    ]


def _rargs(**kw):
    base = dict(risk_only=False, deletions_only=False, top=None,
                section=None, form="10-K", summarize=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_c053_risk_only_keeps_item_1a():
    md, _ht, counts = render_reports(_rargs(risk_only=True), _info(),
                                     _diffs(), _EMPTY_XBRL)
    assert counts == (1, 0, 1)
    assert "Risk Factors" in md and "MD&A" not in md


def test_c054_deletions_only():
    _md, _ht, counts = render_reports(_rargs(deletions_only=True), _info(),
                                      _diffs(), _EMPTY_XBRL)
    assert counts == (1, 0, 0)


def test_c055_top_one_keeps_biggest_change():
    _md, _ht, counts = render_reports(_rargs(top=1), _info(),
                                      _diffs(), _EMPTY_XBRL)
    assert counts == (1, 0, 0)          # the size-500 deletion wins


def test_c056_section_by_name_mdna():
    _md, _ht, counts = render_reports(_rargs(section="mdna"), _info(),
                                      _diffs(), _EMPTY_XBRL)
    assert counts == (0, 1, 0)


def test_c057_section_by_item_number():
    _md, _ht, counts = render_reports(_rargs(section="1A"), _info(),
                                      _diffs(), _EMPTY_XBRL)
    assert counts == (1, 0, 1)


def test_c058_unknown_section_is_an_error():
    with pytest.raises(SystemExit):
        render_reports(_rargs(section="bogus"), _info(), _diffs(),
                       _EMPTY_XBRL)


_XBRL3 = XbrlResult(rows=[
    DeltaRow("Revenue", "Revenues", "USD", 3.5e12, 3.91e12, 4.1e11,
             11.714285714285714, "Income statement"),
    DeltaRow("Net income", "NetIncomeLoss", "USD", -5e9, 3e9, 8e9,
             160.0, "Income statement"),
    DeltaRow("Effective tax rate",
             "EffectiveIncomeTaxRateContinuingOperations", "pure",
             0.21, 0.24, 0.03, None, "Income statement"),
], unmapped=[], matched=3, considered=100)


def test_c059_csv_export_matches_table_exactly():
    parsed = list(csv.reader(io.StringIO(xbrl_to_csv(_XBRL3))))
    assert parsed == table_rows(_XBRL3)
    flat = [c for row in parsed for c in row]
    assert "$3,500.00B" in flat and "-$5.00B" in flat and "—" in flat


def test_c060_xls_export_matches_table_exactly():
    cells = re.findall(r'<Data ss:Type="String">(.*?)</Data>',
                       xbrl_to_xls(_XBRL3))
    assert cells == [c for row in table_rows(_XBRL3) for c in row]


def test_c061_csv_empty_table_is_header_only():
    assert list(csv.reader(io.StringIO(xbrl_to_csv(_EMPTY_XBRL)))) == \
        [["Item", "Older filing", "Newer filing", "Change", "%"]]


def test_c062_xls_empty_table_is_header_only():
    assert xbrl_to_xls(_EMPTY_XBRL).count("<Row>") == 1


def test_c063_cli_finish_writes_export_csv(tmp_path):
    csv_path = tmp_path / "numbers.csv"
    args = _rargs(export_csv=str(csv_path))
    assert finish(args, _info(), _diffs(), _XBRL3, tmp_path / "out") == 0
    assert csv_path.read_text("utf-8") == xbrl_to_csv(_XBRL3)
    assert (tmp_path / "out" / "secdiff-exmp-10-k.html").exists()
    assert (tmp_path / "out" / "secdiff-exmp-10-k.md").exists()


def test_c064_one_line_summary_format():
    assert one_line_summary(_info(), (1, 2, 3), _XBRL3) == (
        "EXMP 10-K: 1 deletions, 2 modifications, 3 additions, "
        "3 XBRL numbers changed (2024-11-01 -> 2025-10-31)")


def test_c065_markdown_links_both_filings():
    md = render_markdown(_info(), _diffs(), _XBRL3)
    assert "[0000012345-25-000111](https://www.sec.gov/new-idx)" in md
    assert "[document](https://www.sec.gov/old)" in md
    assert "3 XBRL numbers changed" in md


# ---------------------------------------------------------------------------
# F. Web behaviour (C066–C092)

def _call(method, path, query="", body=b"", cookie=""):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path,
               "QUERY_STRING": query, "CONTENT_LENGTH": str(len(body)),
               "HTTP_COOKIE": cookie, "wsgi.input": io.BytesIO(body)}
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = headers

    chunks = application(environ, start_response)
    return captured["status"], captured["headers"], \
        b"".join(chunks).decode("utf-8")


_ROW1 = DeltaRow("Revenue", "Revenues", "USD", 100.0, 150.0, 50.0, 50.0,
                 "Income statement")


def _result(ticker, form="10-K", rows=(), new_date="2025-10-31",
            old_date="2024-11-01"):
    info = _info(form=form, ticker=ticker, new_date=new_date,
                 old_date=old_date)
    diffs = [SectionDiff(item="1A", title="Risk Factors", hunks=[
        Hunk(kind="deletion", old="A removed sentence of some length.",
             size=9)])]
    x = XbrlResult(rows=list(rows), unmapped=[], matched=len(rows),
                   considered=100)
    return info, diffs, x


def _stub(monkeypatch, tmp_path, resolve_map, results):
    store = web.UsageStore(tmp_path / "usage.json")
    monkeypatch.setattr(web, "_STORE", store)

    def fake_resolve(params):
        hit = resolve_map[params.ticker]
        if isinstance(hit, BaseException):
            raise hit
        return hit, "Fake Co"

    monkeypatch.setattr(web, "_resolve_company", fake_resolve)
    captured = []

    def fake_live(args):
        captured.append(args)
        return results[args.ticker.upper()]

    monkeypatch.setattr(web, "live_pipeline", fake_live)
    return store, captured


_VID = "secdiff_vid=" + "cd" * 16


def test_c066_home_form_has_all_forms_and_year_inputs():
    status, _h, text = _call("GET", "/")
    assert status == "200 OK"
    for frag in ('value="10-K"', 'value="10-Q"', 'value="8-K"',
                 'value="DEF 14A"', 'name="vs_year"', 'name="base_year"',
                 "Proxy — DEF 14A", "8-K (current / event report)"):
        assert frag in text, frag


def test_c067_demo_path_is_404():
    status, _h, _t = _call("GET", "/demo")
    assert status.startswith("404")


def test_c068_unknown_path_is_404():
    status, _h, text = _call("GET", "/nope")
    assert status.startswith("404") and "Page not found" in text


def test_c069_malformed_ticker_is_400():
    status, _h, _t = _call("POST", "/report", body=b"ticker=AAPL%3Cscript%3E")
    assert status.startswith("400")


def test_c070_unsupported_form_is_400():
    status, _h, text = _call("POST", "/report", body=b"ticker=AAPL&form=S-1")
    assert status.startswith("400")
    assert "DEF 14A" in text            # the error names the real choices


def test_c071_top_not_a_number_is_400():
    status, _h, _t = _call("POST", "/report", body=b"ticker=AAPL&top=abc")
    assert status.startswith("400")


def test_c072_top_zero_is_400():
    status, _h, _t = _call("POST", "/report", body=b"ticker=AAPL&top=0")
    assert status.startswith("400")


def test_c073_vs_year_not_a_year_is_400():
    status, _h, text = _call("POST", "/report",
                             body=b"ticker=AAPL&vs_year=abc")
    assert status.startswith("400") and "must be a year" in text


def test_c074_vs_year_out_of_range_is_400():
    status, _h, _t = _call("POST", "/report",
                           body=b"ticker=AAPL&vs_year=1800")
    assert status.startswith("400")


def test_c075_default_report_passes_no_years(monkeypatch, tmp_path):
    _store, captured = _stub(monkeypatch, tmp_path, {"EXMP": 12345},
                              {"EXMP": _result("EXMP", rows=[_ROW1])})
    status, _h, _t = _call("POST", "/report", body=b"ticker=EXMP")
    assert status == "200 OK"
    assert captured[0].vs_year is None and captured[0].base_year is None


def test_c076_years_reach_the_pipeline(monkeypatch, tmp_path):
    _store, captured = _stub(monkeypatch, tmp_path, {"EXMP": 12345},
                              {"EXMP": _result("EXMP", rows=[_ROW1])})
    status, _h, _t = _call(
        "POST", "/report",
        body=b"ticker=EXMP&vs_year=2025&base_year=2022")
    assert status == "200 OK"
    assert (captured[0].vs_year, captured[0].base_year) == (2025, 2022)


def test_c077_proxy_report_page_notes_and_no_downloads(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result("EXMP", form="DEF 14A", rows=())})
    status, _h, text = _call("POST", "/report",
                             body=b"ticker=EXMP&form=DEF+14A")
    assert status == "200 OK"
    assert "Proxy statements (DEF 14A) are filed once a year" in text
    assert "no numbers table for this report" in text
    assert "/report.csv" not in text and "/report.xls" not in text


def test_c078_download_links_pin_the_compared_years(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result("EXMP", rows=[_ROW1])})
    status, _h, text = _call("POST", "/report", body=b"ticker=EXMP")
    assert status == "200 OK"
    assert "/report.csv?ticker=EXMP" in text
    assert "vs_year=2025" in text and "base_year=2024" in text
    assert "/report.xls?ticker=EXMP" in text


def test_c079_same_year_pair_download_link_unpinned_but_works(
        monkeypatch, tmp_path):
    res = _result("EXMP", form="8-K", rows=[_ROW1],
                  new_date="2025-05-02", old_date="2025-03-03")
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": res})
    status, _h, text = _call("POST", "/report",
                             body=b"ticker=EXMP&form=8-K")
    assert status == "200 OK"
    m = re.search(r"/report\.csv\?([^'\"]+)", text)
    assert m and "vs_year" not in m.group(1)     # a same-year pin would 400
    status, _h, _t = _call("GET", "/report.csv", query=m.group(1))
    assert status == "200 OK"


def test_c080_csv_download_route(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result("EXMP", rows=[_ROW1])})
    status, headers, text = _call("GET", "/report.csv",
                                  query="ticker=EXMP&form=10-K")
    assert status == "200 OK"
    hdrs = dict(headers)
    assert hdrs["Content-Type"].startswith("text/csv")
    assert "secdiff-exmp-10-k-numbers.csv" in hdrs["Content-Disposition"]
    assert list(csv.reader(io.StringIO(text))) == \
        table_rows(_result("EXMP", rows=[_ROW1])[2])


def test_c081_xls_download_route(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result("EXMP", rows=[_ROW1])})
    status, headers, text = _call("GET", "/report.xls",
                                  query="ticker=EXMP&form=10-K")
    assert status == "200 OK"
    assert "vnd.ms-excel" in dict(headers)["Content-Type"]
    assert '<Data ss:Type="String">Revenue</Data>' in text
    assert '<Data ss:Type="String">$150</Data>' in text


def test_c082_downloads_count_toward_free_limit(monkeypatch, tmp_path):
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    resolve_map = {t: 3000 + i for i, t in enumerate(tickers)}
    results = {t: _result(t, rows=[_ROW1]) for t in tickers}
    _stub(monkeypatch, tmp_path, resolve_map, results)
    for t in tickers[:4]:
        status, _h, _x = _call("GET", "/report.csv",
                               query=f"ticker={t}", cookie=_VID)
        assert status == "200 OK", t
    status, _h, text = _call("GET", "/report.csv", query="ticker=EEE",
                             cookie=_VID)
    assert status.startswith("402") and "$27" in text


def test_c083_download_unknown_ticker_no_slot_used(monkeypatch, tmp_path):
    store, _cap = _stub(
        monkeypatch, tmp_path,
        {"ZZZZZ": SystemExit("Ticker not found on EDGAR: ZZZZZ")}, {})
    status, _h, text = _call("GET", "/report.csv", query="ticker=ZZZZZ")
    assert status.startswith("400")
    assert "Ticker not found" in text
    assert store._load() == {}


def test_c084_fifth_distinct_company_paywall(monkeypatch, tmp_path):
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    resolve_map = {t: 4000 + i for i, t in enumerate(tickers)}
    results = {t: _result(t) for t in tickers}
    _stub(monkeypatch, tmp_path, resolve_map, results)
    for t in tickers[:4]:
        status, _h, _x = _call("POST", "/report",
                               body=f"ticker={t}".encode(), cookie=_VID)
        assert status == "200 OK", t
    status, _h, text = _call("POST", "/report", body=b"ticker=EEE",
                             cookie=_VID)
    assert status.startswith("402")
    assert "$9" in text and "$27" in text


def test_c085_same_company_review_is_free(monkeypatch, tmp_path):
    store, _cap = _stub(monkeypatch, tmp_path, {"EXMP": 12345},
                         {"EXMP": _result("EXMP")})
    for _ in range(6):
        status, _h, _t = _call("POST", "/report", body=b"ticker=EXMP",
                               cookie=_VID)
        assert status == "200 OK"
    assert store.companies_today(_VID.split("=", 1)[1]) == [12345]


def _session_cookie(headers):
    for k, v in headers:
        if k == "Set-Cookie" and v.startswith("secdiff_session="):
            return v.split(";")[0].split("=", 1)[1]
    return None


def _signup(username, email):
    body = (f"name=X&email={email}&username={username}"
            f"&password=password123").encode()
    return _call("POST", "/signup", body=body)


@pytest.fixture()
def acct(monkeypatch, tmp_path):
    store = web.AccountStore(tmp_path / "secdiff.db")
    usage = web.UsageStore(tmp_path / "u.json")
    monkeypatch.setattr(web, "_ACCOUNTS", store)
    monkeypatch.setattr(web, "_STATS", web.StatsStore(tmp_path / "secdiff.db"))
    monkeypatch.setattr(web, "_STORE", usage)
    monkeypatch.setenv("STATS_USERNAME", "owner1")
    return store, usage


def test_c086_logged_in_usage_is_account_keyed(acct, monkeypatch, tmp_path):
    _accounts, usage = acct

    def fake_resolve(params):
        return 12345, "Fake Co"

    monkeypatch.setattr(web, "_resolve_company", fake_resolve)
    monkeypatch.setattr(web, "live_pipeline",
                        lambda args: _result(args.ticker.upper()))
    _s, headers, _t = _signup("caseuser", "case%40example.com")
    token = _session_cookie(headers)
    assert token
    status, _h, _t = _call("POST", "/report", body=b"ticker=EXMP",
                           cookie=f"secdiff_session={token}")
    assert status == "200 OK"
    assert list(usage._load()) == ["user:caseuser"]   # NOT the guest cookie


def test_c087_stats_404_for_anonymous(acct):
    status, _h, _t = _call("GET", "/stats")
    assert status.startswith("404")


def test_c088_stats_404_for_non_owner(acct):
    _s, headers, _t = _signup("someoneelse", "other%40example.com")
    token = _session_cookie(headers)
    status, _h, text = _call("GET", "/stats",
                             cookie=f"secdiff_session={token}")
    assert status.startswith("404") and "Site stats" not in text


def test_c089_stats_visible_to_owner(acct):
    _s, headers, _t = _signup("owner1", "owner%40example.com")
    token = _session_cookie(headers)
    status, _h, text = _call("GET", "/stats",
                             cookie=f"secdiff_session={token}")
    assert status == "200 OK" and "Site stats" in text


def test_c090_stats_404_when_env_unset(acct, monkeypatch):
    monkeypatch.delenv("STATS_USERNAME")
    _s, headers, _t = _signup("owner1", "owner%40example.com")
    token = _session_cookie(headers)
    status, _h, _t = _call("GET", "/stats",
                           cookie=f"secdiff_session={token}")
    assert status.startswith("404")


def test_c091_post_to_download_route_is_405(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result("EXMP", rows=[_ROW1])})
    status, _h, _t = _call("POST", "/report.csv", body=b"ticker=EXMP")
    assert status.startswith("405")


def test_c092_get_report_via_query_string(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result("EXMP", rows=[_ROW1])})
    status, _h, text = _call("GET", "/report",
                             query="ticker=EXMP&form=10-K")
    assert status == "200 OK"
    assert "0000012345-25-000111" in text


# ---------------------------------------------------------------------------
# G. Accounts & store robustness (C093–C100)

def test_c093_signup_logs_you_in(acct):
    status, headers, _t = _signup("caseuser", "case%40example.com")
    assert status.startswith("303")
    token = _session_cookie(headers)
    assert token
    status, _h, text = _call("GET", "/",
                             cookie=f"secdiff_session={token}")
    assert status == "200 OK"
    assert "Hi, <strong>caseuser</strong>" in text


def test_c094_duplicate_username_rejected(acct):
    _signup("caseuser", "case%40example.com")
    status, _h, text = _signup("caseuser", "other%40example.com")
    assert status.startswith("400")
    assert "already taken" in text


def test_c095_wrong_password_generic_401(acct):
    _signup("caseuser", "case%40example.com")
    status, _h, text = _call(
        "POST", "/login",
        body=b"identifier=caseuser&password=wrongpass1")
    assert status.startswith("401")
    assert "don't match our records" in text


def test_c096_usage_store_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "u.json"
    path.write_text("{{{ not json", "utf-8")
    store = web.UsageStore(path)
    assert store.check_and_add("v1", 7, day="2026-10-18") is True
    assert store.companies_today("v1", day="2026-10-18") == [7]


def test_c097_usage_store_day_rollover(tmp_path):
    store = web.UsageStore(tmp_path / "u.json")
    for cik in (1, 2, 3, 4):
        assert store.check_and_add("v1", cik, day="2026-10-18") is True
    assert store.check_and_add("v1", 5, day="2026-10-18") is False
    assert store.check_and_add("v1", 5, day="2026-10-19") is True


def test_c098_stats_visitor_counted_once_per_day(tmp_path):
    store = web.StatsStore(tmp_path / "secdiff.db")
    for _ in range(3):
        store.record_visit("visitor-x", day="2026-10-18")
    store.record_visit("visitor-x", day="2026-10-19")   # new day: counts
    assert store.totals()["visits"] == 2


def test_c099_edgar_unreachable_is_502(monkeypatch, tmp_path):
    import urllib.error

    def fake_live(args):
        raise urllib.error.URLError("Name or service not known")

    monkeypatch.setattr(web, "live_pipeline", fake_live)
    monkeypatch.setattr(web, "_resolve_company",
                        lambda params: (320193, "Apple Inc."))
    monkeypatch.setattr(web, "_STORE", web.UsageStore(tmp_path / "u.json"))
    status, _h, text = _call("POST", "/report", body=b"ticker=AAPL")
    assert status.startswith("502")
    assert "SEC EDGAR could not be reached" in text


def test_c100_parse_names_and_lowercase_forms():
    p = parse_report_form("ticker=Berkshire+Hathaway")
    assert p.ticker == "BERKSHIRE HATHAWAY"
    p = parse_report_form("ticker=brk.b&form=def+14a")
    assert p.ticker == "BRK.B" and p.form == "DEF 14A"
    p = parse_report_form("ticker=aapl&form=8-k")
    assert p.form == "8-K"
