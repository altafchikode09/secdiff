# secdiff — web version

A ready-to-use website for secdiff: a visitor opens the page, types a
ticker, and gets the filing-diff report in the browser. The site is part
of this repo — `src/secdiff/web.py`, standard library only, reusing the
exact same diff pipeline as the CLI (no duplicated logic).

## Run it locally

```bash
pip install .                 # from the repo root (zero dependencies)
export EDGAR_EMAIL=you@example.com   # the SEC asks for a real contact
python -m secdiff.web         # or: secdiff serve   |   secdiff-web
```

Open **http://127.0.0.1:8000**

- Type a ticker (e.g. `AAPL`), pick 10-K or 10-Q, optionally tick
  *Risk factors only* / *Deletions only* or set *Top N*, and submit.
- Every report is generated **live** from real SEC EDGAR filings at
  request time — there is no demo, sample or pre-generated report
  anywhere on the site.
- First fetch of a company can take up to a minute (two full filings +
  XBRL, paced under the SEC fair-access limit); EDGAR responses are
  cached in `~/.cache/secdiff` (override with `SECDIFF_CACHE_DIR`), so
  repeat visits are fast.
- Unknown tickers and EDGAR outages get an honest error page — the site
  never shows invented data.
- On a report page: **search in this report** (`?q=` — only the
  changes mentioning a word or phrase, counted honestly, applied after
  the other filters), a **share link** that reproduces the exact same
  report, a **summary PDF** download (`/report.pdf`), the XBRL numbers
  as CSV/Excel (`/report.csv`, `/report.xls`), and a **share card**
  image (`/card`, a 1200×630 PNG with the counts, the
  change-intensity score and the biggest deletion). Downloads and the
  card follow the same 4-companies-a-day accounting as reports.

## Configuration

| Setting | How | Default |
|---|---|---|
| Port | `--port` flag or `PORT` env | `8000` |
| Host | `--host` flag or `HOST` env | `127.0.0.1` locally; `0.0.0.0` when `PORT` is set |
| EDGAR contact email | `EDGAR_EMAIL` env | placeholder (SEC may throttle; set a real one) |
| Cache directory | `SECDIFF_CACHE_DIR` env | `~/.cache/secdiff` |

## Deploy it (deploy-ready, nothing published yet)

The repo root carries a `Procfile` (`web: python -m secdiff.web`), so
typical Python hosts work as-is:

- **Render / Railway:** create a web service from this repo; build
  command `pip install .`, start command `python -m secdiff.web` (or
  let the Procfile supply it). Set the env var `EDGAR_EMAIL` to a real
  contact address. `PORT` is provided by the platform and picked up
  automatically.
- **VPS:** `pip install .`, set `EDGAR_EMAIL`, and run
  `PORT=8000 python -m secdiff.web` under systemd/supervisor, behind
  nginx/Caddy if you want TLS.
- **WSGI instead:** the module exposes `application`, so
  `gunicorn secdiff.web:application` works if you prefer a WSGI server
  (gunicorn itself is the only thing you'd add).

Notes for production: report runs are serialised server-side (EDGAR
fair access + shared disk cache), so one small instance is the right
shape; a persistent disk for the cache directory avoids re-fetching
filings after restarts.
