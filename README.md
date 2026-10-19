# secdiff

**`git diff` for SEC filings.** Give it a ticker; it fetches the company's
latest 10-K from SEC EDGAR, fetches the prior 10-K, and shows you exactly
what changed — **words and numbers** — with deleted language first, because
what a company *stops saying* is often the story.

Free, open-source (MIT), local, no account, no API key. The commercial
versions of this job exist (filing "redline" / "blackline" tools inside
paid terminals); a free open-source one did not — so here it is.

> ⚠️ **Name note:** `secdiff` is a **working title**. Its GitHub-org,
> domain and trademark availability were **not** cleared during research —
> run those checks before any public launch. (The name "Filing Delta" is
> already taken by a paid service; do not use it.)

## Install

```bash
pip install .            # from this repo (zero runtime dependencies)
# or, once published: pip install secdiff
```

Python 3.10+. secdiff uses **only the Python standard library** — there is
no dependency tree to break.

Set your contact email once (the SEC requires a declared User-Agent):

```bash
export EDGAR_EMAIL=you@example.com
```

## Quick start — the one-command shortcut

```bash
secdiff AAPL
```

That does everything: resolves the ticker on EDGAR, downloads and caches
the two most recent 10-Ks, splits them into sections, computes the diff,
builds the XBRL numbers table, and writes two reports:

```
AAPL 10-K: 155 deletions, 162 modifications, 50 additions, 75 XBRL numbers changed (2024-11-01 -> 2025-10-31)
HTML report: /path/to/secdiff-out/secdiff-aapl-10-k.html
Markdown report: /path/to/secdiff-out/secdiff-aapl-10-k.md
```

(Those are the real counts from the bundled Apple demo below.)

## Try it offline in seconds — `secdiff demo`

```bash
secdiff demo
```

No network, no email, no setup. The demo uses **real filings bundled with
the project**: Apple Inc.'s 10-K filed 2025-10-31 (accession
`0000320193-25-000079`) vs its 10-K filed 2024-11-01 (accession
`0000320193-24-000123`), fetched from SEC EDGAR on 2026-10-17, plus the
matching slice of SEC XBRL company-facts. A pre-generated copy of the demo
report lives in [`examples/demo-aapl-10k/`](examples/demo-aapl-10k/).

Real numbers from that demo report:

| Item | Older filing | Newer filing | Change |
|---|---|---|---|
| Revenue | $391.04B | $416.16B | +6.4% |
| Net income | $93.74B | $112.01B | +19.5% |
| EPS (diluted) | $6.08 | $7.46 | +22.7% |
| Long-term debt (noncurrent) | $85.75B | $78.33B | −8.7% |

The full demo report carries **75 matched XBRL numbers** for AAPL (of the
100 tracked concepts — see "XBRL coverage" below), not just these headlines.

## Web version — ready to use in the browser

The same tool also runs as a small website (standard library only, in
`src/secdiff/web.py`, reusing the exact same pipeline as the CLI):

```bash
pip install .
export EDGAR_EMAIL=you@example.com
python -m secdiff.web        # or: secdiff serve  |  secdiff-web
```

Open **http://127.0.0.1:8000** — type a ticker, pick a form (10-K,
10-Q, 8-K or proxy DEF 14A), optionally tick *Risk factors only* /
*Deletions only*, set *Top N*, or pick two filing years to compare
those exact filings, and the report renders in the browser. Reports
with an XBRL numbers table offer CSV and Excel downloads of that table
(the exact values shown on the page). Every web report is generated
live from real SEC EDGAR filings at request time — there is no demo or
pre-generated report on the website. Unknown tickers and EDGAR outages
produce an honest error page, never fake data.

Configuration is by flags or environment: `--port`/`PORT`,
`--host`/`HOST` (binds `0.0.0.0` automatically when `PORT` is set, as
on Render/Railway), `EDGAR_EMAIL`, and `SECDIFF_CACHE_DIR` (defaults to
the CLI's `~/.cache/secdiff`, so the web app and the CLI share one
cache). The repo root carries a `Procfile`
(`web: python -m secdiff.web`), so it deploys as-is on a typical Python
host; see [`web/README.md`](web/README.md) for local-run and deploy
steps. First fetch of a company can take up to a minute (two filings +
XBRL, paced under SEC fair access); repeat visits are cached and fast.

## Shortcuts

| Shortcut | What it does |
|---|---|
| `secdiff TICKER` | Zero-config quick mode: fetch → diff → HTML + Markdown reports + one-line terminal summary |
| `secdiff demo` | Offline demo on bundled real filings |
| `--form 10-Q` | Compare 10-Qs instead of 10-Ks (compares the two most recent 10-Qs — consecutive quarters, see Limitations) |
| `--form 8-K` | Compare the two most recent 8-K event filings (labelled in the report: 8-Ks cover different events, so additions are expected) |
| `--form "DEF 14A"` | Compare the latest proxy statement with the prior year's (words only — proxies carry no XBRL numbers table, and the report says so) |
| `--vs-year Y` / `--base-year Y` | Compare two exact filing years (e.g. `--vs-year 2025 --base-year 2022`); years with no such filing are an honest error listing the years that exist |
| `--export-csv PATH` | Also write the XBRL numbers table to PATH as CSV — exactly the values shown in the report |
| `--risk-only` | Only Item 1A — Risk Factors |
| `--deletions-only` | Only removed text — the headline use case |
| `--top N` | Only the N biggest changes overall (sections ranked by change size) |
| `--section NAME` | One section only: `risk`, `mdna`, `business`, `legal`, `financials`, `controls`, or an item number like `1A` / `7` (10-Q numbering is handled: in a 10-Q, `mdna` is Item 2) |
| `--years N` | Compare against the filing N years back (`--years 2` = latest vs the 10-K from two filings ago) |
| `--output DIR` | Where the reports go (default `./secdiff-out`) |
| `--summarize` | Add a summary **of the already-computed diff** (see below) |
| `--email you@x.com` | Contact email for the EDGAR User-Agent (or set `EDGAR_EMAIL`) |
| `--cache-dir DIR` | Download cache location (default `~/.cache/secdiff`) |

Examples:

```bash
secdiff NVDA --risk-only --deletions-only        # what risk language did Nvidia drop?
secdiff MSFT --form 10-Q --section mdna --top 10 # biggest MD&A changes last quarter
secdiff TSLA --years 2 --summarize               # two-year diff, summarised
```

## How the diff works (and why you can trust it)

1. **Retrieval** — a small built-in EDGAR client (ticker → CIK →
   submissions → filing document → XBRL company-facts). Downloads are
   cached on disk and paced well under the SEC's fair-access limit.
   Ticker resolution is verified, not blind: EDGAR's ticker files can
   point a ticker at a related entity that never files the form you
   asked for (e.g. XOM → ExxonMobil Holdings Corp, which files no 10-Ks),
   so a candidate is accepted only once its own submission history
   proves it files that form; otherwise the other EDGAR ticker sources
   and the classic browse-edgar lookup are tried, and secdiff says so
   when it switches entity.
2. **Section split** — filings are split into ITEM sections (1, 1A, 7, 8…).
   Detection is heuristic — inline-XBRL filings are messy — so every
   report carries a **section-detection confidence** flag, and sections
   that cannot be found are reported as missing, never guessed.
3. **Text diff** — deterministic, sentence-level. Changes are classified
   as **deletions / modifications / additions**, ranked by size of change,
   and shown **deletions first**. Same input → same output, every time.
   No LLM is involved in computing the diff.
4. **XBRL delta** — values are matched **per filing by accession number**,
   so each column is exactly what that filing reported. A concept is only
   compared when *both* filings report the *same* concept. Concepts that
   appear in only one filing are almost always retags: they are **listed
   separately as unmapped and never silently merged** or treated as zero.
   The matcher tracks **100 curated concepts** and was hardened against
   real filings — see the next section.
5. **Citations** — every report header and every section links to both
   EDGAR filings (accession number, filing date, direct document URL), so
   any change can be verified against the source in one click.

## XBRL coverage — 100 tracked concepts

The tracked catalogue lives in
[`src/secdiff/concepts.py`](src/secdiff/concepts.py): 100 curated concepts
— income statement (27), balance sheet (46), cash flow (22) and
per-share/other (5) — each with the real us-gaap tag spellings filers
actually use (plus `dei` for share counts), a kind (fiscal-year *flow* vs
period-end *stock*) and an expected unit.

### How matching works

- **Per filing, by accession number** — each column is what that filing
  itself reported, never a restated value borrowed from a newer filing.
- **Alias scoring across both filings** — an alias reported by *both*
  filings beats a higher-priority alias reported by only one; a concept
  whose tag genuinely changed between filings (a retag) stays unmapped
  and is listed, never merged.
- **Unit series chosen by expected unit kind** (money → USD, shares →
  shares, per-share → USD/shares) — never by dictionary order.
- **Flow concepts require a fiscal-year-duration fact** (~300–400 days)
  in that filing. If none exists, the concept is reported as not matched
  with the reason kept — a quarter is never dressed up as a year.
- **Conflicting values are refused, not guessed** — if several different
  values survive for the target period, `fp="FY"` facts break the tie;
  otherwise the concept is marked ambiguous and excluded.

### Measured coverage (real 10-K pairs, verified 2026-10-17)

| Company (latest two 10-Ks) | Concepts matched | Large-concept misses |
|---|---|---|
| Apple (AAPL) | **75 / 100** | none |
| Microsoft (MSFT) | **77 / 100** | none |
| Exxon Mobil (XOM) | **67 / 100** | cost of revenue, gross profit, operating income — not presented (below) |
| Walmart (WMT) | **74 / 100** | gross profit, total liabilities — not tagged with standard tags (below) |

Unmatched concepts are overwhelmingly lines a company genuinely does not
present or does not tag (no goodwill at Apple or Exxon, no inventory at a
bank, …) — not matcher failures. The matcher was built by hunting misses
on these four filers over two find-and-fix cycles; the bugs found and
fixed included:

- **Unit-series shadowing (large miss):** Walmart's EPS facts carry an
  empty `pure` unit series ahead of the real `USD/shares` series; picking
  the first series reported EPS as missing. Fixed by unit-kind selection —
  this also recovered WMT's dividends-per-share.
- **Taxonomy blindness:** Walmart reports shares outstanding only in the
  `dei` taxonomy (`EntityCommonStockSharesOutstanding`). Aliases are now
  searched in both `us-gaap` and `dei`.
- **Quarterly fallback (false-match risk):** the old matcher fell back to
  whatever period existed when no full-year fact was present, and picked
  an arbitrary first entry when values conflicted. Both are now refused
  and reported instead. (This reclassified one v1 "match" — Microsoft
  goodwill impairment — as not safely matchable: MSFT tags it in the
  newer filing only as instant-dated annual-test facts, $0 both years.)
- **Alternate spellings:** retailer/receivables variants
  (`IncreaseDecreaseInRetailRelatedInventories`,
  `IncreaseDecreaseInAccountsAndNotesReceivable`, …) added from real
  filings.

### Known not-applicable cases (reported, never hidden)

- **Exxon Mobil — cost of revenue / gross profit / operating income:**
  Exxon's income statement presents neither a cost-of-revenue line nor
  those subtotals; it reports total costs & expenses instead (tracked).
  Not applicable to this filer's presentation.
- **Walmart — gross profit & total liabilities:** Walmart does not tag
  these with any standard us-gaap concept in EDGAR company-facts (they
  exist, if at all, only as company-extension tags, which secdiff does
  not use because they are not comparable across filers).
- **Exxon Mobil — diluted share count:** XOM tags diluted EPS but not a
  diluted weighted-average share count; reported as not tagged.
- **Apple — common stock value / APIC:** Apple presents one combined
  face line ("Common stock and additional paid-in capital", tag
  `CommonStocksIncludingAdditionalPaidInCapital`). secdiff deliberately
  does not file a combined line under either component label.
- **Exxon Mobil — accounts payable / accrued liabilities:** XOM presents
  one combined line (`AccountsPayableAndAccruedLiabilitiesCurrent`);
  same rule — not merged into either component concept.
- Values can legitimately differ between filings for the *same* period
  when a filer revises a comparative (observed: XOM restated 2024 R&D
  from $987M to $1,000M in its 2025 10-K; Apple revised a 2024 accrued-
  liabilities comparative from $51,703M to $44,024M). secdiff shows each
  filing's own number — that difference *is* part of the diff.

## `--summarize` and AI

The summary is optional and **only ever rephrases hunks the code already
computed** — it cannot invent changes.

- With no key configured, `--summarize` produces an **extractive** summary
  (the largest computed hunks, quoted) and says so in the report.
- If you set `SECDIFF_LLM_API_KEY` (plus optional `SECDIFF_LLM_MODEL` /
  `SECDIFF_LLM_BASE_URL` for any OpenAI-compatible provider), the computed
  hunks are sent to *your own* provider for a rewrite, labelled as an LLM
  summary. Any failure falls back to the extractive summary.
- Without `--summarize`, nothing is sent anywhere. The tool is fully
  functional with no LLM at all.

## EDGAR fair access

secdiff plays by the SEC's rules: a declared User-Agent with a contact
email on every request, pacing well under **10 requests/second**, and an
on-disk cache so repeat runs don't re-download filings. Please use a real
contact email (`EDGAR_EMAIL`) — the SEC asks for one, and may throttle
placeholder identities.

## Limitations (honest list)

- **Section detection is heuristic.** Well-formed 10-Ks (like Apple's)
  split with high confidence; unusual layouts may be flagged low
  confidence, and short "None." items (e.g. Item 9) may be skipped.
- **Sentence diff is noisy at the margins.** Boilerplate re-ordering and
  table text can surface as changes; hunks are capped per section and
  ranked by size so the biggest changes come first.
- **XBRL coverage is the 100 tracked concepts** (see "XBRL coverage"
  above for measured per-company numbers). The catalogue is
  segment-agnostic — segment/dimensional breakdowns are never used in
  place of the consolidated fact — and it does not use
  financial-institution-specific or company-extension tags, so filers
  that rely on those (many banks) match fewer concepts. Retagged
  concepts are listed as unmapped rather than force-matched.
- **`--form 10-Q` compares consecutive quarters** (the two most recent
  10-Qs), not the same quarter a year apart.
- **Filing text is the primary document only** — exhibits (EX-99 etc.)
  are not diffed in v1.
- **Proxy (DEF 14A) heading styles vary wildly** between filers and are
  often split across lines by inline-XBRL layout, so proposal sections
  cannot always be detected. When no sections are detected in either
  proxy, secdiff diffs the whole documents as one clearly labelled
  section (confidence: low) instead of reporting an empty diff.
- The demo filings are a frozen snapshot from 2026-10-17; live runs
  always fetch the current latest filings.

## What secdiff is not

- **Not investment advice.** No buy/sell outputs, no recommendations, no
  price data, no predictions. It shows what changed in a public document;
  what that means is your call.
- No alerts, no watchlists, no accounts — those are possible hosted-layer
  features for later. (A simple self-hostable web front end *is* included —
  see "Web version" above; the open-source tool stays free.)

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m pytest     # offline tests: diff, sections, XBRL, shortcuts
```

## Licence

MIT. Filing data: SEC EDGAR (public filings).

*Pre-launch checklist reminder: clear the `secdiff` name (GitHub org,
domain, trademark) before publishing — see the note at the top.*
