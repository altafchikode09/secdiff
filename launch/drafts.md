# secdiff — launch drafts (DO NOT POST without owner approval)

Prepared 2026-10-17. All examples below were produced by the secdiff CLI on
real SEC EDGAR filings and verified two ways: the quoted text appears in the
report file secdiff generated, and the key phrase counts were checked
directly in the raw filing documents.

Links: GitHub https://github.com/altafchikode09/secdiff ·
Live site https://secdiff.onrender.com

---

## The verified examples (ranked)

### #1 — Apple (AAPL): an entire risk subsection deleted
- 10-K filed 2024-11-01 (accession 0000320193-24-000123) vs 10-K filed
  2025-10-31 (accession 0000320193-25-000079), Item 1A — Risk Factors.
- Apple's 2024 10-K had a risk subsection about expectations relating to
  environmental, social and governance considerations. In the 2025 10-K it
  is gone. The phrase "environmental, social and governance" appears 6
  times in the 2024 filing and 0 times in the 2025 filing.
- Deleted sentence (exact, from the secdiff report):
  "Many governments, regulators, investors, employees, customers and other
  stakeholders are increasingly focused on environmental, social and
  governance considerations relating to businesses, including climate
  change and greenhouse gas emissions, human and civil rights, and
  diversity, equity and inclusion."

### #2 — Tesla (TSLA): "ESG" erased entirely
- 10-K filed 2025-01-30 (accession 0001628280-25-003063) vs 10-K filed
  2026-01-29 (accession 0001628280-26-003952), Item 1A — Risk Factors.
- The word "ESG" appears 8 times in the older filing and 0 times in the
  newer one.
- Deleted sentences (exact, from the secdiff report):
  "Companies across many industries are facing increasing scrutiny related
  to their environmental, social and governance (ESG) practices."
  "Increased scrutiny and changing expectations from stakeholders with
  respect to the Company's ESG practices may result in additional costs
  or risks."

### #3 — Exxon Mobil (XOM): a commitment reworded weaker
- 10-K filed 2025-02-19 (accession 0000034088-25-000010) vs 10-K filed
  2026-02-18 (accession 0000034088-26-000045), Item 1A — Risk Factors.
- Was: "The Company's objective to play a leading role in the energy
  transition, including the Company's announced ambition ultimately to
  achieve net zero with respect to Scope 1 and 2 emissions from operations
  with continued technology development and government policy support
  where ExxonMobil is the operator, carries risks that the transition …
  will not be available or develop at the pace or in the manner expected
  by current net-zero scenarios."
- Now: "The Company seeks opportunities to play a leading role in the
  energy transition, including the Company's announced ambition ultimately
  to achieve net zero with respect to Scope 1 and 2 emissions from our
  operated assets with continued technology development and government
  policy support, which carries risks that the transition …"
- The lead-in changed from "objective to play a leading role" to "seeks
  opportunities to play a leading role".

---

## a) Show HN

**Title (61 chars):**
Show HN: secdiff – git diff for SEC filings (deletions first)

**First comment / body:**

I built secdiff, an open-source command-line tool (and free hosted version)
that diffs a company's newest SEC filing against the previous one — like
`git diff`, but for 10-Ks and 10-Qs. Deletions come first, because what a
company stops saying is often the interesting part.

Example, found with it this week: Apple's 10-K filed 2025-10-31 deleted
the risk-factor subsection on environmental, social and governance
considerations that was in its 2024 10-K. One of the removed sentences:

"Many governments, regulators, investors, employees, customers and other
stakeholders are increasingly focused on environmental, social and
governance considerations relating to businesses, including climate
change and greenhouse gas emissions, human and civil rights, and
diversity, equity and inclusion."

The phrase "environmental, social and governance" appears 6 times in the
2024 filing and 0 times in the 2025 one. Tesla's newest 10-K does the same
thing (the word "ESG" goes from 8 occurrences to 0), and Exxon Mobil
reworded "The Company's objective to play a leading role in the energy
transition" to "The Company seeks opportunities to play a leading role in
the energy transition". All three are Item 1A changes anyone can reproduce
in a few seconds.

Why build this: comparing filings line by line ("redlining") is a paid
feature inside professional terminals. I could not find an open-source
version, so I wrote one.

How it works:
- Fetches the two most recent filings of a form from SEC EDGAR, splits
  them into sections (Item 1A, MD&A, financial statements, …) and computes
  a deterministic, sentence-level diff in code. No LLM is involved in the
  diff itself; an optional summary feature uses your own API key if you
  provide one.
- Also compares the filings' XBRL numbers across 100 tracked financial
  concepts (revenue, net income, cash, debt, EPS, …) matched by
  accession, so figure changes sit next to the wording changes.
- Output is an HTML and a Markdown report, every change linked back to
  the source filing on EDGAR.
- Python standard library only, no runtime dependencies. 62 tests pass.
  MIT licence.

Try it without installing anything: https://secdiff.onrender.com
(ticker in, live EDGAR report out).
Code: https://github.com/altafchikode09/secdiff — `secdiff AAPL` is the
whole workflow locally.

This is a research/information tool; it is not investment advice.

Feedback welcome — especially filings where the section splitting or the
diff gets something wrong.

---

## b) Reddit — r/opensource

**Title:**
secdiff – an open-source "git diff" for SEC filings (MIT, Python stdlib only, no dependencies)

**Body:**

I wanted to see what companies quietly remove from their annual reports,
and the tools that do this (filing "redlines") are paid features in
professional terminals. So I built an open-source one.

`secdiff AAPL` fetches a company's two most recent 10-Ks from SEC EDGAR,
diffs them sentence by sentence with deletions first, and also compares
the XBRL numbers (100 tracked concepts) between the two filings. The diff
is computed deterministically in code — no LLM; there's only an optional
BYO-key summary on top. Output is HTML + Markdown, linked back to the
source filings.

What it surfaces, from this earnings cycle:

- Apple deleted its entire ESG risk subsection from Item 1A ("environmental,
  social and governance" goes from 6 mentions in the 2024 10-K to 0 in
  the 2025 10-K).
- Tesla removed every mention of "ESG" (8 → 0).
- Exxon Mobil softened "The Company's objective to play a leading role in
  the energy transition" to "The Company seeks opportunities to play a
  leading role…".

Free hosted version (no signup): https://secdiff.onrender.com
Repo: https://github.com/altafchikode09/secdiff

It's a research tool, not investment advice. Bug reports and filing
edge cases very welcome — EDGAR filers are creative.

---

## c) Reddit — r/SideProject

**Title:**
I built a "git diff" for SEC filings to catch what companies quietly delete from their annual reports

**Body:**

Side project: secdiff. You give it a ticker, it downloads the company's
newest 10-K and the previous one from SEC EDGAR and shows you exactly
what changed — deleted sentences first, plus a table of the financial
numbers (XBRL) that moved.

I built it because I kept reading that professionals pay for filing
"redline" tools, and I wanted to know what a company like Apple changes
when nobody is watching. Answer, from its most recent 10-K: it deleted
its whole ESG risk section. "Environmental, social and
governance" appears 6 times in the 2024 filing and 0 times in the new
one. Tesla did the same (8 mentions of "ESG" → 0). Exxon Mobil changed
its "objective to play a leading role in the energy transition" into
"seeks opportunities to play a leading role".

Tech notes: Python, standard library only (zero dependencies), the diff
is deterministic code — the only optional AI part is a summary you can
run with your own API key. 62 tests passing. MIT licence.

Live demo (real EDGAR data, free): https://secdiff.onrender.com
Code: https://github.com/altafchikode09/secdiff

Not investment advice — it's a reading tool. I'd love feedback on what
to add next (more forms? watchlists? alerts when a filing changes?).

---

## d) X / Twitter (236 chars)

Apple's newest 10-K deleted its entire ESG risk section — "environmental,
social and governance" appears 6× in the 2024 filing, 0× in the new one.
Found with secdiff, an open-source git diff for SEC filings:
https://secdiff.onrender.com

(236 characters including the link as written.)
