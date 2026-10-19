"""The owner's 100-case suite for Phase 3 (search-in-report, PDF
download, report share links, share card PNG). Same rule as Phases 1
and 2: run 100 cases, fix the small bugs, run again, fix the big bugs,
run again — go live only at 100/100.

Every case is offline and deterministic: the web pipeline is stubbed
at the same seam the other web suites use, PDF/card cases run on
hand-built diffs, and PDF validity is checked structurally (header,
trailer, xref offsets) plus by decompressing the content streams.
Case numbers (S001–S100) are stable so cycle reports can cite them.

Categories:
  A  Search core                 S001–S022
  B  Search web + URL safety     S023–S036
  C  PDF                         S037–S060
  D  Share links                 S061–S074
  E  Share card                  S075–S100
"""
import html as html_mod
import io
import re
import struct
import zlib
from types import SimpleNamespace

import pytest

import secdiff.web as web
from secdiff.card import (CardData, build_card_data, card_text, fit_text,
                          render_card_png, text_width, wrap_card_text)
from secdiff.cli import render_reports
from secdiff.diffing import Hunk, SectionDiff
from secdiff.pdf import pdf_safe_text, render_pdf, wrap_text
from secdiff.report import FilingInfo, render_html
from secdiff.score import compute_score
from secdiff.search import (clean_search_term, count_matches,
                            filter_diffs_by_term, hunk_matches,
                            mark_search_term)
from secdiff.web import application, parse_report_form
from secdiff.xbrl import DeltaRow, XbrlResult


# ---------------------------------------------------------------------------
# Harness (mirrors the earlier suites: pipeline stubbed at the web seam)

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
_ROW1 = DeltaRow("Revenue", "Revenues", "USD", 100.0, 150.0, 50.0, 50.0,
                 "Income statement")


def _result(ticker="EXMP", form="10-K", rows=(), diffs=None,
            new_date="2025-10-31", old_date="2024-11-01", company=None):
    info = _info(form=form, ticker=ticker, new_date=new_date,
                 old_date=old_date, company=company or "Example Corp")
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


def _stub(monkeypatch, tmp_path, resolve_map, results):
    store = web.UsageStore(tmp_path / "usage.json")
    monkeypatch.setattr(web, "_STORE", store)

    def fake_resolve(params):
        hit = resolve_map[params.ticker]
        if isinstance(hit, BaseException):
            raise hit
        return hit, "Fake Co"

    monkeypatch.setattr(web, "_resolve_company", fake_resolve)

    def fake_live(args):
        return results[args.ticker.upper()]

    monkeypatch.setattr(web, "live_pipeline", fake_live)
    return store


def _rargs(**kw):
    base = dict(risk_only=False, deletions_only=False, top=None,
                section=None, summarize=False, form="10-K")
    base.update(kw)
    return SimpleNamespace(**base)


def _counts_of(page: str):
    m = re.search(r"(\d+) deletions · (\d+) modifications · "
                  r"(\d+) additions", page)
    assert m, "counts line missing"
    return tuple(int(g) for g in m.groups())


_RISK_DIFFS = [
    SectionDiff(item="1A", title="Risk Factors", hunks=[
        Hunk("deletion", old="A lawsuit threatens the whole business.",
             size=40),
        Hunk("addition", new="New risk language was added here.", size=30)]),
    SectionDiff(item="7", title="MD&A", hunks=[
        Hunk("deletion", old="A lawsuit was mentioned in the MD&A text.",
             size=42),
        Hunk("deletion", old="Revenue grew in every segment this year.",
             size=40)]),
]


# ---------------------------------------------------------------------------
# A. Search core (S001–S022)

def test_s001_match_in_deletion():
    h = Hunk("deletion", old="The lawsuit was settled last year.")
    assert hunk_matches(h, "lawsuit")


def test_s002_match_in_addition():
    h = Hunk("addition", new="A new lawsuit defence fund was created.")
    assert hunk_matches(h, "lawsuit")


def test_s003_match_in_modification_old_side():
    h = Hunk("modification", old="The lawsuit risk was small.",
             new="The litigation risk was small.")
    assert hunk_matches(h, "lawsuit")


def test_s004_match_in_modification_new_side():
    h = Hunk("modification", old="The litigation risk was small.",
             new="The lawsuit risk was small.")
    assert hunk_matches(h, "lawsuit")


def test_s005_case_insensitive():
    h = Hunk("deletion", old="A LAWSUIT was filed in Texas.")
    assert hunk_matches(h, "lawsuit")
    assert hunk_matches(h, "LaWsUiT")


def test_s006_whole_phrase_matching():
    hit = Hunk("deletion", old="There is a going concern warning here.")
    miss = Hunk("deletion", old="Going forward, the concern is pricing.")
    assert hunk_matches(hit, "going concern")
    assert not hunk_matches(miss, "going concern")


def test_s007_phrase_split_across_hunks_does_not_match():
    diffs = [SectionDiff("1A", "Risk Factors", [
        Hunk("deletion", old="Our supply arrangements are stable."),
        Hunk("deletion", old="Chain stores reported higher sales.")])]
    _out, n = filter_diffs_by_term(diffs, "supply chain")
    assert n == 0


def test_s008_zero_hits_filter():
    out, n = filter_diffs_by_term(_RISK_DIFFS, "bankruptcy")
    assert out == [] and n == 0


def test_s009_count_is_hunks_not_occurrences():
    diffs = [SectionDiff("1A", "Risk Factors", [
        Hunk("deletion", old="lawsuit lawsuit lawsuit everywhere.")])]
    assert count_matches(diffs, "lawsuit") == 1


def test_s010_filter_keeps_order_and_drops_empty_sections():
    out, n = filter_diffs_by_term(_RISK_DIFFS, "lawsuit")
    assert n == 2                       # one matching hunk per section
    assert [s.item for s in out] == ["1A", "7"]
    assert [h.kind for h in out[1].hunks] == ["deletion"]


def test_s011_mark_wraps_term_and_counts():
    out, n = mark_search_term("the lawsuit ended", "lawsuit")
    assert n == 1
    assert "<mark class='search'>lawsuit</mark>" in out


def test_s012_mark_is_literal_not_regex():
    out, n = mark_search_term("value a.b and axb and C++ here", "a.b")
    assert n == 1
    assert "<mark class='search'>a.b</mark>" in out
    assert "<mark class='search'>axb</mark>" not in out
    out2, n2 = mark_search_term("written in C++ today", "C++")
    assert n2 == 1 and "<mark class='search'>C++</mark>" in out2


def test_s013_mark_never_inside_url():
    text = "see https://example.com/lawsuit-filing for the lawsuit news"
    out, n = mark_search_term(text, "lawsuit")
    assert n == 1
    assert "https://example.com/lawsuit-filing" in out
    assert out.count("<mark class='search'>") == 1


def test_s014_mark_never_inside_tag():
    text = "<a href='https://x.com/lawsuit'>lawsuit</a>"
    out, n = mark_search_term(text, "lawsuit")
    assert n == 1
    assert "href='https://x.com/lawsuit'" in out


def test_s015_mark_counts_every_occurrence():
    out, n = mark_search_term("fraud and fraud and fraud", "fraud")
    assert n == 3 and out.count("<mark class='search'>") == 3


def test_s016_clean_term_collapses_whitespace():
    assert clean_search_term("  going \t  concern  ") == "going concern"


def test_s017_clean_term_blank_is_no_search():
    assert clean_search_term(None) == ""
    assert clean_search_term("") == ""
    assert clean_search_term("   ") == ""


def test_s018_rendered_search_page_shows_only_matches():
    _md, ht, counts = render_reports(
        _rargs(search="lawsuit", search_base_url="/report?ticker=EXMP"),
        _info(), _RISK_DIFFS, _EMPTY_XBRL)
    assert counts == (2, 0, 0)
    assert "2 changes mention" in ht
    assert "<mark class='search'>lawsuit</mark>" in ht
    assert "Revenue grew in every segment" not in ht


def test_s019_rendered_zero_hit_page_is_honest():
    _md, ht, counts = render_reports(
        _rargs(search="bankruptcy"), _info(), _RISK_DIFFS, _EMPTY_XBRL)
    assert counts == (0, 0, 0)
    assert "0 changes mention" in ht
    assert "No changes in this report mention" in ht
    assert "bankruptcy" in ht


def test_s020_search_composes_with_deletions_only():
    _md, ht, counts = render_reports(
        _rargs(deletions_only=True, search="added"),
        _info(), _RISK_DIFFS, _EMPTY_XBRL)
    # "added" appears only in an ADDED hunk, which deletions-only
    # removed before the search ran.
    assert counts == (0, 0, 0)


def test_s021_search_composes_with_risk_only():
    _md, _ht, counts = render_reports(
        _rargs(risk_only=True, search="lawsuit"),
        _info(), _RISK_DIFFS, _EMPTY_XBRL)
    # Only Item 1A survives risk-only: 1 deletion + 0 from MD&A.
    assert counts == (1, 0, 0)


def test_s022_search_applies_after_top_n():
    big_first = [SectionDiff("1A", "Risk Factors", [
        Hunk("deletion", old="A huge unrelated removal of text.", size=100),
        Hunk("deletion", old="A tiny lawsuit removal.", size=10)])]
    _md, _ht, counts = render_reports(
        _rargs(top=1, search="lawsuit"), _info(), big_first, _EMPTY_XBRL)
    # top=1 kept only the big hunk; the search then found nothing,
    # proving the search runs after (not before) the top-N filter.
    assert counts == (0, 0, 0)
    _md, _ht, counts2 = render_reports(
        _rargs(search="lawsuit"), _info(), big_first, _EMPTY_XBRL)
    assert counts2 == (1, 0, 0)


# ---------------------------------------------------------------------------
# B. Search web + URL safety (S023–S036)

def test_s023_search_via_get(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, text = _call_text("GET", "/report",
                                  query="ticker=EXMP&q=lawsuit")
    assert status == "200 OK"
    assert _counts_of(text) == (1, 0, 0)
    assert "1 change mention" in text


def test_s024_search_via_post(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, text = _call_text("POST", "/report",
                                  body=b"ticker=EXMP&q=lawsuit")
    assert status == "200 OK"
    assert _counts_of(text) == (1, 0, 0)


def test_s025_script_term_rejected_400(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, text = _call_text(
        "GET", "/report", query="ticker=EXMP&q=%3Cscript%3Ealert(1)")
    assert status.startswith("400")
    assert "<script>alert" not in text


def test_s026_ampersand_term_shown_escaped(monkeypatch, tmp_path):
    diffs = [SectionDiff("1A", "Risk Factors", [
        Hunk("deletion", old="The AT&T contract ended this year.",
             size=30)])]
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(diffs=diffs)})
    status, _h, text = _call_text("GET", "/report",
                                  query="ticker=EXMP&q=AT%26T")
    assert status == "200 OK"
    assert "AT&amp;T" in text
    assert "1 change mention" in text


def test_s027_remove_filter_link_reproduces_full_report(
        monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, text = _call_text("GET", "/report",
                                  query="ticker=EXMP&q=lawsuit")
    assert status == "200 OK"
    m = re.search(r"href='(/report\?[^']+)'[^>]*>Show the full report",
                  text)
    assert m, "remove-filter link missing"
    link = html_mod.unescape(m.group(1))
    assert "q=" not in link.split("?", 1)[1]
    status, _h, full = _call_text("GET", "/report",
                                  query=link.split("?", 1)[1])
    assert status == "200 OK"
    assert _counts_of(full) == (1, 1, 1)


def test_s028_blank_q_means_no_search(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, text = _call_text("GET", "/report",
                                  query="ticker=EXMP&q=")
    assert status == "200 OK"
    assert "changes mention" not in text
    assert _counts_of(text) == (1, 1, 1)


def test_s029_whitespace_q_means_no_search(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, text = _call_text("GET", "/report",
                                  query="ticker=EXMP&q=%20%20%20")
    assert status == "200 OK"
    assert "changes mention" not in text


def test_s030_too_long_term_is_400(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, text = _call_text("GET", "/report",
                                  query="ticker=EXMP&q=" + "a" * 81)
    assert status.startswith("400")
    assert "80 characters" in text


def test_s031_punctuation_only_term_is_400(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, _t = _call_text("GET", "/report",
                                query="ticker=EXMP&q=---")
    assert status.startswith("400")


def test_s032_unicode_term_matches(monkeypatch, tmp_path):
    diffs = [SectionDiff("1A", "Risk Factors", [
        Hunk("deletion", old="The Café segment was sold this year.",
             size=30)])]
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(diffs=diffs)})
    status, _h, text = _call_text(
        "GET", "/report", query="ticker=EXMP&q=caf%C3%A9")
    assert status == "200 OK"
    assert _counts_of(text) == (1, 0, 0)


def test_s033_search_view_uses_no_extra_slot(monkeypatch, tmp_path):
    store = _stub(monkeypatch, tmp_path, {"EXMP": 12345},
                  {"EXMP": _result()})
    status, headers, _t = _call_text("GET", "/report", query="ticker=EXMP")
    assert status == "200 OK"
    vid = None
    for k, v in headers:
        if k == "Set-Cookie" and v.startswith("secdiff_vid="):
            vid = v.split(";")[0]
    assert vid
    status, _h, _t = _call_text("GET", "/report",
                                query="ticker=EXMP&q=lawsuit",
                                cookie=vid)
    assert status == "200 OK"
    assert len(store._load()[vid.split("=", 1)[1]]["ciks"]) == 1


def test_s034_search_on_proxy_report(monkeypatch, tmp_path):
    diffs = [SectionDiff("FULL", "Full document", [
        Hunk("deletion", old="The board election lawsuit ended.",
             size=30),
        Hunk("addition", new="A new director was proposed.", size=25)])]
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(form="DEF 14A", diffs=diffs)})
    status, _h, text = _call_text(
        "GET", "/report", query="ticker=EXMP&form=DEF+14A&q=lawsuit")
    assert status == "200 OK"
    assert _counts_of(text) == (1, 0, 0)


def test_s035_percent_encoded_phrase_search(monkeypatch, tmp_path):
    diffs = [SectionDiff("1A", "Risk Factors", [
        Hunk("deletion", old="Auditors raised a going concern doubt.",
             size=35)])]
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(diffs=diffs)})
    status, _h, text = _call_text(
        "GET", "/report", query="ticker=EXMP&q=going%20concern")
    assert status == "200 OK"
    assert "going concern" in text
    assert _counts_of(text) == (1, 0, 0)


def test_s036_parse_form_cleans_q():
    p = parse_report_form("ticker=AAPL&q=%20going%20%20%20concern%20")
    assert p.q == "going concern"
    p2 = parse_report_form("ticker=AAPL")
    assert p2.q == ""


# ---------------------------------------------------------------------------
# C. PDF (S037–S060)

def _pdf_stream_text(pdf: bytes) -> bytes:
    """All decompressed content-stream bytes of a secdiff PDF."""
    out = []
    for m in re.finditer(rb"<<[^>]*?/Length (\d+)[^>]*?>>\s*stream\n",
                         pdf, re.S):
        n = int(m.group(1))
        out.append(zlib.decompress(pdf[m.end():m.end() + n]))
    return b"\n".join(out)


def _assert_xref_valid(pdf: bytes, n_objects: int):
    assert pdf.startswith(b"%PDF-1.4")
    assert pdf.rstrip().endswith(b"%%EOF")
    sx = int(pdf.rsplit(b"startxref", 1)[1].split()[0])
    assert pdf[sx:sx + 4] == b"xref"
    lines = pdf[sx:].split(b"\n")
    count = int(lines[1].split()[1])
    assert count == n_objects + 1
    for ln in lines[2:2 + count]:
        parts = ln.split()
        if len(parts) == 3 and parts[2] == b"n":
            off = int(parts[0])
            assert re.match(rb"\d+ 0 obj", pdf[off:off + 12]), off


def _pdf_diffs(n_del=1, n_mod=1, n_add=1, del_text=None):
    hunks = []
    for i in range(n_del):
        hunks.append(Hunk("deletion",
                          old=(del_text or
                               f"Deleted clause {i + 1:02d} was removed "
                               "from the filing entirely."),
                          size=50))
    for i in range(n_mod):
        hunks.append(Hunk("modification", old="Old wording here.",
                          new="New wording here.", size=10))
    for i in range(n_add):
        hunks.append(Hunk("addition", new="Brand new sentence added.",
                          size=10))
    return [SectionDiff("1A", "Risk Factors", hunks)]


def test_s037_pdf_header_and_eof():
    pdf = render_pdf(_info(), _pdf_diffs(), _EMPTY_XBRL)
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")


def test_s038_pdf_xref_offsets_are_exact():
    pdf = render_pdf(_info(), _pdf_diffs(), _EMPTY_XBRL)
    _assert_xref_valid(pdf, 6)          # 4 fixed + page + contents


def test_s039_pdf_stream_names_company_and_ticker():
    pdf = render_pdf(_info(), _pdf_diffs(), _EMPTY_XBRL)
    text = _pdf_stream_text(pdf)
    assert b"Example Corp" in text and b"EXMP" in text
    assert b"2025-10-31" in text and b"2024-11-01" in text


def test_s040_pdf_counts_line():
    pdf = render_pdf(_info(), _pdf_diffs(2, 3, 4), _EMPTY_XBRL)
    assert b"2 deletions - 3 modifications - 4 additions" \
        in _pdf_stream_text(pdf)


def test_s041_pdf_score_line_matches_formula():
    diffs = _pdf_diffs()
    score = compute_score(diffs, _EMPTY_XBRL)
    pdf = render_pdf(_info(), diffs, _EMPTY_XBRL)
    expect = (f"Change-intensity score: {score.total}/100 "
              f"({score.band})")
    # Parentheses are escaped in PDF strings.
    expect = expect.replace("(", "\\(").replace(")", "\\)").encode()
    assert expect in _pdf_stream_text(pdf)


def test_s042_pdf_contains_top_deletion_text():
    pdf = render_pdf(_info(), _pdf_diffs(), _EMPTY_XBRL)
    assert b"Deleted clause 01 was removed" in _pdf_stream_text(pdf)


def test_s043_pdf_deletion_cap_is_25_with_honest_count():
    pdf = render_pdf(_info(), _pdf_diffs(40, 0, 0), _EMPTY_XBRL)
    text = _pdf_stream_text(pdf)
    assert b"showing 25 of 40" in text
    assert b"Deleted clause 25" in text
    assert b"Deleted clause 26" not in text


def test_s044_zero_change_pdf_is_valid_and_honest():
    pdf = render_pdf(_info(), [SectionDiff("1A", "Risk Factors", [])],
                     _EMPTY_XBRL)
    _assert_xref_valid(pdf, 6)
    text = _pdf_stream_text(pdf)
    assert b"0 deletions - 0 modifications - 0 additions" in text
    assert b"No deletions in this report" in text


def test_s045_proxy_pdf_is_valid_with_form_note():
    pdf = render_pdf(_info(form="DEF 14A"), _pdf_diffs(), _EMPTY_XBRL)
    _assert_xref_valid(pdf, 6)
    # Parentheses are escaped in PDF strings: \(DEF 14A\).
    assert b"Proxy statements \\(DEF 14A\\)" in _pdf_stream_text(pdf)


def test_s046_pdf_escapes_parens_and_backslash():
    diffs = _pdf_diffs(1, 0, 0,
                       del_text="Costs (net of tax) rose \\ sharply "
                                "this year in the quarter.")
    pdf = render_pdf(_info(), diffs, _EMPTY_XBRL)
    _assert_xref_valid(pdf, 6)
    assert b"\\(net of tax\\)" in _pdf_stream_text(pdf)


def test_s047_pdf_transliterates_unicode():
    diffs = _pdf_diffs(1, 0, 0,
                       del_text="The supply—chain risk ☃ grew "
                                "“materially” this year.")
    pdf = render_pdf(_info(), diffs, _EMPTY_XBRL)
    _assert_xref_valid(pdf, 6)
    text = _pdf_stream_text(pdf)
    assert b"supply-chain risk ? grew" in text
    assert b'"materially"' in text


def test_s048_huge_report_paginates():
    long_del = ("A very long deleted sentence about supply chains, "
                "litigation exposure and regulatory risk that keeps "
                "going for quite a while longer in this filing. ") * 4
    diffs = _pdf_diffs(25, 0, 0, del_text=long_del)
    pdf = render_pdf(_info(), diffs, _EMPTY_XBRL)
    n_pages = len(re.findall(rb"/Type /Page[^s]", pdf))
    m = re.search(rb"/Count (\d+)", pdf)
    assert n_pages >= 3
    assert int(m.group(1)) == n_pages
    _assert_xref_valid(pdf, 4 + 2 * n_pages)
    assert len(_pdf_stream_text(pdf)) > 5000


def test_s049_pdf_is_deterministic():
    a = render_pdf(_info(), _pdf_diffs(), _EMPTY_XBRL)
    b = render_pdf(_info(), _pdf_diffs(), _EMPTY_XBRL)
    assert a == b


def test_s050_long_deletion_truncated_with_marker():
    text = "x" * 900 + " ZZTAIL"
    pdf = render_pdf(_info(), _pdf_diffs(1, 0, 0, del_text=text),
                     _EMPTY_XBRL)
    body = _pdf_stream_text(pdf)
    assert b"[truncated" in body
    assert b"ZZTAIL" not in body


def test_s051_pdf_route_headers_and_filename(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(rows=[_ROW1])})
    status, headers, raw = _call("GET", "/report.pdf",
                                 query="ticker=EXMP&form=10-K")
    assert status == "200 OK"
    hdrs = dict(headers)
    assert hdrs["Content-Type"] == "application/pdf"
    assert hdrs["Content-Disposition"] == \
        'attachment; filename="secdiff-exmp-10-k-2025-10-31.pdf"'
    assert raw.startswith(b"%PDF-")


def test_s052_pdf_route_unknown_ticker_no_slot(monkeypatch, tmp_path):
    store = _stub(monkeypatch, tmp_path,
                  {"ZZZZZ": SystemExit("Ticker not found on EDGAR: ZZZZZ")},
                  {})
    status, _h, raw = _call("GET", "/report.pdf", query="ticker=ZZZZZ")
    assert status.startswith("400")
    assert store._load() == {}


def test_s053_pdf_route_counts_toward_limit(monkeypatch, tmp_path):
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    _stub(monkeypatch, tmp_path,
          {t: 5000 + i for i, t in enumerate(tickers)},
          {t: _result(ticker=t) for t in tickers})
    for t in tickers[:4]:
        status, _h, _r = _call("GET", "/report.pdf", query=f"ticker={t}",
                               cookie=_VID)
        assert status == "200 OK", t
    status, _h, raw = _call("GET", "/report.pdf", query="ticker=EEE",
                            cookie=_VID)
    assert status.startswith("402")
    assert b"$27" in raw


def test_s054_pdf_route_repeat_company_is_free(monkeypatch, tmp_path):
    store = _stub(monkeypatch, tmp_path, {"EXMP": 12345},
                  {"EXMP": _result()})
    for _ in range(3):
        status, _h, _r = _call("GET", "/report.pdf", query="ticker=EXMP",
                               cookie=_VID)
        assert status == "200 OK"
    assert len(store._load()["cd" * 16]["ciks"]) == 1


def test_s055_pdf_route_body_is_the_real_report(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, raw = _call("GET", "/report.pdf", query="ticker=EXMP")
    assert status == "200 OK"
    text = _pdf_stream_text(raw)
    assert b"Example Corp" in text
    assert b"A lawsuit was filed against the company." in text


def test_s056_pdf_route_respects_risk_only(monkeypatch, tmp_path):
    diffs = [SectionDiff("1A", "Risk Factors", [
                 Hunk("deletion", old="Risk deletion unique text.",
                      size=30)]),
             SectionDiff("7", "MD&A", [
                 Hunk("deletion", old="MDNA deletion unique text.",
                      size=30)])]
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(diffs=diffs)})
    status, _h, raw = _call("GET", "/report.pdf",
                            query="ticker=EXMP&risk_only=1")
    assert status == "200 OK"
    text = _pdf_stream_text(raw)
    assert b"Risk deletion unique text." in text
    assert b"MDNA deletion unique text." not in text


def test_s057_pdf_route_post_is_405(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, _r = _call("POST", "/report.pdf", body=b"ticker=EXMP")
    assert status.startswith("405")


def test_s058_very_long_company_name_pdf_still_valid():
    info = _info(company="The " + "Very Long Named " * 12 + "Company")
    pdf = render_pdf(info, _pdf_diffs(), _EMPTY_XBRL)
    _assert_xref_valid(pdf, 6)
    assert b"Very Long Named" in _pdf_stream_text(pdf)


def test_s059_wrap_text_never_exceeds_limit():
    lines = wrap_text("word " * 200, 10.0)
    limit = max(len(ln) for ln in lines)
    from secdiff.pdf import _chars_per_line
    assert limit <= _chars_per_line(10.0)
    hard = wrap_text("x" * 500, 10.0)
    assert all(len(ln) <= _chars_per_line(10.0) for ln in hard)
    assert "".join(hard) == "x" * 500


def test_s060_pdf_safe_text_replaces_unencodable():
    assert pdf_safe_text("snowman ☃ here") == "snowman ? here"
    assert pdf_safe_text("dash — here") == "dash - here"


# ---------------------------------------------------------------------------
# D. Share links (S061–S074)

def test_s061_report_page_has_share_link(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(rows=[_ROW1])})
    status, _h, text = _call_text("POST", "/report", body=b"ticker=EXMP")
    assert status == "200 OK"
    assert "Share this report:" in text
    assert "value='/report?ticker=EXMP&amp;form=10-K&amp;" in text


def test_s062_share_link_carries_options(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(rows=[_ROW1])})
    status, _h, text = _call_text(
        "POST", "/report",
        body=b"ticker=EXMP&risk_only=on&deletions_only=on&top=5")
    assert status == "200 OK"
    assert "risk_only=1" in text and "deletions_only=1" in text
    assert "top=5" in text


def test_s063_share_link_pins_compared_years(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(rows=[_ROW1])})
    status, _h, text = _call_text("POST", "/report", body=b"ticker=EXMP")
    assert "vs_year=2025" in text and "base_year=2024" in text


def test_s064_share_link_same_year_pair_unpinned(monkeypatch, tmp_path):
    res = _result(form="8-K", new_date="2025-05-02", old_date="2025-03-03")
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": res})
    status, _h, text = _call_text("POST", "/report",
                                  body=b"ticker=EXMP&form=8-K")
    assert status == "200 OK"
    m = re.search(r"id='share-url' readonly value='([^']+)'", text)
    assert m and "vs_year" not in html_mod.unescape(m.group(1))


def test_s065_share_link_reproduces_identical_counts(
        monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(rows=[_ROW1])})
    status, _h, text = _call_text("POST", "/report", body=b"ticker=EXMP")
    m = re.search(r"id='share-url' readonly value='([^']+)'", text)
    link = html_mod.unescape(m.group(1))
    status, _h, again = _call_text("GET", "/report",
                                   query=link.split("?", 1)[1])
    assert status == "200 OK"
    assert _counts_of(again) == _counts_of(text) == (1, 1, 1)


def test_s066_share_link_uses_resolved_ticker(monkeypatch, tmp_path):
    res = _result(ticker="GOOG")
    _stub(monkeypatch, tmp_path, {"ALPHABET": 1652044},
          {"ALPHABET": res})
    status, _h, text = _call_text("POST", "/report", body=b"ticker=Alphabet")
    assert status == "200 OK"
    assert "ticker=GOOG" in text


def test_s067_share_link_excludes_search_term(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, text = _call_text("GET", "/report",
                                  query="ticker=EXMP&q=lawsuit")
    m = re.search(r"id='share-url' readonly value='([^']+)'", text)
    assert m
    assert "q=" not in html_mod.unescape(m.group(1)).split("?", 1)[1]


def test_s068_copy_button_present(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, text = _call_text("POST", "/report", body=b"ticker=EXMP")
    assert "id='copy-share'" in text
    assert "Copy share link" in text


def test_s069_proxy_report_has_share_and_pdf_links(
        monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(form="DEF 14A", rows=())})
    status, _h, text = _call_text("POST", "/report",
                                  body=b"ticker=EXMP&form=DEF+14A")
    assert status == "200 OK"
    assert "id='share-url'" in text
    assert "/report.pdf?ticker=EXMP" in text
    assert "/report.csv" not in text       # still no numbers downloads


def test_s070_pdf_and_card_links_on_report(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(rows=[_ROW1])})
    status, _h, text = _call_text("POST", "/report", body=b"ticker=EXMP")
    assert "/report.pdf?ticker=EXMP" in text
    assert "/card?ticker=EXMP" in text
    assert "Download PDF" in text and "Share card (PNG)" in text


def test_s071_share_link_with_options_reproduces_counts(
        monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, text = _call_text(
        "POST", "/report", body=b"ticker=EXMP&deletions_only=on")
    assert _counts_of(text) == (1, 0, 0)
    m = re.search(r"id='share-url' readonly value='([^']+)'", text)
    link = html_mod.unescape(m.group(1))
    status, _h, again = _call_text("GET", "/report",
                                   query=link.split("?", 1)[1])
    assert status == "200 OK"
    assert _counts_of(again) == (1, 0, 0)


def test_s072_share_query_parses_back_to_same_params(
        monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(rows=[_ROW1])})
    status, _h, text = _call_text(
        "POST", "/report", body=b"ticker=EXMP&risk_only=on&top=7")
    m = re.search(r"id='share-url' readonly value='([^']+)'", text)
    query = html_mod.unescape(m.group(1)).split("?", 1)[1]
    p = parse_report_form(query)
    assert (p.ticker, p.form) == ("EXMP", "10-K")
    assert p.risk_only is True and p.top == 7
    assert (p.vs_year, p.base_year) == (2025, 2024)


def test_s073_card_link_pins_years_too(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(rows=[_ROW1])})
    status, _h, text = _call_text("POST", "/report", body=b"ticker=EXMP")
    m = re.search(r"href='(/card\?[^']+)'", text)
    assert m
    q = html_mod.unescape(m.group(1)).split("?", 1)[1]
    assert "vs_year=2025" in q and "base_year=2024" in q


def test_s074_numbers_downloads_still_listed(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(rows=[_ROW1])})
    status, _h, text = _call_text("POST", "/report", body=b"ticker=EXMP")
    assert "/report.csv?ticker=EXMP" in text
    assert "/report.xls?ticker=EXMP" in text


# ---------------------------------------------------------------------------
# E. Share card (S075–S100)

def _png_parts(png: bytes):
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ihdr = 8, b"", None
    kinds = []
    while pos < len(png):
        ln, kind = struct.unpack(">I4s", png[pos:pos + 8])
        kinds.append(kind)
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", png[pos + 8:pos + 8 + ln])
        if kind == b"IDAT":
            idat += png[pos + 8:pos + 8 + ln]
        pos += 12 + ln
    return ihdr, zlib.decompress(idat), kinds


def _pixel(raw: bytes, w: int, x: int, y: int):
    o = y * (1 + 3 * w) + 1 + 3 * x
    return tuple(raw[o:o + 3])


def _card_data(**kw):
    base = dict(company="Example Corp", ticker="EXMP", form="10-K",
                new_date="2025-10-31", old_date="2024-11-01",
                deletions=1, modifications=1, additions=1,
                xbrl_changed=0, score_total=6, score_band="Low",
                biggest_deletion="A lawsuit was filed.",
                report_url="https://x.test/report?ticker=EXMP")
    base.update(kw)
    return CardData(**base)


def test_s075_png_signature_and_iend():
    png = render_card_png(_card_data())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    _ihdr, _raw, kinds = _png_parts(png)
    assert kinds[-1] == b"IEND" and kinds[0] == b"IHDR"


def test_s076_png_dimensions_and_format():
    ihdr, _raw, _k = _png_parts(render_card_png(_card_data()))
    w, h, depth, ctype = ihdr[0], ihdr[1], ihdr[2], ihdr[3]
    assert (w, h) == (1200, 630)
    assert depth == 8 and ctype == 2


def test_s077_png_scanlines_wellformed():
    ihdr, raw, _k = _png_parts(render_card_png(_card_data()))
    w, h = ihdr[0], ihdr[1]
    assert len(raw) == h * (1 + 3 * w)
    assert all(raw[y * (1 + 3 * w)] == 0 for y in range(h))


def test_s078_card_background_and_accent_pixels():
    ihdr, raw, _k = _png_parts(render_card_png(_card_data()))
    w = ihdr[0]
    from secdiff.card import ACCENT, BG
    assert _pixel(raw, w, 5, 20) == BG
    assert _pixel(raw, w, 600, 5) == ACCENT


def test_s079_card_uses_the_three_report_colours():
    ihdr, raw, _k = _png_parts(render_card_png(_card_data()))
    colors = set()
    for y in range(0, ihdr[1], 3):
        for x in range(0, ihdr[0], 3):
            colors.add(_pixel(raw, ihdr[0], x, y))
    from secdiff.card import AMBER, GREEN, RED
    assert RED in colors and AMBER in colors and GREEN in colors


def test_s080_card_data_matches_pipeline_result():
    info, diffs, xbrl = _result(rows=[_ROW1])
    data = build_card_data(info, diffs, xbrl, "https://x.test/r")
    assert (data.deletions, data.modifications, data.additions) == (1, 1, 1)
    assert data.xbrl_changed == 1
    score = compute_score(diffs, xbrl)
    assert data.score_total == score.total
    assert data.biggest_deletion == "A lawsuit was filed against the company."


def test_s081_biggest_deletion_is_largest_hunk():
    diffs = [SectionDiff("1A", "Risk Factors", [
        Hunk("deletion", old="small one", size=9),
        Hunk("deletion", old="the biggest deletion text", size=99),
        Hunk("deletion", old="tied with biggest", size=99)])]
    data = build_card_data(_info(), diffs, _EMPTY_XBRL, "u")
    assert data.biggest_deletion == "the biggest deletion text"


def test_s082_no_deletions_card_is_honest():
    diffs = [SectionDiff("1A", "Risk Factors", [
        Hunk("addition", new="Only an addition here.", size=20)])]
    data = build_card_data(_info(), diffs, _EMPTY_XBRL, "u")
    assert data.biggest_deletion == "" and data.deletions == 0
    png = render_card_png(data)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_s083_long_company_name_is_fitted():
    name = "BERKSHIRE HATHAWAY INCORPORATED HOLDINGS COMPANY"
    fitted = fit_text(name, 1080, 6)
    assert text_width(fitted, 6) <= 1080
    assert fitted.endswith("..")
    png = render_card_png(_card_data(company=name))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_s084_card_text_transliterates_and_uppercases():
    out = card_text("Apple — “ESG” 🔴 fell…")
    assert out == 'APPLE - "ESG" FELL...'
    assert card_text("at&t (delaware)") == "AT&T (DELAWARE)"


def test_s085_card_is_deterministic():
    assert render_card_png(_card_data()) == render_card_png(_card_data())


def test_s086_wrap_card_text_two_lines_max():
    text = " ".join(["alpha"] * 100)
    lines = wrap_card_text(text, 1080, 3, 2)
    assert len(lines) == 2
    assert all(text_width(ln, 3) <= 1080 for ln in lines)
    assert lines[-1].endswith("..")
    one = wrap_card_text("short text", 1080, 3, 2)
    assert one == ["short text"]


def test_s087_card_route_headers_and_filename(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(rows=[_ROW1])})
    status, headers, raw = _call("GET", "/card",
                                 query="ticker=EXMP&form=10-K")
    assert status == "200 OK"
    hdrs = dict(headers)
    assert hdrs["Content-Type"] == "image/png"
    assert hdrs["Content-Disposition"] == \
        'attachment; filename="secdiff-exmp-10-k-card.png"'
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_s088_card_route_unknown_ticker_is_404(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path,
          {"ZZZZZ": SystemExit("Ticker not found on EDGAR: ZZZZZ")}, {})
    status, _h, raw = _call("GET", "/card", query="ticker=ZZZZZ")
    assert status.startswith("404")
    assert not raw.startswith(b"\x89PNG")


def test_s089_card_route_counts_toward_limit(monkeypatch, tmp_path):
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    _stub(monkeypatch, tmp_path,
          {t: 6000 + i for i, t in enumerate(tickers)},
          {t: _result(ticker=t) for t in tickers})
    for t in tickers[:4]:
        status, _h, _r = _call("GET", "/card", query=f"ticker={t}",
                               cookie=_VID)
        assert status == "200 OK", t
    status, _h, _r = _call("GET", "/card", query="ticker=EEE",
                           cookie=_VID)
    assert status.startswith("402")


def test_s090_card_unknown_ticker_uses_no_slot(monkeypatch, tmp_path):
    store = _stub(monkeypatch, tmp_path,
                  {"ZZZZZ": SystemExit("Ticker not found on EDGAR: ZZZZZ")},
                  {})
    status, _h, _r = _call("GET", "/card", query="ticker=ZZZZZ")
    assert status.startswith("404")
    assert store._load() == {}


def test_s091_card_route_proxy_works(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(form="DEF 14A", rows=())})
    status, _h, raw = _call("GET", "/card",
                            query="ticker=EXMP&form=DEF+14A")
    assert status == "200 OK"
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_s092_card_route_8k_works(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345},
          {"EXMP": _result(form="8-K")})
    status, _h, raw = _call("GET", "/card", query="ticker=EXMP&form=8-K")
    assert status == "200 OK"
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_s093_card_route_bad_form_is_400(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, _r = _call("GET", "/card", query="ticker=EXMP&form=S-1")
    assert status.startswith("400")


def test_s094_card_route_post_is_405(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, _r = _call("POST", "/card", body=b"ticker=EXMP")
    assert status.startswith("405")


def test_s095_card_route_png_dimensions(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, raw = _call("GET", "/card", query="ticker=EXMP")
    assert status == "200 OK"
    ihdr, _raw, _k = _png_parts(raw)
    assert (ihdr[0], ihdr[1]) == (1200, 630)


def test_s096_score_bar_fill_matches_score():
    from secdiff.card import TRACK
    full = render_card_png(_card_data(score_total=100,
                                      score_band="Very high"))
    ihdr, raw, _k = _png_parts(full)
    assert _pixel(raw, ihdr[0], 1130, 494) == (248, 113, 113)
    empty = render_card_png(_card_data(score_total=0,
                                       score_band="No change detected"))
    ihdr, raw, _k = _png_parts(empty)
    assert _pixel(raw, ihdr[0], 70, 494) == TRACK


def test_s097_card_changes_with_deletion_excerpt():
    a = render_card_png(_card_data(biggest_deletion="First deletion text."))
    b = render_card_png(_card_data(biggest_deletion="Other deletion text."))
    assert a != b


def test_s098_card_changes_with_counts():
    a = render_card_png(_card_data(deletions=1))
    b = render_card_png(_card_data(deletions=2))
    assert a != b


def test_s099_card_route_with_year_params(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, raw = _call(
        "GET", "/card",
        query="ticker=EXMP&vs_year=2025&base_year=2024")
    assert status == "200 OK"
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_s100_card_route_head_ok(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, {"EXMP": 12345}, {"EXMP": _result()})
    status, _h, _r = _call("HEAD", "/card", query="ticker=EXMP")
    assert status == "200 OK"
