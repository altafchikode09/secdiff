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

Host/port: `--host` / `--port` flags win, then the `HOST` / `PORT`
environment variables (the convention Render/Railway/Heroku use). If
`PORT` is set and `HOST` is not, the server binds `0.0.0.0` so a
platform router can reach it; otherwise it binds `127.0.0.1`.

The module also exposes a plain WSGI `application`, so it can be mounted
under any WSGI server (`gunicorn secdiff.web:application`) unchanged.
"""
from __future__ import annotations

import argparse
import html
import io
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
from contextlib import redirect_stderr
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from .cli import live_pipeline, one_line_summary, render_reports

__all__ = ["application", "main", "serve", "run_report",
           "parse_report_form", "ReportParams", "home_page", "error_page"]


# ---------------------------------------------------------------------------
# Form parameters (pure, unit-tested offline)

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")
_FORMS = ("10-K", "10-Q")


@dataclass
class ReportParams:
    ticker: str                      # upper-cased ticker symbol
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

    ticker = first("ticker").upper()
    if not ticker:
        raise ValueError("Please enter a ticker symbol (for example AAPL).")
    if not _TICKER_RE.match(ticker):
        raise ValueError(
            f"“{ticker}” does not look like a ticker symbol. Use letters, "
            "digits, '.' or '-' (e.g. AAPL, BRK.B).")
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


def run_report(params: ReportParams) -> tuple[str, str]:
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
    return summary, _wrap_report(page, notes.getvalue().strip())


def _wrap_report(report_html: str, notes: str) -> str:
    """Add a way back home (and any honest stderr notes, e.g. the ticker
    resolved to a different EDGAR entity) to the shared report page."""
    nav = ("<div style='max-width:920px;margin:1rem auto 0;padding:0 1rem'>"
           "<a href='/'>&larr; New secdiff search</a></div>")
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
input[type=text],input[type=number],select{font-size:1rem;padding:.5rem .6rem;
border:1px solid #d1d5db;border-radius:6px;width:100%;box-sizing:border-box}
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


def home_page() -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>secdiff — git diff for SEC filings</title><style>{_CSS}</style></head>
<body>
<h1>secdiff</h1>
<p class="tagline"><strong><code>git diff</code> for SEC filings.</strong>
Type a ticker and secdiff fetches the company's latest filing from SEC
EDGAR, fetches the one before it, and shows you exactly what changed —
words and numbers — with <strong>deleted language first</strong>, because
what a company <em>stops saying</em> is often the story.</p>

<div class="card">
<form method="post" action="/report" id="diff-form">
  <div class="row">
    <div>
      <label for="ticker">Ticker symbol</label>
      <input type="text" id="ticker" name="ticker" required maxlength="12"
             placeholder="AAPL" autocomplete="off" autocapitalize="characters">
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

<footer>
<p class="note"><strong>Not investment advice.</strong> secdiff shows
what changed in a public document; it makes no recommendations and has
no buy/sell outputs, price data or predictions. Data: SEC EDGAR (public
filings). secdiff is free and open-source (MIT) — and
<code>secdiff</code> is a working title.</p>
</footer>
<script>{_HOME_JS}</script>
</body></html>
"""


def error_page(title: str, message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>secdiff — {html.escape(title)}</title><style>{_CSS}</style></head>
<body>
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
# WSGI application (also the core the stdlib server dispatches to)

def _respond(start_response, status: str, body,
             content_type: str = "text/html; charset=utf-8"):
    data = body.encode("utf-8") if isinstance(body, str) else body
    start_response(status, [("Content-Type", content_type),
                            ("Content-Length", str(len(data)))])
    return [data]


def _error(start_response, status: str, title: str, message: str):
    return _respond(start_response, status, error_page(title, message))


def application(environ, start_response):
    """WSGI app: / (search form), /report (POST form or GET query),
    /healthz. All other paths 404; all failures are honest error pages."""
    method = (environ.get("REQUEST_METHOD") or "GET").upper()
    path = environ.get("PATH_INFO") or "/"
    try:
        if path in ("/", "/index.html"):
            if method not in ("GET", "HEAD"):
                return _error(start_response, "405 Method Not Allowed",
                              "Method not allowed", "Use the search form.")
            return _respond(start_response, "200 OK", home_page())
        if path == "/healthz":
            return _respond(start_response, "200 OK", "ok\n",
                            "text/plain; charset=utf-8")
        if path == "/report":
            if method == "POST":
                try:
                    length = int(environ.get("CONTENT_LENGTH") or 0)
                except ValueError:
                    length = 0
                raw = environ["wsgi.input"].read(length).decode("utf-8", "replace")
            elif method in ("GET", "HEAD"):
                raw = environ.get("QUERY_STRING", "")
            else:
                return _error(start_response, "405 Method Not Allowed",
                              "Method not allowed", "Use the search form.")
            params = parse_report_form(raw)
            _summary, page = run_report(params)
            return _respond(start_response, "200 OK", page)
        return _error(start_response, "404 Not Found", "Page not found",
                      f"There is no page at {path}.")
    except ValueError as exc:                       # bad form input
        return _error(start_response, "400 Bad Request",
                      "Check the form", str(exc))
    except SystemExit as exc:                       # pipeline's honest errors
        msg = str(exc.code) if exc.code else "The request could not be completed."
        return _error(start_response, "400 Bad Request",
                      "No report could be built", msg)
    except urllib.error.URLError as exc:           # EDGAR unreachable
        reason = getattr(exc, "reason", exc)
        return _error(start_response, "502 Bad Gateway",
                      "SEC EDGAR could not be reached",
                      f"EDGAR did not answer ({reason}). Nothing was fetched, "
                      "so there is no report to show — please try again in "
                      "a moment.")
    except Exception as exc:                        # never leak a stack trace
        return _error(start_response, "500 Internal Server Error",
                      "Something went wrong",
                      f"Building the report failed: {exc}")


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
