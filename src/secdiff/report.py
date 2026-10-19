"""Report rendering: one self-contained HTML file + one Markdown file.

Every change in both reports sits under a header that links to both EDGAR
filings (accession numbers, filing dates, direct document URLs), so any
hunk can be verified against the source filing in one click.
"""
from __future__ import annotations

import html
from dataclasses import dataclass

from .charts import render_chart_svg
from .diffing import SectionDiff
from .highlight import RISK_TERMS, highlight_risk
from .score import FORMULA_TEXT, compute_score
from .search import mark_search_term
from .theme import REPORT_CSS
from .xbrl import XbrlResult, fmt_money


@dataclass
class FilingInfo:
    ticker: str
    company: str
    form: str
    cik: int
    # newer
    new_accession: str
    new_date: str
    new_url: str
    new_index_url: str
    # older
    old_accession: str
    old_date: str
    old_url: str
    old_index_url: str
    section_confidence: str = "high"


# Honest framing for forms whose diff needs a caveat up front. Shown in
# every report (Markdown + HTML) for these forms, never silently.
FORM_NOTES = {
    "8-K": ("8-Ks are event filings: each one reports specific events "
            "(results, deals, leadership changes…), so the two filings "
            "compared here usually cover DIFFERENT events — many "
            "additions are expected and do not by themselves mean "
            "anything was hidden."),
    "DEF 14A": ("Proxy statements (DEF 14A) are filed once a year for "
                "the annual shareholder meeting; this compares the "
                "latest proxy with the prior year's. Proxies carry no "
                "XBRL financial-statement data, so there is no numbers "
                "table for this form."),
}


def _no_xbrl_message(form: str) -> str:
    """The honest line shown when the XBRL numbers table is empty."""
    if form == "DEF 14A":
        return ("Proxy statements carry no XBRL financial-statement "
                "data, so there is no numbers table for this report.")
    return "No matched XBRL concepts were reported by both filings."


def _counts(diffs: list[SectionDiff]) -> tuple[int, int, int]:
    d = sum(1 for s in diffs for h in s.hunks if h.kind == "deletion")
    m = sum(1 for s in diffs for h in s.hunks if h.kind == "modification")
    a = sum(1 for s in diffs for h in s.hunks if h.kind == "addition")
    return d, m, a


def render_markdown(info: FilingInfo, diffs: list[SectionDiff], xbrl: XbrlResult,
                    summary: list[str] | None = None,
                    summary_kind: str = "") -> str:
    d, m, a = _counts(diffs)
    out: list[str] = []
    out.append(f"# secdiff — {info.company} ({info.ticker}) {info.form}")
    out.append("")
    out.append(f"- **Newer filing:** {info.new_date} · accession "
               f"[{info.new_accession}]({info.new_index_url}) · "
               f"[document]({info.new_url})")
    out.append(f"- **Older filing:** {info.old_date} · accession "
               f"[{info.old_accession}]({info.old_index_url}) · "
               f"[document]({info.old_url})")
    out.append(f"- **Changes found:** {d} deletions · {m} modifications · "
               f"{a} additions · {len(xbrl.rows)} XBRL numbers changed")
    score = compute_score(diffs, xbrl)
    out.append(f"- **Change-intensity score: {score.total}/100 "
               f"({score.band})** — {score.breakdown()}. "
               f"{FORMULA_TEXT}")
    if xbrl.considered:
        out.append(f"- **XBRL coverage:** {xbrl.matched} of {xbrl.considered} "
                   "tracked concepts were reported by both filings")
    out.append(f"- Section-detection confidence: **{info.section_confidence}**")
    if info.form in FORM_NOTES:
        out.append(f"- **About this form:** {FORM_NOTES[info.form]}")
    out.append("")
    out.append("> Deterministic diff computed by code (no AI). "
               "This report is not investment advice.")
    out.append("")
    if summary:
        out.append(f"## Summary ({summary_kind})")
        out.append("")
        out.extend(summary)
        out.append("")
    out.append("## XBRL numbers — what changed")
    out.append("")
    if xbrl.rows:
        out.append("| Item | Older filing | Newer filing | Change | % |")
        out.append("|---|---|---|---|---|")
        for r in xbrl.rows:
            pct = f"{r.pct:+.1f}%" if r.pct is not None else "—"
            out.append(f"| {r.label} | {fmt_money(r.old, r.unit)} | {fmt_money(r.new, r.unit)} "
                       f"| {fmt_money(r.change, r.unit)} | {pct} |")
    else:
        out.append(f"_{_no_xbrl_message(info.form)}_")
    if xbrl.unmapped:
        out.append("")
        out.append("**Retagged / unmapped concepts (listed, never merged):**")
        for u in xbrl.unmapped:
            out.append(f"- {u}")
    if xbrl.charts:
        out.append("")
        out.append("## Multi-year trends — annual values")
        out.append("")
        out.append("One tag and one unit per concept; the most recently "
                   "filed value wins each fiscal year (values as most "
                   "recently reported). Source: SEC EDGAR "
                   "company-concept API.")
        for s in xbrl.charts:
            out.append("")
            out.append(f"### {s.label} ({s.unit}) — tag `{s.alias}`")
            out.append("")
            out.append("| Fiscal year | Value |")
            out.append("|---|---|")
            for pt in s.points:
                out.append(f"| {pt.fy} | {fmt_money(pt.value, s.unit)} |")
            out.append("")
            out.append(f"Source: {s.source_url}")
    out.append("")
    out.append("## Text changes — deletions first")
    for s in diffs:
        if not s.hunks:
            continue
        out.append("")
        # "FULL" is the whole-document fallback (no ITEM sections were
        # detected): its title already says what it is, with no "Item".
        out.append(f"### {s.title}" if s.item == "FULL"
                   else f"### Item {s.item} — {s.title}")
        out.append(f"_Source: [older filing]({info.old_url}) vs "
                   f"[newer filing]({info.new_url})_")
        out.append("")
        for h in s.hunks:
            if h.kind == "deletion":
                out.append(f"- 🔴 **DELETED:** {h.old}")
            elif h.kind == "modification":
                out.append(f"- 🟡 **CHANGED:**")
                out.append(f"  - was: {h.old}")
                out.append(f"  - now: {h.new}")
            else:
                out.append(f"- 🟢 **ADDED:** {h.new}")
            if h.note:
                link = (f" [See the earlier report]({h.note_url})"
                        if h.note_url else "")
                out.append(f"  - ↩ _{h.note}.{link}_")
    out.append("")
    out.append("---")
    out.append("Generated by secdiff. Data: SEC EDGAR (public domain filings). "
               "Not investment advice.")
    return "\n".join(out) + "\n"


# The report stylesheet lives in secdiff.theme with the rest of the
# design system (both themes, all colours via custom properties) —
# kept under this name for the existing imports/tests.
_CSS = REPORT_CSS


def render_html(info: FilingInfo, diffs: list[SectionDiff], xbrl: XbrlResult,
                summary: list[str] | None = None,
                summary_kind: str = "",
                search_term: str | None = None,
                search_base_url: str = "") -> str:
    d, m, a = _counts(diffs)
    e = html.escape
    p: list[str] = []
    p.append("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>")
    p.append("<meta name='viewport' "
             "content='width=device-width, initial-scale=1'>")
    p.append(f"<title>secdiff — {e(info.ticker)} {e(info.form)}</title>"
             f"<style>{_CSS}</style></head><body>")
    p.append(f"<h1>secdiff — {e(info.company)} ({e(info.ticker)}) {e(info.form)}</h1>")
    p.append("<div class='meta'>")
    p.append(f"<div><strong>Newer:</strong> {e(info.new_date)} · "
             f"<a href='{e(info.new_index_url)}'>{e(info.new_accession)}</a> · "
             f"<a href='{e(info.new_url)}'>document</a></div>")
    p.append(f"<div><strong>Older:</strong> {e(info.old_date)} · "
             f"<a href='{e(info.old_index_url)}'>{e(info.old_accession)}</a> · "
             f"<a href='{e(info.old_url)}'>document</a></div>")
    p.append(f"<div><strong>{d} deletions · {m} modifications · {a} additions · "
             f"{len(xbrl.rows)} XBRL numbers changed</strong></div>")
    score = compute_score(diffs, xbrl)
    p.append("<div class='score'>")
    p.append(f"<span class='big'>{score.total}/100</span> "
             f"<span class='band'>Change-intensity score: "
             f"{e(score.band)}</span>")
    p.append(f"<div>{e(score.breakdown())}</div>")
    p.append(f"<details class='score-how'><summary>How is this score "
             f"calculated?</summary>{e(FORMULA_TEXT)}</details>")
    p.append("</div>")
    if xbrl.considered:
        p.append(f"<div class='note'>XBRL coverage: {xbrl.matched} of "
                 f"{xbrl.considered} tracked concepts were reported by both "
                 f"filings.</div>")
    p.append(f"<div class='note'>Section-detection confidence: "
             f"{e(info.section_confidence)}. Deterministic diff computed by code "
             f"(no AI). Not investment advice.</div>")
    if info.form in FORM_NOTES:
        p.append(f"<div class='note'><strong>About this form:</strong> "
                 f"{e(FORM_NOTES[info.form])}</div>")
    p.append("</div>")
    if search_term:
        # `diffs` here are already the search-filtered hunks, so the
        # counts above ARE the match counts; say exactly that, and
        # offer the unfiltered report one click away.
        n = d + m + a
        remove = (f" <a href='{e(search_base_url, quote=True)}'>Show the "
                  f"full report</a>." if search_base_url else "")
        p.append("<div class='searchbanner'>")
        p.append(f"<strong>Search: &lsquo;{e(search_term)}&rsquo;</strong> "
                 f"— {n} change{'s' if n != 1 else ''} mention "
                 f"&lsquo;{e(search_term)}&rsquo;. Only those changes are "
                 f"shown below, with the term "
                 f"<mark class='search'>highlighted</mark>. The search "
                 f"runs after any risk-only / deletions-only / top-N "
                 f"filters.{remove}</div>")
    if summary:
        p.append(f"<h2>Summary ({e(summary_kind)})</h2>")
        for ln in summary:
            p.append(f"<p>{e(ln)}</p>")
    p.append("<h2>XBRL numbers — what changed</h2>")
    if xbrl.rows:
        p.append("<div class='tablewrap'><table><tr><th>Item</th>"
                 "<th>Older filing</th>"
                 "<th>Newer filing</th><th>Change</th><th>%</th></tr>")
        for r in xbrl.rows:
            cls = "pos" if (r.change or 0) >= 0 else "neg"
            pct = f"{r.pct:+.1f}%" if r.pct is not None else "—"
            p.append(f"<tr><td>{e(r.label)}</td><td>{fmt_money(r.old, r.unit)}</td>"
                     f"<td>{fmt_money(r.new, r.unit)}</td>"
                     f"<td class='{cls}'>{fmt_money(r.change, r.unit)}</td>"
                     f"<td class='{cls}'>{pct}</td></tr>")
        p.append("</table></div>")
    else:
        p.append(f"<p><em>{e(_no_xbrl_message(info.form))}</em></p>")
    if xbrl.unmapped:
        p.append("<p><strong>Retagged / unmapped concepts (listed, never merged):</strong></p><ul>")
        for u in xbrl.unmapped:
            p.append(f"<li>{e(u)}</li>")
        p.append("</ul>")
    if xbrl.charts:
        p.append("<h2>Multi-year trends — annual values</h2>")
        p.append("<p class='note'>One tag and one unit per concept; the "
                 "most recently filed value wins each fiscal year "
                 "(values as most recently reported). Source: SEC EDGAR "
                 "company-concept API.</p>")
        for s in xbrl.charts:
            p.append("<div class='chart'>")
            p.append(f"<h3>{e(s.label)} ({e(s.unit)})</h3>")
            p.append(render_chart_svg(s))
            p.append(f"<p class='note'>Tag: {e(s.alias)} · "
                     f"<a href='{e(s.source_url)}'>EDGAR source</a></p>")
            p.append("</div>")
    # Highlight risk terms in the change text first (on the escaped
    # text, never inside URLs), count them, and only then print the
    # legend — the legend lists exactly the published terms.
    rendered: list[tuple[SectionDiff, list[tuple[str, str, str, str, str]]]] = []
    risk_hits = 0
    for s in diffs:
        if not s.hunks:
            continue
        hunks_html: list[tuple[str, str, str, str, str]] = []
        for h in s.hunks:
            old_html, n1 = highlight_risk(e(h.old)) if h.old else ("", 0)
            new_html, n2 = highlight_risk(e(h.new)) if h.new else ("", 0)
            risk_hits += n1 + n2
            if search_term:
                # Mark the search term on top of the risk marks; the
                # marker protects tags/URLs, so the two markups can
                # nest but never corrupt each other.
                old_html, _ = mark_search_term(old_html, search_term)
                new_html, _ = mark_search_term(new_html, search_term)
            hunks_html.append((h.kind, old_html, new_html,
                               h.note, h.note_url))
        rendered.append((s, hunks_html))
    p.append("<h2>Text changes — deletions first</h2>")
    if search_term and not rendered:
        p.append(f"<p>No changes in this report mention "
                 f"&lsquo;{e(search_term)}&rsquo;.</p>")
    if risk_hits:
        p.append(f"<p class='note'><mark class='risk'>Highlighted</mark> "
                 f"words are the published risk terms ({risk_hits} "
                 f"highlight{'s' if risk_hits != 1 else ''} in this "
                 f"report): {e(', '.join(RISK_TERMS))}. Highlighting "
                 f"is a reading aid only — a highlighted word is not, "
                 f"by itself, a finding about the company.</p>")
    for s, hunks_html in rendered:
        if s.item == "FULL":    # whole-document fallback section
            p.append(f"<h3>{e(s.title)}</h3>")
        else:
            p.append(f"<h3>Item {e(s.item)} — {e(s.title)}</h3>")
        p.append(f"<p class='note'>Source: <a href='{e(info.old_url)}'>older filing</a>"
                 f" vs <a href='{e(info.new_url)}'>newer filing</a></p>")
        for kind, old_html, new_html, note, note_url in hunks_html:
            if kind == "deletion":
                p.append(f"<div class='del'><span class='tag'>DELETED</span><br>{old_html}</div>")
            elif kind == "modification":
                p.append(f"<div class='mod'><span class='tag'>CHANGED</span><br>"
                         f"<strong>was:</strong> {old_html}<br>"
                         f"<strong>now:</strong> {new_html}</div>")
            else:
                p.append(f"<div class='add'><span class='tag'>ADDED</span><br>{new_html}</div>")
            if note:
                link = (f" <a href='{e(note_url, quote=True)}'>See the "
                        f"earlier report</a>." if note_url else "")
                p.append(f"<div class='repeat-note'>&#8617; "
                         f"{e(note)}.{link}</div>")
    p.append("<hr><p class='note'>Generated by secdiff. Data: SEC EDGAR. "
             "Not investment advice.</p></body></html>")
    return "\n".join(p)
