"""The secdiff web design system: one palette, two themes, no assets.

Phase 5 is presentation only. This module is the single source of the
site's look:

- `PALETTES` holds the light and dark colour palettes as plain hex
  values. Both the stylesheets AND the test suite's WCAG contrast
  checks are generated/computed from these dicts, so the colours a
  visitor sees are exactly the colours the tests verified.
- `SITE_CSS` styles the web.py pages; `REPORT_CSS` styles the report
  pages (report.py). Every colour in both goes through a CSS custom
  property, so a theme is just a variable swap — the diff colours
  (red deletions, amber modifications, green additions) keep their
  meaning in both themes.
- Theme selection: a `secdiff_theme` cookie (set by the POST /theme
  toggle) wins and is rendered server-side as a `data-theme`
  attribute on <html>. With no cookie there is NO attribute, and a
  `@media (prefers-color-scheme: dark)` block applies the dark
  palette unless the visitor explicitly chose light — i.e. the first
  visit respects the OS setting. A tiny inline script re-applies the
  cookie client-side before paint as a belt-and-braces measure.
- No external fonts, CDNs, frameworks or images: the system font
  stack only, one inline stylesheet per page, generated icons.

PWA note — why there is deliberately NO service worker: every page
on this site is generated live and is personalised (daily-limit
accounting, account pages, the owner-only stats page). A service
worker cache could serve a stale report or, worse, another state of
an account page; the manifest + generated icons below give visitors
"add to home screen" without that risk. If offline support is ever
wanted, it must be network-first for every HTML page — that decision
belongs to a future phase, not this one.
"""
from __future__ import annotations

import html

THEME_COOKIE = "secdiff_theme"
THEMES = ("light", "dark")

# ---------------------------------------------------------------------------
# Palettes (hex). Keys are CSS custom-property names (without the --).

PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#ffffff",
        "surface": "#f3f4f6",
        "surface-2": "#eef2ff",
        "text": "#1a1a2e",
        "muted": "#5f6875",
        "border": "#e5e7eb",
        "border-strong": "#d1d5db",
        "accent": "#2563eb",
        "accent-text": "#ffffff",
        "link": "#2563eb",
        "del-bg": "#fef2f2",
        "del-border": "#dc2626",
        "add-bg": "#f0fdf4",
        "add-border": "#16a34a",
        "mod-bg": "#fffbeb",
        "mod-border": "#d97706",
        "err-bg": "#fef2f2",
        "ok-bg": "#f0fdf4",
        "code-bg": "#eef2ff",
        "mark-risk": "#fde68a",
        "mark-search": "#bfdbfe",
        "mark-search-ring": "#60a5fa",
        "score-bg": "#eef2ff",
        "score-border": "#c7d2fe",
        "repeat-bg": "#f5f3ff",
        "repeat-border": "#8b5cf6",
        "repeat-text": "#4c1d95",
        "note-bg": "#fffbeb",
        "banner-bg": "#eff6ff",
        "banner-border": "#bfdbfe",
        "pos": "#15803d",
        "neg": "#dc2626",
        "chart-bar": "#2563eb",
        "chart-neg": "#dc2626",
        "chart-text": "#1a1a2e",
        "chart-muted": "#5f6875",
        "chart-axis": "#9ca3af",
        "input-bg": "#ffffff",
    },
    "dark": {
        "bg": "#0b1220",
        "surface": "#131f36",
        "surface-2": "#182a4a",
        "text": "#e6ebf4",
        "muted": "#9dabc2",
        "border": "#263750",
        "border-strong": "#3a5070",
        "accent": "#2563eb",
        "accent-text": "#ffffff",
        "link": "#8ab8ff",
        "del-bg": "#3d1a20",
        "del-border": "#f87171",
        "add-bg": "#12301e",
        "add-border": "#4ade80",
        "mod-bg": "#38290e",
        "mod-border": "#fbbf24",
        "err-bg": "#3d1a20",
        "ok-bg": "#12301e",
        "code-bg": "#1b2b47",
        "mark-risk": "#6e5510",
        "mark-search": "#2456a6",
        "mark-search-ring": "#60a5fa",
        "score-bg": "#182a4a",
        "score-border": "#2e4a7a",
        "repeat-bg": "#2a2145",
        "repeat-border": "#a78bfa",
        "repeat-text": "#ddd0ff",
        "note-bg": "#38290e",
        "banner-bg": "#14263f",
        "banner-border": "#2e4a7a",
        "pos": "#4ade80",
        "neg": "#f87171",
        "chart-bar": "#5b93ff",
        "chart-neg": "#f87171",
        "chart-text": "#e6ebf4",
        "chart-muted": "#9dabc2",
        "chart-axis": "#5b6b84",
        "input-bg": "#0e1930",
    },
}


def _var_block(palette: dict[str, str]) -> str:
    return ";".join(f"--{name}:{value}"
                    for name, value in palette.items())


# The theme layer shared by both stylesheets: explicit choice first
# (data-theme attribute), OS preference only when no explicit light
# choice exists.
VARIABLES_CSS = (
    f":root{{{_var_block(PALETTES['light'])}}}"
    f"[data-theme=\"dark\"]{{{_var_block(PALETTES['dark'])}}}"
    "@media (prefers-color-scheme: dark){"
    f":root:not([data-theme=\"light\"]){{{_var_block(PALETTES['dark'])}}}}}"
)


# ---------------------------------------------------------------------------
# Contrast helpers (used by the test suite; WCAG 2.x relative luminance)

def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    """WCAG contrast ratio between two hex colours (1.0–21.0)."""
    la, lb = _luminance(fg_hex), _luminance(bg_hex)
    lo, hi = min(la, lb), max(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------------------
# HTML helpers

def clean_theme(value: str | None) -> str | None:
    """A visitor's stored theme choice, or None when unset/invalid."""
    return value if value in THEMES else None


def html_tag(theme: str | None) -> str:
    """The opening <html> tag. No data-theme attribute when the
    visitor has made no explicit choice — the CSS media query then
    supplies the OS preference."""
    if theme in THEMES:
        return f'<html lang="en" data-theme="{theme}">'
    return '<html lang="en">'


# Re-apply the cookie before first paint (covers pages rendered
# without the attribute, e.g. a cached shell). Reads only the theme
# cookie; never blocks rendering.
THEME_INIT_JS = (
    "<script>(function(){try{"
    "var m=document.cookie.match(/(?:^|; )secdiff_theme=(light|dark)/);"
    "if(m){document.documentElement.setAttribute('data-theme',m[1]);}"
    "}catch(e){}})();</script>"
)


def head_links(theme: str | None) -> str:
    """The <head> extras shared by every page: theme colours (light
    and dark variants), the PWA manifest and the generated icons."""
    light_bg = PALETTES["light"]["bg"]
    dark_bg = PALETTES["dark"]["bg"]
    return (
        f'<meta name="theme-color" media="(prefers-color-scheme: light)" '
        f'content="{light_bg}">'
        f'<meta name="theme-color" media="(prefers-color-scheme: dark)" '
        f'content="{dark_bg}">'
        '<link rel="manifest" href="/manifest.webmanifest">'
        '<link rel="icon" type="image/png" sizes="192x192" '
        'href="/icon-192.png">'
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
    )


def head_block(title: str, theme: str | None, css: str) -> str:
    """A complete <head> for a web.py page: charset, mobile viewport,
    title, PWA/theme links, the one inline stylesheet, theme init."""
    return (
        "<head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" "
        "content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title>"
        f"{head_links(theme)}"
        f"<style>{css}</style>"
        f"{THEME_INIT_JS}</head>"
    )


# ---------------------------------------------------------------------------
# The site stylesheet (web.py pages)

SITE_CSS = VARIABLES_CSS + """
*{box-sizing:border-box}
body{font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
margin:0 auto;max-width:920px;padding:1.6rem 1rem 3rem;
background:var(--bg);color:var(--text);line-height:1.55}
h1{font-size:1.9rem;margin:.2rem 0}
h2{margin-top:2.2rem;border-bottom:2px solid var(--border);
padding-bottom:.3rem}
a{color:var(--link)}
.tagline{font-size:1.1rem;color:var(--text)}
.muted,.note{color:var(--muted);font-size:.92rem}
.card{background:var(--surface);border-radius:10px;
padding:1.2rem 1.4rem;margin:1.2rem 0}
label{display:block;font-weight:600;margin:.7rem 0 .2rem}
label.check{font-weight:400}
input[type=text],input[type=number],input[type=email],
input[type=password],select{font-size:1rem;padding:.5rem .6rem;
border:1px solid var(--border-strong);border-radius:6px;width:100%;
background:var(--input-bg);color:var(--text)}
.topbar{display:flex;justify-content:space-between;align-items:center;
gap:.6rem 1rem;flex-wrap:wrap;margin-bottom:1.4rem;
padding-bottom:.8rem;border-bottom:2px solid var(--border)}
.topbar .brand{font-weight:800;font-size:1.3rem;text-decoration:none;
color:var(--text)}
.topbar nav{display:flex;gap:.4rem .9rem;align-items:center;flex-wrap:wrap}
.topbar nav a{padding:.3rem .1rem}
.topbar nav .btn{margin-top:0;padding:.45rem .95rem}
.linkbtn{background:none;border:0;color:var(--link);cursor:pointer;
font-size:1rem;padding:0;text-decoration:underline;margin-top:0;
font-weight:400}
.themeform{display:inline-flex;gap:.3rem;margin:0}
.themebtn{background:var(--surface);color:var(--text);
border:1px solid var(--border-strong);border-radius:6px;
font-size:.85rem;font-weight:600;padding:.35rem .6rem;margin-top:0;
cursor:pointer}
.themebtn.on{border-color:var(--accent);color:var(--link);
background:var(--surface-2)}
.ok{background:var(--ok-bg);border-left:4px solid var(--add-border);
border-radius:6px;padding:1rem 1.2rem;margin:1.2rem 0}
.err{background:var(--err-bg);border-left:4px solid var(--del-border);
border-radius:6px;padding:1rem 1.2rem;margin:1.2rem 0}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch;
max-width:100%}
table.kv{border-collapse:collapse;margin:1rem 0}
table.kv td{padding:.35rem 1.2rem .35rem 0;vertical-align:top}
table.kv td:first-child{font-weight:600;color:var(--muted)}
table.data{border-collapse:collapse;margin:1rem 0}
table.data th,table.data td{padding:.35rem 1rem .35rem 0;text-align:left}
table.data th{border-bottom:2px solid var(--border);color:var(--muted)}
table.data td{border-bottom:1px solid var(--surface)}
button,.btn{display:inline-block;background:var(--accent);
color:var(--accent-text);border:0;font-size:1rem;font-weight:600;
padding:.65rem 1.3rem;border-radius:8px;cursor:pointer;
text-decoration:none;margin-top:1rem}
button:disabled{opacity:.55;cursor:wait}
.btn.secondary{background:var(--text);color:var(--bg)}
.row{display:grid;grid-template-columns:1fr 160px;gap:1rem}
@media (max-width:560px){.row{grid-template-columns:1fr}}
.spinner{width:34px;height:34px;border-radius:50%;
border:4px solid var(--surface-2);border-top-color:var(--accent);
animation:spin 0.9s linear infinite;margin:.4rem 0}
@keyframes spin{to{transform:rotate(360deg)}}
.plans{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;
margin:1.2rem 0}
@media (max-width:700px){.plans{grid-template-columns:1fr}}
.plan{background:var(--bg);border:2px solid var(--border);
border-radius:10px;padding:1.2rem 1.3rem}
.plan.hot{border-color:var(--accent)}
.plan h3{margin:.1rem 0 .4rem}
.price{font-size:1.7rem;font-weight:700}
.price span{font-size:.95rem;font-weight:400;color:var(--muted)}
.plan ul{margin:.6rem 0;padding-left:1.15rem}
.plan .btn{margin-top:.6rem}
footer.sitefoot{margin-top:3rem;border-top:2px solid var(--border);
padding-top:1rem}
.footnav{display:flex;gap:.4rem 1rem;flex-wrap:wrap;margin-bottom:.6rem}
code{background:var(--code-bg);color:var(--text);
padding:.1rem .35rem;border-radius:4px}
@media (max-width:700px){
body{padding:1rem .8rem 2.4rem}
h1{font-size:1.55rem}
button,.btn{min-height:44px}
.topbar nav a{min-height:40px;display:inline-flex;align-items:center}
.themebtn{min-height:40px}
}
"""

# ---------------------------------------------------------------------------
# The report stylesheet (report.py pages + standalone CLI HTML files).
# Same variables, so a shared report link renders in the reader's
# theme; standalone files (no data-theme) follow the OS preference.

REPORT_CSS = VARIABLES_CSS + """
*{box-sizing:border-box}
body{font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
margin:2rem auto;max-width:920px;padding:0 1rem;
background:var(--bg);color:var(--text);line-height:1.5}
h1{font-size:1.6rem}
h2{margin-top:2.2rem;border-bottom:2px solid var(--border);
padding-bottom:.3rem}
a{color:var(--link)}
.meta{background:var(--surface);border-radius:8px;padding:1rem 1.2rem}
.del{background:var(--del-bg);border-left:4px solid var(--del-border);
padding:.6rem .9rem;margin:.5rem 0}
.add{background:var(--add-bg);border-left:4px solid var(--add-border);
padding:.6rem .9rem;margin:.5rem 0}
.mod{background:var(--mod-bg);border-left:4px solid var(--mod-border);
padding:.6rem .9rem;margin:.5rem 0}
.tag{font-weight:700;font-size:.75rem;letter-spacing:.05em}
.repeat-note{background:var(--repeat-bg);
border-left:4px solid var(--repeat-border);
padding:.35rem .9rem;margin:-.3rem 0 .5rem;font-size:.88rem;
color:var(--repeat-text)}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch;
max-width:100%}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid var(--border);padding:.45rem .6rem;text-align:right}
th:first-child,td:first-child{text-align:left}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.note{color:var(--muted);font-size:.9rem}
.score{background:var(--score-bg);border:1px solid var(--score-border);
border-radius:8px;padding:.7rem 1rem;margin-top:.8rem}
.score .big{font-size:1.5rem;font-weight:800}
.score .band{font-weight:700}
.score-how{margin-top:.5rem;font-size:.9rem;color:var(--text)}
.score-how summary{cursor:pointer;font-weight:600}
mark.risk{background:var(--mark-risk);color:inherit;
padding:0 .12em;border-radius:3px}
mark.search{background:var(--mark-search);color:inherit;
padding:0 .12em;border-radius:3px;
box-shadow:inset 0 0 0 1px var(--mark-search-ring)}
.searchbanner{background:var(--banner-bg);
border:1px solid var(--banner-border);border-radius:8px;
padding:.7rem 1rem;margin-top:.8rem}
.reporttools{max-width:920px;margin:1rem auto 0;padding:0 1rem}
.themeform{display:inline-flex;gap:.3rem;margin:0 0 0 .8rem;
vertical-align:middle}
.themebtn{background:var(--surface);color:var(--text);
border:1px solid var(--border-strong);border-radius:6px;
font-size:.85rem;font-weight:600;padding:.35rem .6rem;cursor:pointer}
.themebtn.on{border-color:var(--accent);color:var(--link);
background:var(--surface-2)}
.sharebox{max-width:920px;margin:.6rem auto 0;padding:.6rem 1rem;
background:var(--surface);border-radius:6px;font-size:.92rem}
.sharebox input[type=text]{width:min(480px,90%);font-size:.9rem;
padding:.3rem .45rem;border:1px solid var(--border-strong);
border-radius:6px;background:var(--input-bg);color:var(--text)}
.sharebox button{margin-top:0;padding:.35rem .8rem;font-size:.9rem;
background:var(--accent);color:var(--accent-text);border:0;
font-weight:600;border-radius:8px;cursor:pointer}
.sharebox form{margin-top:.6rem}
.sharebox .qinput{width:min(300px,80%)}
.notesbox{max-width:920px;margin:.6rem auto 0;padding:.6rem 1rem;
background:var(--note-bg);border-left:4px solid var(--mod-border);
border-radius:4px;font-size:.9rem}
.chart{margin:1.2rem 0}
.chart h3{margin:.4rem 0}
.chart svg{max-width:100%;height:auto}
.chart rect.cb{fill:var(--chart-bar)}
.chart rect.cbn{fill:var(--chart-neg)}
.chart text.ct{fill:var(--chart-text)}
.chart text.cl{fill:var(--chart-muted)}
.chart line.ca{stroke:var(--chart-axis)}
@media (max-width:700px){body{margin:1rem auto;padding:0 .8rem}
h1{font-size:1.35rem}}
"""
