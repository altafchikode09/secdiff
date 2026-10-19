# Phase 5 — 100-case log (owner's rule)

Phase 5 is presentation only: one design system for every page
(`src/secdiff/theme.py`), a light/dark theme (cookie toggle +
`prefers-color-scheme` first visit), a responsive layout, and PWA
basics (manifest + generated icons; deliberately **no service
worker** — every page is generated live and personalised, so a
cache could serve a stale report; the manifest/icons give
add-to-home-screen without that risk).

Suite: `tests/test_phase5_cases.py`, exactly 100 cases T001–T100.

- **A** Theme cookie + toggle (T001–T016): no cookie → no
  `data-theme` attribute (media query decides); dark/light cookies
  stamp the attribute; bogus values fall back; POST /theme sets the
  cookie (Path=/, 1 year, SameSite=Lax, deliberately not HttpOnly so
  the init script can read it), 303s back, honours local `next`
  only (protocol-relative and absolute URLs rejected); invalid or
  missing theme → 400; GET → 405; toggle form with both buttons on
  pages, active choice marked.
- **B** Pages in both themes (T017–T036): home, login, signup,
  forgot-password, forgot-username, trending, sectors, API docs,
  404 page, account, watchlist, company page, paywall, waitlist
  thanks, report page (dark/light/no-cookie), /stats still 404 in
  both themes, downloads byte-identical across themes.
- **C** Design-system markers (T037–T051): variable layers in both
  stylesheets (`:root`, `[data-theme="dark"]`, media-query block),
  palette key parity + hex shape, header nav order (Reports,
  Trending, Sectors, Watchlist, API) + My account when logged in,
  shared footer, `.tablewrap`, responsive breakpoints, 44px tap
  targets, chart classes, diff colours via variables.
- **D** Contrast (T052–T066): WCAG ratios computed from the shipped
  `PALETTES` dicts (the same values the CSS is generated from) —
  text/muted/link on bg + surface, text on every diff/banner/score/
  mark background, repeat-note pair, button pair, pos/neg, chart
  text — all ≥ 4.5:1 in BOTH themes; chart bars ≥ 3:1 (non-text).
- **E** Mobile + PWA (T067–T081): viewport meta on home/report/
  login (+ standalone report rendering), manifest route + content
  type + fields + palette colours, icon routes (PNG signature,
  IHDR dimensions 192/512, apple-touch at 192), determinism, cache
  headers, /sw.js and /service-worker.js are 404 by design, no
  `serviceWorker` reference anywhere.
- **F** Offline purity (T082–T091): no external script/link/img/
  font resources on home, login, report, API docs; stylesheets
  contain no URLs at all; manifest is fully local; system font
  stack only; theme-init script present.
- **G** Anchor regressions (T092–T100): crafted AAPL-equivalent
  result reproduces 155/162/50/75 + score 80 via the JSON API and
  the HTML report; report chrome uses classes (no leftover inline
  styles); numbers table wrapped; /demo and /examples still 404;
  /stats still owner-only; paywall still at the 5th company with
  the $9/$27 pricing; home keeps pricing + biggest-deletes.

## Cycles

- **Cycle 0 (baseline):** full existing suite 491/491 before any
  Phase 5 change.
- **Cycle 1: 100/100** on the new suite at first run; full suite
  591/591. Manual adversarial probing then found two small issues
  (below), fixed before cycle 2.
- **Cycle 2: 100/100; full suite 591/591** after the small fixes.
- **Cycle 3 (confirmation + round-trip probes):** toggle return-to
  behaviour verified end-to-end (GET pages return to themselves;
  the report page's toggle returns to its canonical URL; POST
  result pages fall back to home); icon size guard verified;
  full suite **591/591** — only then committed and pushed.

## Bug log

- **B-01 (small, contrast):** the active theme-toggle button used
  the accent colour for its label — in the dark theme #2563eb on
  the dark surface-2 measured ~2.5:1, illegible. Fixed: the active
  button's label uses the theme link colour (≥4.5:1 in both
  themes); the border keeps the accent.
- **B-02 (small, consistency):** the report page — the one page
  without the shared header — had no theme toggle, so a reader
  could not switch themes mid-report. Fixed: the toggle sits in
  the report toolbar and returns to the report's canonical URL;
  `.themeform`/`.themebtn` rules added to the report stylesheet.
- **B-03 (small, UX):** the header toggle originally returned
  every visitor to the home page. Fixed: for GET requests the app
  records the current path+query and the toggle returns there;
  POST result pages (whose URL cannot be replayed as a GET) fall
  back to home, and `_theme_route` still only honours local paths.

No big (functional) bugs were found: no route, form field, limit
accounting, auth flow or anchor output changed behaviour — the
full pre-existing suite (491 cases) passed unmodified after the
redesign, and category G re-verified the anchors inside the new
suite.

## Left out, deliberately

- **Service worker:** not shipped (see above and
  `src/secdiff/theme.py`). Manifest + icons only.
- The share-card PNG and the summary PDF keep their existing
  fixed designs (they are downloads, not pages; the card was
  already dark-themed).
