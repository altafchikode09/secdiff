"""The owner's 100-case suite for Phase 5 (professional redesign,
dark mode, mobile + PWA basics). Same rule as Phases 1–4: run the
100 cases, fix the small bugs, run again, fix the big bugs, run
again — go live only at 100/100 plus the full suite green.

Phase 5 is presentation only: this suite proves the design system
(one palette, two themes, shared header/footer), the theme cookie +
toggle behaviour, per-theme legibility (WCAG contrast computed from
the very palette dicts the CSS is generated from), the mobile/PWA
surface (viewport, manifest, generated icons, NO service worker by
design), offline purity (no external resources anywhere), and that
every functional anchor from the earlier phases is unchanged.

Every case is offline and deterministic: EDGAR is stubbed at the
same web seams the earlier suites use, stores live in tmp dirs.
Case numbers (T001–T100) are stable so cycle reports can cite them.

Categories:
  A  Theme cookie + toggle   T001–T016
  B  Pages in both themes    T017–T036
  C  Design-system markers   T037–T051
  D  Contrast (WCAG)         T052–T066
  E  Mobile + PWA            T067–T081
  F  Offline purity          T082–T091
  G  Anchor regressions      T092–T100
"""
import io
import json
import re
import struct
from types import SimpleNamespace

import pytest

import secdiff.web as web
from secdiff.charts import ChartPoint, ChartSeries
from secdiff.diffing import Hunk, SectionDiff
from secdiff.edgar import Filing
from secdiff.icons import ICON_SIZES, manifest, render_icon_png
from secdiff.majors import MajorsStore
from secdiff.report import FilingInfo, render_html
from secdiff.theme import PALETTES, REPORT_CSS, SITE_CSS, \
    contrast_ratio
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


_VID = "secdiff_vid=" + "cd" * 16
_DARK = _VID + "; secdiff_theme=dark"
_LIGHT = _VID + "; secdiff_theme=light"


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


def _html_tag_of(page: str) -> str:
    m = re.search(r"<html[^>]*>", page)
    assert m, "no <html> tag"
    return m.group(0)


def _info(form="10-K", new_date="2025-10-31", old_date="2024-11-01",
          ticker="AAPL", company="Apple Inc.", cik=320193):
    return FilingInfo(
        ticker=ticker, company=company, form=form, cik=cik,
        new_accession="0000320193-25-000079", new_date=new_date,
        new_url="https://www.sec.gov/new",
        new_index_url="https://www.sec.gov/new-idx",
        old_accession="0000320193-24-000123", old_date=old_date,
        old_url="https://www.sec.gov/old",
        old_index_url="https://www.sec.gov/old-idx",
        section_confidence="high")


def _anchor_result():
    """A crafted result with the AAPL 10-K anchor counts:
    155 deletions / 162 modifications / 50 additions / 75 XBRL rows,
    scoring exactly 80 (40 + 30 + 0 + 10) under the published
    formula."""
    hunks = (
        [Hunk(kind="deletion", old=f"Deleted sentence {i}.", size=10)
         for i in range(155)]
        + [Hunk(kind="modification", old=f"Old wording {i}.",
                new=f"New wording {i}.", size=10) for i in range(162)]
        + [Hunk(kind="addition", new=f"Added sentence {i}.", size=10)
           for i in range(50)])
    diffs = [SectionDiff(item="1A", title="Risk Factors", hunks=hunks)]
    rows = [DeltaRow(label=f"Concept {i}", concept=f"C{i}",
                     old=100.0 + i, new=110.0 + i, change=10.0, pct=5.0)
            for i in range(75)]
    xbrl = XbrlResult(rows=rows, unmapped=[], matched=75,
                      considered=100, disappeared=0)
    return _info(), diffs, xbrl


def _stub_pipeline(monkeypatch, resolve_map, results):
    """resolve_map: typed ticker -> cik or exception.
    results: ticker -> (info, diffs, xbrl)."""
    def fake_resolve(params):
        hit = resolve_map[params.ticker]
        if isinstance(hit, BaseException):
            raise hit
        return hit, "Fake Co"

    monkeypatch.setattr(web, "_resolve_company", fake_resolve)

    def fake_live(args):
        return results[args.ticker.upper()]

    monkeypatch.setattr(web, "live_pipeline", fake_live)


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


# ---------------------------------------------------------------------------
# A. Theme cookie + toggle (T001–T016)

def test_t001_home_no_cookie_has_no_theme_attr(env):
    _s, _h, text = _call_text("GET", "/", cookie=_VID)
    assert _html_tag_of(text) == '<html lang="en">'


def test_t002_home_dark_cookie_sets_dark_attr(env):
    _s, _h, text = _call_text("GET", "/", cookie=_DARK)
    assert _html_tag_of(text) == '<html lang="en" data-theme="dark">'


def test_t003_home_light_cookie_sets_light_attr(env):
    _s, _h, text = _call_text("GET", "/", cookie=_LIGHT)
    assert _html_tag_of(text) == '<html lang="en" data-theme="light">'


def test_t004_bogus_theme_cookie_falls_back(env):
    _s, _h, text = _call_text(
        "GET", "/", cookie=_VID + "; secdiff_theme=purple")
    assert _html_tag_of(text) == '<html lang="en">'


def test_t005_theme_cookie_among_others_parses(env):
    _s, _h, text = _call_text(
        "GET", "/", cookie="other=1; secdiff_theme=dark; " + _VID)
    assert 'data-theme="dark"' in _html_tag_of(text)


def test_t006_post_theme_dark_sets_cookie_redirects(env):
    status, headers, _t = _call_text("POST", "/theme", body=b"theme=dark",
                                     cookie=_VID)
    assert status.startswith("303")
    cookies = [v for k, v in headers if k == "Set-Cookie"]
    theme = [c for c in cookies if c.startswith("secdiff_theme=")]
    assert theme and theme[0].startswith("secdiff_theme=dark;")
    assert "Path=/" in theme[0]
    assert "Max-Age=31536000" in theme[0]
    assert "SameSite=Lax" in theme[0]


def test_t007_post_theme_light_sets_cookie(env):
    status, headers, _t = _call_text("POST", "/theme",
                                     body=b"theme=light", cookie=_VID)
    assert status.startswith("303")
    theme = [v for k, v in headers
             if k == "Set-Cookie" and v.startswith("secdiff_theme=")]
    assert theme and theme[0].startswith("secdiff_theme=light;")


def test_t008_theme_cookie_is_not_httponly(env):
    # The inline theme-init script must be able to read it.
    _s, headers, _t = _call_text("POST", "/theme", body=b"theme=dark",
                                 cookie=_VID)
    theme = [v for k, v in headers
             if k == "Set-Cookie" and v.startswith("secdiff_theme=")]
    assert theme and "HttpOnly" not in theme[0]


def test_t009_post_theme_invalid_value_400(env):
    status, _h, _t = _call_text("POST", "/theme", body=b"theme=purple",
                                cookie=_VID)
    assert status.startswith("400")


def test_t010_post_theme_missing_value_400(env):
    status, _h, _t = _call_text("POST", "/theme", body=b"next=/",
                                cookie=_VID)
    assert status.startswith("400")


def test_t011_get_theme_is_405(env):
    status, _h, _t = _call_text("GET", "/theme", cookie=_VID)
    assert status.startswith("405")


def test_t012_post_theme_honours_local_next(env):
    status, headers, _t = _call_text(
        "POST", "/theme", body=b"theme=dark&next=/trending",
        cookie=_VID)
    assert status.startswith("303")
    assert dict(headers)["Location"] == "/trending"


def test_t013_post_theme_rejects_protocol_relative_next(env):
    status, headers, _t = _call_text(
        "POST", "/theme", body=b"theme=dark&next=//evil.example",
        cookie=_VID)
    assert status.startswith("303")
    assert dict(headers)["Location"] == "/"


def test_t014_post_theme_rejects_absolute_next(env):
    status, headers, _t = _call_text(
        "POST", "/theme", body=b"theme=dark&next=https://evil.example",
        cookie=_VID)
    assert status.startswith("303")
    assert dict(headers)["Location"] == "/"


def test_t015_toggle_form_present_with_both_buttons(env):
    _s, _h, text = _call_text("GET", "/", cookie=_VID)
    assert "action='/theme'" in text
    assert "name='theme' value='light'" in text
    assert "name='theme' value='dark'" in text


def test_t016_toggle_marks_the_active_theme(env):
    _s, _h, text = _call_text("GET", "/", cookie=_DARK)
    assert "value='dark' class='themebtn on'" in text
    _s, _h, text = _call_text("GET", "/", cookie=_LIGHT)
    assert "value='light' class='themebtn on'" in text


# ---------------------------------------------------------------------------
# B. Every page renders in both themes (T017–T036)

def _both_themes(path, query="", cookie_base=_VID, **kw):
    out = {}
    for name, extra in (("dark", "; secdiff_theme=dark"),
                        ("light", "; secdiff_theme=light")):
        status, _h, text = _call_text(
            "GET", path, query=query, cookie=cookie_base + extra, **kw)
        assert status == "200 OK", (path, name, status)
        out[name] = text
    return out


def _assert_themed(pages):
    assert 'data-theme="dark"' in _html_tag_of(pages["dark"])
    assert 'data-theme="light"' in _html_tag_of(pages["light"])


def test_t017_home_dark(env):
    pages = _both_themes("/")
    assert 'data-theme="dark"' in _html_tag_of(pages["dark"])


def test_t018_home_light(env):
    pages = _both_themes("/")
    assert 'data-theme="light"' in _html_tag_of(pages["light"])


def test_t019_login_both_themes(env):
    _assert_themed(_both_themes("/login"))


def test_t020_signup_both_themes(env):
    _assert_themed(_both_themes("/signup"))


def test_t021_forgot_password_both_themes(env):
    _assert_themed(_both_themes("/forgot-password"))


def test_t022_forgot_username_both_themes(env):
    _assert_themed(_both_themes("/forgot-username"))


def test_t023_trending_both_themes(env):
    _assert_themed(_both_themes("/trending"))


def test_t024_sectors_both_themes(env):
    _assert_themed(_both_themes("/sectors"))


def test_t025_api_docs_both_themes(env):
    _assert_themed(_both_themes("/api"))


def test_t026_error_page_both_themes(env):
    out = {}
    for name, extra in (("dark", "; secdiff_theme=dark"),
                        ("light", "; secdiff_theme=light")):
        status, _h, text = _call_text("GET", "/no-such-page",
                                      cookie=_VID + extra)
        assert status.startswith("404")
        out[name] = text
    _assert_themed(out)


def test_t027_account_page_both_themes(env):
    session = _signup("themeuser", "theme@example.com")
    out = {}
    for name, extra in (("dark", "; secdiff_theme=dark"),
                        ("light", "; secdiff_theme=light")):
        status, _h, text = _call_text(
            "GET", "/account", cookie=_VID + "; " + session + extra)
        assert status == "200 OK"
        out[name] = text
    _assert_themed(out)


def test_t028_watchlist_page_both_themes(env):
    session = _signup("watchuser", "watch@example.com")
    out = {}
    for name, extra in (("dark", "; secdiff_theme=dark"),
                        ("light", "; secdiff_theme=light")):
        status, _h, text = _call_text(
            "GET", "/watchlist", cookie=_VID + "; " + session + extra)
        assert status == "200 OK"
        out[name] = text
    _assert_themed(out)


def test_t029_company_page_both_themes(env, monkeypatch):
    class FakeClient:
        name_matched_ticker = None

        def resolve_ticker(self, q, form="10-K", min_count=2):
            return 424242, "Fake Holdings Inc."

        def all_filings(self, cik, form, count=None):
            return [Filing(form=form, accession="a1",
                           filing_date="2026-02-01", report_date="",
                           primary_document="d.htm"),
                    Filing(form=form, accession="a2",
                           filing_date="2025-02-01", report_date="",
                           primary_document="d.htm")]

    monkeypatch.setattr(web, "EdgarClient",
                        lambda email=None, cache_dir=None: FakeClient())
    _assert_themed(_both_themes("/company/FAKE"))


def test_t030_paywall_page_both_themes(env):
    for theme in ("dark", "light"):
        web._THEME_CTX.value = theme
        try:
            page = web.paywall_page(None)
        finally:
            web._THEME_CTX.value = None
        assert f'data-theme="{theme}"' in _html_tag_of(page)


def test_t031_waitlist_thanks_both_themes(env):
    for theme in ("dark", "light"):
        web._THEME_CTX.value = theme
        try:
            page = web.waitlist_thanks_page(None)
        finally:
            web._THEME_CTX.value = None
        assert f'data-theme="{theme}"' in _html_tag_of(page)


def test_t032_report_page_dark(env, monkeypatch):
    _stub_pipeline(monkeypatch, {"AAPL": 320193},
                   {"AAPL": _anchor_result()})
    status, _h, text = _call_text("POST", "/report",
                                  body=b"ticker=AAPL", cookie=_DARK)
    assert status == "200 OK"
    assert _html_tag_of(text) == '<html lang="en" data-theme="dark">'


def test_t033_report_page_light(env, monkeypatch):
    _stub_pipeline(monkeypatch, {"AAPL": 320193},
                   {"AAPL": _anchor_result()})
    status, _h, text = _call_text("POST", "/report",
                                  body=b"ticker=AAPL", cookie=_LIGHT)
    assert status == "200 OK"
    assert _html_tag_of(text) == '<html lang="en" data-theme="light">'


def test_t034_report_page_no_cookie_no_attr(env, monkeypatch):
    _stub_pipeline(monkeypatch, {"AAPL": 320193},
                   {"AAPL": _anchor_result()})
    status, _h, text = _call_text("POST", "/report",
                                  body=b"ticker=AAPL", cookie=_VID)
    assert status == "200 OK"
    assert _html_tag_of(text) == "<html lang='en'>"


def test_t035_stats_stays_404_in_both_themes(env):
    for extra in ("; secdiff_theme=dark", "; secdiff_theme=light"):
        status, _h, text = _call_text("GET", "/stats",
                                      cookie=_VID + extra)
        assert status.startswith("404")


def test_t036_theme_choice_survives_to_report_downloads(env,
                                                       monkeypatch):
    # The theme is presentation only: downloads are byte-identical
    # whatever the cookie says.
    _stub_pipeline(monkeypatch, {"AAPL": 320193},
                   {"AAPL": _anchor_result()})
    _s1, _h1, raw_dark = _call("GET", "/report.csv",
                               query="ticker=AAPL", cookie=_DARK)
    _s2, _h2, raw_light = _call("GET", "/report.csv",
                                 query="ticker=AAPL", cookie=_LIGHT)
    assert raw_dark == raw_light


# ---------------------------------------------------------------------------
# C. Design-system markers (T037–T051)

def test_t037_site_css_root_light_vars(env):
    assert ":root{--bg:#ffffff;" in SITE_CSS
    assert "--text:#1a1a2e" in SITE_CSS


def test_t038_site_css_dark_attr_block(env):
    assert '[data-theme="dark"]{--bg:#0b1220;' in SITE_CSS


def test_t039_site_css_media_query_fallback(env):
    assert "@media (prefers-color-scheme: dark)" in SITE_CSS
    assert ':root:not([data-theme="light"])' in SITE_CSS


def test_t040_report_css_has_all_theme_layers(env):
    assert ":root{--bg:#ffffff;" in REPORT_CSS
    assert '[data-theme="dark"]{--bg:#0b1220;' in REPORT_CSS
    assert "@media (prefers-color-scheme: dark)" in REPORT_CSS
    assert ':root:not([data-theme="light"])' in REPORT_CSS


def test_t041_palettes_have_identical_keys(env):
    assert set(PALETTES["light"]) == set(PALETTES["dark"])
    assert len(PALETTES["light"]) >= 30


def test_t042_palette_values_are_hex(env):
    for palette in PALETTES.values():
        for value in palette.values():
            assert re.fullmatch(r"#[0-9a-f]{6}", value), value


def test_t043_header_nav_sections_in_order(env):
    _s, _h, text = _call_text("GET", "/", cookie=_VID)
    header = text.split("</header>")[0]
    positions = [header.index(f">{label}<") for label in
                 ("Reports", "Trending", "Sectors", "Watchlist", "API")]
    assert positions == sorted(positions)
    for href in ("/trending", "/sectors", "/watchlist", "/api"):
        assert f"href='{href}'" in header


def test_t044_header_logged_in_shows_my_account(env):
    session = _signup("navuser", "nav@example.com")
    _s, _h, text = _call_text("GET", "/", cookie=_VID + "; " + session)
    header = text.split("</header>")[0]
    assert "My account" in header
    assert "Hi, <strong>navuser</strong>" in header


def test_t045_footer_shared_with_nav_and_note(env):
    _s, _h, text = _call_text("GET", "/login", cookie=_VID)
    assert "class='sitefoot'" in text
    foot = text.split("class='sitefoot'")[1]
    for href in ("/trending", "/sectors", "/watchlist", "/api"):
        assert f"href='{href}'" in foot
    assert "Not investment advice" in foot


def test_t046_site_css_tablewrap_scrolls(env):
    assert ".tablewrap{overflow-x:auto" in SITE_CSS


def test_t047_site_css_responsive_breakpoints(env):
    assert "@media (max-width:700px)" in SITE_CSS
    assert "@media (max-width:560px)" in SITE_CSS


def test_t048_site_css_tap_targets(env):
    assert "min-height:44px" in SITE_CSS


def test_t049_report_css_chart_classes(env):
    for marker in (".chart rect.cb{fill:var(--chart-bar)}",
                   ".chart rect.cbn{fill:var(--chart-neg)}",
                   ".chart text.ct{fill:var(--chart-text)}",
                   ".chart text.cl{fill:var(--chart-muted)}",
                   ".chart line.ca{stroke:var(--chart-axis)}"):
        assert marker in REPORT_CSS, marker
    # …and the SVG renderer emits those classes (keeping the
    # light-theme fill attributes the phase-2 suite anchors on).
    from secdiff.charts import render_chart_svg
    series = ChartSeries(
        label="Revenue", alias="Revenues", taxonomy="us-gaap",
        unit="USD",
        points=[ChartPoint(fy="FY2024", end="2024-09-28", value=3.9e11),
                ChartPoint(fy="FY2025", end="2025-09-27",
                           value=-1.0e10)])
    svg = render_chart_svg(series)
    for marker in ("class='cb'", "class='cbn'", "class='ct'",
                   "class='cl'", "class='ca'"):
        assert marker in svg, marker
    assert "#dc2626" in svg


def test_t050_report_css_tablewrap(env):
    assert ".tablewrap{overflow-x:auto" in REPORT_CSS


def test_t051_diff_colours_come_from_variables(env):
    assert ".del{background:var(--del-bg)" in REPORT_CSS
    assert ".add{background:var(--add-bg)" in REPORT_CSS
    assert ".mod{background:var(--mod-bg)" in REPORT_CSS
    assert ".card{background:var(--surface)" in SITE_CSS
    assert "mark.risk{background:var(--mark-risk)" in REPORT_CSS
    assert ".score{background:var(--score-bg)" in REPORT_CSS


# ---------------------------------------------------------------------------
# D. Contrast — computed from the shipped palettes (T052–T066)

def _both(fg, bg, minimum=4.5):
    for theme in ("light", "dark"):
        pal = PALETTES[theme]
        ratio = contrast_ratio(pal[fg], pal[bg])
        assert ratio >= minimum, (theme, fg, bg, ratio)


def test_t052_contrast_text_on_bg(env):
    _both("text", "bg")


def test_t053_contrast_text_on_surface(env):
    _both("text", "surface")


def test_t054_contrast_muted_on_bg(env):
    _both("muted", "bg")


def test_t055_contrast_muted_on_surface(env):
    _both("muted", "surface")


def test_t056_contrast_link_on_bg(env):
    _both("link", "bg")


def test_t057_contrast_text_on_del_bg(env):
    _both("text", "del-bg")


def test_t058_contrast_text_on_add_bg(env):
    _both("text", "add-bg")


def test_t059_contrast_text_on_mod_bg(env):
    _both("text", "mod-bg")


def test_t060_contrast_text_on_mark_risk(env):
    _both("text", "mark-risk")


def test_t061_contrast_text_on_mark_search(env):
    _both("text", "mark-search")


def test_t062_contrast_text_on_score_bg(env):
    _both("text", "score-bg")


def test_t063_contrast_repeat_note(env):
    _both("repeat-text", "repeat-bg")


def test_t064_contrast_button_text(env):
    _both("accent-text", "accent")


def test_t065_contrast_pos_neg_on_bg(env):
    _both("pos", "bg")
    _both("neg", "bg")


def test_t066_contrast_chart_text(env):
    _both("chart-text", "bg")
    _both("chart-muted", "bg")
    # Chart bars are graphical objects: WCAG non-text minimum 3:1.
    _both("chart-bar", "bg", minimum=3.0)
    _both("chart-neg", "bg", minimum=3.0)


# ---------------------------------------------------------------------------
# E. Mobile + PWA (T067–T081)

def test_t067_viewport_meta_home(env):
    _s, _h, text = _call_text("GET", "/", cookie=_VID)
    assert 'content="width=device-width, initial-scale=1"' in text


def test_t068_viewport_meta_report(env, monkeypatch):
    _stub_pipeline(monkeypatch, {"AAPL": 320193},
                   {"AAPL": _anchor_result()})
    _s, _h, text = _call_text("POST", "/report", body=b"ticker=AAPL",
                              cookie=_VID)
    assert "width=device-width, initial-scale=1" in text
    # The standalone (CLI) rendering of the same report also carries
    # the viewport + the OS-preference theme layer, with no
    # data-theme attribute stamped on it.
    info, diffs, xbrl = _anchor_result()
    page = render_html(info, diffs, xbrl)
    assert "width=device-width, initial-scale=1" in page
    assert "@media (prefers-color-scheme: dark)" in page
    assert _html_tag_of(page) == "<html lang='en'>"


def test_t069_viewport_meta_login(env):
    _s, _h, text = _call_text("GET", "/login", cookie=_VID)
    assert 'content="width=device-width, initial-scale=1"' in text


def test_t070_manifest_route_and_content_type(env):
    status, headers, _raw = _call("GET", "/manifest.webmanifest",
                                  cookie=_VID)
    assert status == "200 OK"
    assert dict(headers)["Content-Type"] \
        == "application/manifest+json"


def test_t071_manifest_names(env):
    _s, _h, raw = _call("GET", "/manifest.webmanifest", cookie=_VID)
    data = json.loads(raw)
    assert data["name"] == "secdiff"
    assert data["short_name"] == "secdiff"


def test_t072_manifest_display_fields(env):
    data = manifest()
    assert data["start_url"] == "/"
    assert data["scope"] == "/"
    assert data["display"] == "standalone"


def test_t073_manifest_icons(env):
    data = manifest()
    sizes = {icon["sizes"] for icon in data["icons"]}
    assert sizes == {"192x192", "512x512"}
    for icon in data["icons"]:
        assert icon["type"] == "image/png"
        assert icon["src"].startswith("/icon-")


def test_t074_manifest_colours_from_palette(env):
    data = manifest()
    assert data["theme_color"] == PALETTES["dark"]["bg"]
    assert data["background_color"] == PALETTES["dark"]["bg"]
    assert re.fullmatch(r"#[0-9a-f]{6}", data["theme_color"])


def _png_dims(raw: bytes):
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", raw[16:24])


def test_t075_icon_192_route(env):
    status, headers, raw = _call("GET", "/icon-192.png", cookie=_VID)
    assert status == "200 OK"
    assert dict(headers)["Content-Type"] == "image/png"
    assert _png_dims(raw) == (192, 192)


def test_t076_icon_512_route(env):
    status, headers, raw = _call("GET", "/icon-512.png", cookie=_VID)
    assert status == "200 OK"
    assert dict(headers)["Content-Type"] == "image/png"
    assert _png_dims(raw) == (512, 512)


def test_t077_apple_touch_icon_route(env):
    status, headers, raw = _call("GET", "/apple-touch-icon.png",
                                 cookie=_VID)
    assert status == "200 OK"
    assert dict(headers)["Content-Type"] == "image/png"
    assert _png_dims(raw) == (192, 192)


def test_t078_icons_deterministic(env):
    assert render_icon_png(192) == render_icon_png(192)
    assert render_icon_png(512) == render_icon_png(512)
    _s, _h, raw = _call("GET", "/icon-192.png", cookie=_VID)
    assert raw == render_icon_png(192)
    assert ICON_SIZES == (192, 512)


def test_t079_icons_carry_cache_header(env):
    _s, headers, _raw = _call("GET", "/icon-192.png", cookie=_VID)
    cache = [v for k, v in headers if k == "Cache-Control"]
    assert cache and "max-age=" in cache[0]


def test_t080_no_service_worker_by_design(env):
    status, _h, _t = _call_text("GET", "/sw.js", cookie=_VID)
    assert status.startswith("404")
    status, _h, _t = _call_text("GET", "/service-worker.js",
                                cookie=_VID)
    assert status.startswith("404")


def test_t081_manifest_linked_and_theme_color_meta(env):
    _s, _h, text = _call_text("GET", "/", cookie=_VID)
    head = text.split("</head>")[0]
    assert '<link rel="manifest" href="/manifest.webmanifest">' in head
    assert 'name="theme-color"' in head
    assert 'media="(prefers-color-scheme: dark)"' in head
    assert 'media="(prefers-color-scheme: light)"' in head
    assert "serviceWorker" not in text


# ---------------------------------------------------------------------------
# F. Offline purity — no external resources anywhere (T082–T091)

_EXTERNAL_PATTERNS = ('<script src', "<script src=", 'rel="stylesheet"',
                      "rel='stylesheet'", "@import", "<img")


def _assert_pure(text):
    for pat in _EXTERNAL_PATTERNS:
        assert pat not in text, pat
    for tag in re.findall(r"<link[^>]*>", text):
        href = re.search(r'href="([^"]*)"', tag)
        assert href and href.group(1).startswith("/"), tag
    for tag in re.findall(r"<script[^>]*>", text):
        assert "src" not in tag, tag


def test_t082_home_has_no_external_resources(env):
    _s, _h, text = _call_text("GET", "/", cookie=_VID)
    _assert_pure(text)


def test_t083_login_has_no_external_resources(env):
    _s, _h, text = _call_text("GET", "/login", cookie=_VID)
    _assert_pure(text)


def test_t084_report_has_no_external_resources(env, monkeypatch):
    # EDGAR source links are <a> content links (allowed); loaded
    # resources (script/link/img) must all be local/inline.
    _stub_pipeline(monkeypatch, {"AAPL": 320193},
                   {"AAPL": _anchor_result()})
    _s, _h, text = _call_text("POST", "/report", body=b"ticker=AAPL",
                              cookie=_VID)
    _assert_pure(text)


def test_t085_api_docs_no_external_resources(env):
    _s, _h, text = _call_text("GET", "/api", cookie=_VID)
    _assert_pure(text)


def test_t086_stylesheets_have_no_external_refs(env):
    for css in (SITE_CSS, REPORT_CSS):
        assert "http://" not in css
        assert "https://" not in css
        assert "@import" not in css
        assert "url(" not in css


def test_t087_manifest_has_no_external_urls(env):
    data = manifest()
    blob = json.dumps(data)
    assert "http://" not in blob and "https://" not in blob
    for icon in data["icons"]:
        assert icon["src"].startswith("/")


def test_t088_theme_init_script_present(env):
    _s, _h, text = _call_text("GET", "/", cookie=_VID)
    assert "secdiff_theme=(light|dark)" in text


def test_t089_theme_init_script_on_report(env, monkeypatch):
    _stub_pipeline(monkeypatch, {"AAPL": 320193},
                   {"AAPL": _anchor_result()})
    _s, _h, text = _call_text("POST", "/report", body=b"ticker=AAPL",
                              cookie=_VID)
    assert "secdiff_theme=(light|dark)" in text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in text


def test_t090_no_external_urls_in_theme_or_icons(env):
    assert "http" not in SITE_CSS and "http" not in REPORT_CSS
    _s, _h, raw = _call("GET", "/manifest.webmanifest", cookie=_VID)
    assert b"http" not in raw


def test_t091_pages_use_system_font_stack_only(env):
    for css in (SITE_CSS, REPORT_CSS):
        assert "-apple-system" in css
        assert "@font-face" not in css


# ---------------------------------------------------------------------------
# G. Anchor regressions — presentation changed nothing (T092–T100)

def test_t092_api_anchor_counts_and_score(env, monkeypatch):
    _stub_pipeline(monkeypatch, {"AAPL": 320193},
                   {"AAPL": _anchor_result()})
    status, _h, raw = _call("GET", "/api/report", query="ticker=AAPL",
                            cookie=_VID)
    assert status == "200 OK"
    payload = json.loads(raw)
    assert payload["counts"] == {"deletions": 155, "modifications": 162,
                                 "additions": 50, "xbrl_changed": 75}
    assert payload["score"]["total"] == 80


def test_t093_report_html_anchor_counts(env, monkeypatch):
    _stub_pipeline(monkeypatch, {"AAPL": 320193},
                   {"AAPL": _anchor_result()})
    status, _h, text = _call_text("POST", "/report",
                                  body=b"ticker=AAPL", cookie=_VID)
    assert status == "200 OK"
    assert ("155 deletions · 162 modifications · 50 additions · "
            "75 XBRL numbers changed") in text
    assert "80/100" in text


def test_t094_report_chrome_uses_classes_not_inline_styles(env,
                                                          monkeypatch):
    _stub_pipeline(monkeypatch, {"AAPL": 320193},
                   {"AAPL": _anchor_result()})
    _s, _h, text = _call_text("POST", "/report", body=b"ticker=AAPL",
                              cookie=_VID)
    assert "class='sharebox'" in text
    assert "class='reporttools'" in text
    assert "background:#f3f4f6" not in text
    assert "background:#fffbeb" not in text


def test_t095_report_numbers_table_wrapped(env, monkeypatch):
    _stub_pipeline(monkeypatch, {"AAPL": 320193},
                   {"AAPL": _anchor_result()})
    _s, _h, text = _call_text("POST", "/report", body=b"ticker=AAPL",
                              cookie=_VID)
    assert "<div class='tablewrap'><table>" in text


def test_t096_demo_still_404(env):
    status, _h, _t = _call_text("GET", "/demo", cookie=_VID)
    assert status.startswith("404")


def test_t097_examples_still_404(env):
    status, _h, _t = _call_text("GET", "/examples", cookie=_VID)
    assert status.startswith("404")


def test_t098_stats_still_owner_only(env):
    status, _h, _t = _call_text("GET", "/stats", cookie=_VID)
    assert status.startswith("404")


def test_t099_paywall_still_at_fifth_company(env, monkeypatch):
    results = {}
    resolve = {}
    for i, ticker in enumerate(("AAA", "BBB", "CCC", "DDD", "EEE")):
        cik = 5000 + i
        resolve[ticker] = cik
        info, diffs, xbrl = _anchor_result()
        results[ticker] = (_info(ticker=ticker, cik=cik), diffs, xbrl)
    _stub_pipeline(monkeypatch, resolve, results)
    store = web.get_usage_store()
    vid = _VID.split("=", 1)[1]
    for i in range(4):
        assert store.check_and_add(vid, 5000 + i)
    status, _h, text = _call_text("POST", "/report", body=b"ticker=EEE",
                                  cookie=_VID)
    assert status.startswith("402")
    assert "You've used today's 4 free companies" in text
    assert "$9" in text and "$27" in text


def test_t100_home_keeps_pricing_and_biggest_sections(env):
    _s, _h, text = _call_text("GET", "/", cookie=_VID)
    assert "Biggest deletes" in text
    assert "$9" in text and "$27" in text
    assert "4 companies per day" in text

