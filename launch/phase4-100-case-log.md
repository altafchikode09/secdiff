# Phase 4 — 100-case log (owner's rule)

Phase 4 adds, on top of the Phase 1–3 pipeline: (1) company pages
(`/company/<TICKER>` — profile, per-form report links, filing years,
this site's own report count); (2) a trending page (`/trending` —
most-reported companies on THIS site, all-time window, from the
stats store); (3) "Biggest deletes" on the home page (AAPL, MSFT,
GOOGL, TSLA, XOM, AMZN, NVDA, META — latest-10-K deletions + score
via the same pipeline, disk-cached per UTC day, background-refreshed,
honest "computing" state, never blocking the page); (4) a per-account
watchlist (`/watchlist` — add/remove by ticker, resolved + validated,
stored in the same SQLite DB; alerts copy honestly says email alerts
arrive when email is switched on); (5) an API docs page (`/api`) and
a read-only JSON endpoint (`GET /api/report?ticker=&form=`) using the
same pipeline and the SAME daily-limit accounting as HTML reports;
(6) repeat-deletion pattern notes in reports ("Also deleted in the
previous cycle" — a normalised deleted sentence of ≥8 words that was
also deleted in the immediately-earlier comparison pair, with a link
to that earlier report; deterministic matching only, silent when the
earlier pair can't be computed); (7) sector rankings (`/sectors` —
the majors + trending companies grouped by a small curated sector
map, labelled as a curated list, not market coverage).

Access model unchanged: report generations count toward the CIK/day
limit on every surface (HTML, API, downloads); doc/trending/sectors/
company page views consume no slots; `/demo` 404; `/stats`
owner-only.

## Owner correction applied mid-phase — /examples removed

The brief originally included a verified-examples page. The owner
overrode it mid-phase: *"Sabhi se demo or examples hata de — kuch bhi
demo or example nahi hona chahiye."* Applied in full:

- The `/examples` page, its route, its data module
  (`src/secdiff/examples.py`) and every link to it (home page,
  biggest-deletes section) were removed; `/examples` is a plain 404.
- Whole-UI audit: the API docs endpoint is now documented as a
  value-free schema (`GET /api/report?ticker={ticker}&form={form}`)
  with response fields described in words — no worked response block
  with made-up numbers. A stray "for example…" clause in the home
  page's year-picker help text was trimmed. Remaining input hints
  live only inside form fields (placeholders) and validation error
  messages, where they cannot be mistaken for site content.
- Test category A (P001–P010) was rewritten to verify the removal
  across the whole UI instead of testing the page.
- Note: the three candidate examples had all been verified live
  before the removal (Apple ESG subsection 2024→2025; Tesla "ESG"
  8→0 mentions 2025→2026; Exxon "objective to play a leading role"
  → "seeks opportunities…", which the diff surfaces as a deletion +
  an addition in Item 1A, not a paired modification). Verification
  work is recorded here only; nothing of it ships.

Suite: `tests/test_phase4_cases.py` — exactly 100 cases, P001–P100.
A  No demo / no examples (removal audit)                        P001–P010
B  Company pages (200/404, forms, years, view counts)           P011–P024
C  Trending (ordering vs stats store, honest empty state)       P025–P032
D  Biggest deletes (cache, sorting, lazy refresh, honesty)      P033–P046
E  Watchlist (auth, add/remove, validation, isolation)          P047–P066
F  API (JSON shape, 400/402/404/405, limit parity with HTML)    P067–P084
G  Patterns (true positive, no false positive, silence)         P085–P094
H  Sectors (mapping, ranking, curated-list labels)              P095–P100

## Bugs found & fixed
| # | Severity | Found | Bug | Fix |
|---|----------|-------|-----|-----|
| B-01 | big (design) | implementation | The background majors-refresh thread resolved the majors store and the pipeline from module globals at compute time, so it could write into whatever store/pipeline a later caller had installed (cross-context pollution — first seen as a failing ordering test when a refresh outlived its fixture) | The worker now receives the store captured at spawn time, and refresh only ever spawns from `serve()` (`_AUTO_REFRESH_MAJORS`, default off) — requests and tests never spawn threads behind the caller's back |
| B-02 | big (design) | implementation | Pattern annotation could trigger real EDGAR downloads inside otherwise-stubbed contexts (and, in principle, slow a report whose caller never asked for the earlier pair) | Annotation now runs only when the company's submissions JSON is already in the on-disk cache — always true for a real live run, since resolution just cached it — and stays silent otherwise |
| B-03 | small | cycle 1 (P043) | Test's biggest-deletes ordering assertion was polluted by the literal "AAPL" in the home form's placeholder | Assertion now compares the positions of the `/company/<TICKER>` links |
| B-04 | small | cycle 1 (P055) | Test compared a still-URL-quoted redirect message | Test now `unquote_plus`-decodes the Location before comparing |
| B-05 | BIG (production) | cycle 3 (live) | **Watchlist cold-start deadlock**: `get_watchlist_store()` called `get_account_store()` while holding the shared `_STORE_LOCK` (a plain `threading.Lock`, not re-entrant), so the first-ever watchlist call on a cold server hung the request thread forever. The 100-case suite missed it because fixtures pre-create the store; only a real cold server hit it | `get_account_store()` is now resolved before the lock (the same pattern `get_stats_store` already used), and `_STORE_LOCK` is an `RLock` so getters may nest safely. Regression check folded into P051: the cold getter must return, in a thread with a timeout |
| B-06 | small | cycle 3 (live) | Polluted dev majors cache briefly showed mixed real/stub entries | Traced to residue of the pre-B-01-fix background worker writing during an earlier test run; the mechanism is fixed by B-01, the dev cache file was discarded, and a clean cold refresh reproduced all-real values (see cycle 3). The deployed cache starts empty |

## Cycles
- Cycle 1: Phase 4 suite → **98/100** (P043, P055 — both test-side,
  B-03/B-04). Existing suites unaffected.
- Cycle 2: fixes applied, re-run → **100/100**; full suite
  **491/491** (391 pre-existing + 100 new). Owner correction
  (examples removal) applied after this cycle; suite re-verified
  100/100 and full suite 491/491 after the removal, with category A
  rewritten to audit the removal.
- Cycle 3: live-EDGAR probing of the new surfaces on a local server
  with the real cache:
  - Anchors intact through the new API: `GET /api/report?ticker=
    AAPL` → 155 del / 162 mod / 50 add / 75 XBRL, score 80; unknown
    ticker → 404 JSON; missing params → 400 JSON.
  - Pattern true positive on real data: the AAPL latest-pair report
    carries exactly 2 "Also deleted in the previous cycle" notes,
    linking to the pinned 2024-vs-2023 report (the earlier probe
    counted 2 repeating deletions for AAPL, 14 for MSFT).
  - Biggest deletes cold refresh (all 8 majors, real values):
    GOOGL 272/100, TSLA 262/95, NVDA 250/95, META 248/85,
    MSFT 237/70, AAPL 155/80, XOM 158/80, AMZN 92/70 — AAPL and XOM
    match their known-good anchors exactly.
  - `/company/AAPL` → CIK 320193, filing years, per-form report
    links, this site's report count. `/trending`, `/sectors`,
    `/api` render 200; `/examples` and `/demo` → 404.
  - Watchlist live flow exposed B-05 (cold-start deadlock — add
    hung >4 min on a cold server). Fixed, regression-tested
    (P051), and the full live flow re-verified cold: signup → add
    NVDA (real resolution) → page lists NVIDIA CORP with its real
    last-known "250 deletions · score 95/100" and the honest
    email-alerts note → remove → empty state.
- Post-fix re-run: Phase 4 suite **100/100**, full suite **491/491**.
- Go-live: commit `4df973d` pushed to main (Render auto-deploy
  confirmed live when `/api` went 404 → 200). Lightweight checks on
  https://secdiff.onrender.com, values as seen:
  - `/examples` 404, `/demo` 404, `/trending` 200 (honest all-time
    label; production stats start empty), `/sectors` 200,
    `/watchlist` as guest → 303 to `/login`.
  - `/company/AAPL` 200 (Apple Inc., CIK 320193, per-form report
    links incl. DEF 14A); `/company/ZZZZQQ` 404.
  - `GET /api/report?ticker=AAPL` → 200 JSON: 155 del / 162 mod /
    50 add / 75 XBRL, score 80, 10 top deletions. Missing ticker →
    400 JSON; unknown ticker → 404 JSON.
  - Live AAPL report carries exactly **2** "Also deleted in the
    previous cycle" notes — the pattern feature reproduces in
    production.
  - Home biggest-deletes table live with real values (GOOGL 272 /
    score 100, MSFT 237 / 70, AAPL 155 / 80, "last checked
    2026-10-19"); the remaining majors showed the honest "still
    being computed" note while the first background refresh worked
    through Render's cold cache.
