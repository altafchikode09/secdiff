# Phase 2 — 100-case log (owner's rule)

Phase 2 adds, on top of the Phase 1 pipeline: (1) the Change-Intensity
Score ("Chhupaya Score") at the top of every report, formula published
in the report; (2) the Risk Words Highlighter over the changed text of
the HTML report, term list published as a legend; (3) multi-year
annual trend charts (Revenue, Net income, Total assets, Long-term
debt) from EDGAR's company-concept API, rendered as server-side SVG.

Suite: `tests/test_phase2_cases.py` — exactly 100 cases, P001–P100.
A  Score (determinism, weights, caps, band boundaries, no-XBRL)  P001–P030
B  Highlighter (hits, boundaries, phrases, URL/tag protection)   P031–P065
C  Chart data (annual-only, one alias, one unit, restatements)   P066–P085
D  Chart SVG (labels, negatives, escaping, determinism)          P086–P095
E  Integration (web page, Markdown, CLI form gate, counts)       P096–P100

## Cycle 1 — offline suite

- Phase 2 suite: **100/100**. Full suite: **291/291** (191 pre-existing
  + 100 new). No bugs found in this cycle; the three features were
  built test-first against the honesty rules (score = counts only,
  never hunk sizes; highlighter never fires inside URLs/tags;
  charts refuse substitute units, non-annual facts and <2 points).

## Cycle 2 — live EDGAR probing (the cycle that caught Phase 1's big one)

Real runs against live EDGAR (contact email set), reports inspected
by hand:

- **AAPL 10-K** — anchors intact: 155 del / 162 mod / 50 add / 75 XBRL.
  Score **80/100**; breakdown 40+30+0+10 recomputes by hand from the
  counts on the page. 4 charts × 5 points; Revenue FY2021 $365.82B →
  FY2025 $416.16B and Net income / Total assets / Long-term debt all
  match Apple's reported figures. Legend says "23 highlights" and a
  manual count of hunk highlights is exactly 23 (investigation ×10,
  litigation ×6, lawsuit ×6, default ×1).
- **AAPL DEF 14A** — anchors intact: 457 del / 353 mod / 482 add.
  Score 30/100 from text only (whole-document fallback is never
  treated as a risk/legal section); no charts, no numbers table,
  proxy note present; 24 highlights.
- **MSFT 10-K** — score 70/100; 4 charts with the first-priority
  aliases (RevenueFromContractWithCustomerExcludingAssessedTax,
  NetIncomeLoss, Assets, LongTermDebtNoncurrent); FY labels follow
  Microsoft's June fiscal year end (FY2023 revenue $211.91B ✓).
- **RIVN 10-K** — negative Net income chart renders below the axis in
  red with signed labels (−$4.69B FY2021 … −$3.65B FY2025, Rivian's
  real losses). One suspicious value hand-verified against the EDGAR
  source: Long-term debt FY2023 $4.431B in the chart is exactly what
  the company-concept API reports (a genuine coincidence with
  Rivian's $4.43B FY2023 revenue, not an alias mix-up).
- Observed, pre-existing, NOT a Phase 2 regression: `secdiff JPM`
  still fails resolution ("1 recent 10-K" in EDGAR's recent window,
  needs 2) — the Phase 0 resolver behaviour, unchanged by this phase.

No Phase 2 bugs found in cycle 2.

## Cycle 3 — confirmation re-run

- Phase 2 suite: **100/100**. Full suite: **291/291**. Anchors
  re-verified live in cycle 2 after all code was final (AAPL 10-K
  155/162/50/75; AAPL proxy 457/353/482).

## Design decisions recorded

- The score is computed from the diff the report actually displays
  (same convention as the report's existing change counts), from
  counts only — hunk size and section order never affect it (P025,
  P026).
- "Disappeared XBRL" is a structured count from compute_delta
  (concepts reported by the older filing only), not parsed from the
  human-readable unmapped strings; those strings are unchanged.
- Charts attach only to 10-K / 10-Q reports. 8-K and proxy reports
  carry no financial trend charts, consistent with their existing
  "no numbers table" framing.
- Chart data uses the company-concept API per concept (one alias,
  one unit), NOT the already-downloaded company-facts file, so each
  chart's series is traceable to a single tag's own history.
