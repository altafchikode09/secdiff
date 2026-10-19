"""secdiff CLI.

Quick mode (the shortcut that matters most):

    secdiff AAPL

does everything: fetch the latest two 10-Ks from EDGAR, diff them, write
an HTML + a Markdown report, and print a one-line summary.

Shortcuts:
    secdiff demo                 offline, bundled REAL filings, no network
    secdiff AAPL --form 10-Q     compare 10-Qs instead of 10-Ks
    secdiff AAPL --form 8-K      compare the last two 8-K event filings
    secdiff AAPL --form "DEF 14A"  compare the last two proxy statements
    secdiff AAPL --risk-only     only Item 1A Risk Factors
    secdiff AAPL --deletions-only  only removed text
    secdiff AAPL --top 20        only the 20 biggest changes
    secdiff AAPL --section mdna  just one section (risk, mdna, business…)
    secdiff AAPL --years 2       compare against the filing 2 years back
    secdiff AAPL --base-year 2022  compare the latest filing against 2022's
    secdiff AAPL --vs-year 2025 --base-year 2022
                                 compare the 2025 filing against 2022's
    secdiff AAPL --export-csv FILE  also save the XBRL numbers table as CSV
    secdiff AAPL --output DIR    choose where reports go
    secdiff AAPL --summarize     add a summary of the computed diff
    secdiff serve                run the web front end (see secdiff.web)

Supported forms: 10-K, 10-Q, 8-K and DEF 14A (proxy). 8-Ks are event
filings and proxies carry no XBRL numbers table; both are labelled as
such in their reports rather than presented as if they were 10-Ks.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .charts import build_charts
from .diffing import diff_section
from .edgar import EdgarClient, Filing
from .export import xbrl_to_csv
from .report import FilingInfo, _counts, render_html, render_markdown
from .sections import split_sections
from .shortcuts import filter_diffs, resolve_section
from .summarize import extractive_summary, llm_summary
from .xbrl import compute_delta

# Every form the diff pipeline supports end to end.
SUPPORTED_FORMS = ["10-K", "10-Q", "8-K", "DEF 14A"]


def build_diffs(old_text: str, new_text: str, form: str):
    old_split = split_sections(old_text, form)
    new_split = split_sections(new_text, form)
    if not old_split.sections and not new_split.sections \
            and (old_text.strip() or new_text.strip()):
        # No ITEM/Proposal headings could be detected in EITHER document
        # (common for proxy statements, whose heading styles vary wildly
        # and are often split across lines by inline-XBRL layout). An
        # empty report would silently claim "nothing changed", which is
        # false — diff the whole documents as one clearly labelled
        # section instead. Section confidence stays "low" (it is, since
        # no sections were detected), so the caveat is visible.
        # The hunk cap is far larger than the per-section one: with a
        # single section, the usual cap of 60 (deletions first) would
        # show ONLY deletions and report totals that hide every
        # modification and addition in the document.
        return [diff_section(
            "FULL",
            "Full document — no ITEM sections detected in either "
            "filing, so the whole documents were diffed",
            old_text, new_text, max_hunks=2000)], "low"
    diffs = []
    items = sorted(set(old_split.sections) | set(new_split.sections),
                   key=lambda x: (len(x), x))
    for item in items:
        old_s = old_split.sections.get(item)
        new_s = new_split.sections.get(item)
        title = (new_s or old_s).title
        diffs.append(diff_section(item, title,
                                  old_s.text if old_s else "",
                                  new_s.text if new_s else ""))
    conf = "high" if old_split.confidence == "high" and \
        new_split.confidence == "high" else "low"
    return diffs, conf


def render_reports(args, info: FilingInfo, diffs, xbrl):
    """Filter + (optionally) summarise + render. Shared by the CLI and the
    web front end so both always produce the identical report.

    Returns (markdown, html, (deletions, modifications, additions)).
    """
    only_item = None
    if getattr(args, "risk_only", False):
        only_item = "1A"
    elif getattr(args, "section", None):
        only_item = resolve_section(args.section, getattr(args, "form", "10-K"))
    diffs = filter_diffs(diffs, only_item=only_item,
                         deletions_only=getattr(args, "deletions_only", False),
                         top=getattr(args, "top", None))
    summary, summary_kind = None, ""
    if getattr(args, "summarize", False):
        summary = llm_summary(diffs)
        if summary:
            summary_kind = "LLM summary of the computed diff — your own provider, BYO key"
        else:
            summary = extractive_summary(diffs)
            summary_kind = "extractive summary of the computed diff (no LLM used)"
    md = render_markdown(info, diffs, xbrl, summary, summary_kind)
    ht = render_html(info, diffs, xbrl, summary, summary_kind)
    return md, ht, _counts(diffs)


def one_line_summary(info: FilingInfo, counts, xbrl) -> str:
    """The canonical one-line run summary (CLI prints it, the web shows it)."""
    d, m, a = counts
    return (f"{info.ticker} {info.form}: {d} deletions, {m} modifications, "
            f"{a} additions, {len(xbrl.rows)} XBRL numbers changed "
            f"({info.old_date} -> {info.new_date})")


def finish(args, info: FilingInfo, diffs, xbrl, out_dir: Path) -> int:
    md, ht, counts = render_reports(args, info, diffs, xbrl)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"secdiff-{info.ticker}-{info.form}".lower() \
        .replace("/", "-").replace(" ", "-")
    md_path = out_dir / f"{stem}.md"
    html_path = out_dir / f"{stem}.html"
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(ht, encoding="utf-8")
    print(one_line_summary(info, counts, xbrl))
    print(f"HTML report: {html_path.resolve()}")
    print(f"Markdown report: {md_path.resolve()}")
    export_csv = getattr(args, "export_csv", None)
    if export_csv:
        csv_path = Path(export_csv)
        if csv_path.parent and str(csv_path.parent) not in ("", "."):
            csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(xbrl_to_csv(xbrl), encoding="utf-8")
        print(f"CSV numbers table: {csv_path.resolve()}")
    return 0


def demo_pipeline(args):
    """(info, diffs, xbrl) for the bundled offline demo. No network."""
    from .demo import load_demo
    bundle = load_demo()
    meta = bundle["meta"]
    diffs, conf = build_diffs(bundle["old_text"], bundle["new_text"], meta["form"])
    xbrl = compute_delta(bundle["facts"], meta["old"]["accession"],
                         meta["new"]["accession"])
    info = FilingInfo(
        ticker=meta["ticker"], company=meta["company"], form=meta["form"],
        cik=meta["cik"],
        new_accession=meta["new"]["accession"], new_date=meta["new"]["filing_date"],
        new_url=meta["new"]["url"], new_index_url=meta["new"]["index_url"],
        old_accession=meta["old"]["accession"], old_date=meta["old"]["filing_date"],
        old_url=meta["old"]["url"], old_index_url=meta["old"]["index_url"],
        section_confidence=conf)
    return info, diffs, xbrl


def run_demo(args) -> int:
    info, diffs, xbrl = demo_pipeline(args)
    print("Demo mode: bundled REAL filings from SEC EDGAR — no network used.")
    return finish(args, info, diffs, xbrl, Path(args.output))


def live_pipeline(args):
    """(info, diffs, xbrl) for a live EDGAR run.

    Uses the package's EdgarClient, so downloads go through the on-disk
    cache and the SEC fair-access pacing. Raises SystemExit (with an
    honest, human-readable message) when the ticker or the filings can't
    be found, and urllib errors when EDGAR can't be reached.
    """
    email = args.email or os.environ.get("EDGAR_EMAIL") or \
        os.environ.get("SECDIFF_EMAIL") or "you@example.com"
    if email == "you@example.com":
        print("Note: using a placeholder contact email in the EDGAR User-Agent. "
              "Set EDGAR_EMAIL=you@realmail.com — the SEC asks for a real one.",
              file=sys.stderr)
    cache = Path(args.cache_dir) if args.cache_dir else \
        Path.home() / ".cache" / "secdiff"
    client = EdgarClient(email=email, cache_dir=cache)
    vs_year = getattr(args, "vs_year", None)
    base_year = getattr(args, "base_year", None)
    year_mode = vs_year is not None or base_year is not None
    want = 2 if year_mode else args.years + 1
    # Resolve the ticker to the entity that actually files this form —
    # EDGAR's ticker files can point at a related entity that never does.
    cik, company = client.resolve_ticker(args.ticker, form=args.form,
                                         min_count=want)
    if year_mode:
        newer, older = _pick_filings_by_year(
            client.all_filings(cik, args.form), args.form, vs_year, base_year)
    else:
        filings = client.recent_filings(cik, args.form, count=want)
        newer, older = filings[0], filings[args.years]
    new_text = client.filing_text(cik, newer)
    old_text = client.filing_text(cik, older)
    diffs, conf = build_diffs(old_text, new_text, args.form)
    facts = client.company_facts(cik)
    xbrl = compute_delta(facts, older.accession, newer.accession)
    if args.form in ("10-K", "10-Q"):
        # Multi-year annual trend charts for the headline numbers.
        # build_charts never raises and returns [] when a concept has
        # fewer than 2 comparable annual points; 8-K / proxy reports
        # carry no financial trend charts (their reports say why).
        xbrl.charts = build_charts(client, cik)
    info = FilingInfo(
        # When the user typed a company name, label the report with the
        # ticker it matched; otherwise with what they typed, as before.
        ticker=client.name_matched_ticker or args.ticker.upper(),
        company=company, form=args.form, cik=cik,
        new_accession=newer.accession, new_date=newer.filing_date,
        new_url=newer.document_url(cik), new_index_url=newer.index_url(cik),
        old_accession=older.accession, old_date=older.filing_date,
        old_url=older.document_url(cik), old_index_url=older.index_url(cik),
        section_confidence=conf)
    return info, diffs, xbrl


def _pick_filings_by_year(filings: list[Filing], form: str,
                          vs_year: int | None,
                          base_year: int | None) -> tuple[Filing, Filing]:
    """Pick the (newer, older) pair for an explicit-year compare.

    `vs_year` names the newer filing's filing year (default: the latest
    filing); `base_year` names the older one's (default: the filing
    immediately before the newer one). Only years in which the company
    really filed this form are accepted — anything else is an honest
    error listing the years that do exist; the pair is never guessed.
    When several filings of the form share a year (routine for 8-Ks,
    which can be filed many times a year), the year's LATEST filing is
    used and the caller can narrow further with the other options.
    """
    if len(filings) < 2:
        raise SystemExit(
            f"Only {len(filings)} {form} filing(s) found on EDGAR; "
            "need 2 to diff.")
    years = sorted({f.filing_year for f in filings
                    if f.filing_year is not None}, reverse=True)

    def find(year: int, newest: bool) -> Filing | None:
        matches = [f for f in filings if f.filing_year == year]
        if not matches:
            return None
        return matches[0] if newest else matches[-1]

    if vs_year is not None:
        newer = find(vs_year, newest=True)
        if newer is None:
            raise SystemExit(
                f"No {form} filing from {vs_year} on EDGAR for this "
                f"company. Filing years available: "
                f"{', '.join(map(str, years))}.")
    else:
        newer = filings[0]
    if base_year is not None:
        if vs_year is not None and base_year == vs_year:
            raise SystemExit(
                f"--base-year and --vs-year are both {base_year}: a "
                "filing cannot be compared with itself. Pick two "
                "different years.")
        older = find(base_year, newest=False)
        if older is None:
            raise SystemExit(
                f"No {form} filing from {base_year} on EDGAR for this "
                f"company. Filing years available: "
                f"{', '.join(map(str, years))}.")
    else:
        idx = filings.index(newer)
        if idx + 1 >= len(filings):
            raise SystemExit(
                f"The {newer.filing_date} {form} is the oldest one on "
                "EDGAR for this company — there is no earlier filing "
                "to compare it against.")
        older = filings[idx + 1]
    if filings.index(older) <= filings.index(newer):
        raise SystemExit(
            f"The 'older' filing ({older.filing_date}) is not older "
            f"than the 'newer' one ({newer.filing_date}). Check the "
            "two years: --vs-year must be the later filing, "
            "--base-year the earlier one.")
    return newer, older


def run_live(args) -> int:
    info, diffs, xbrl = live_pipeline(args)
    return finish(args, info, diffs, xbrl, Path(args.output))


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="secdiff",
        description="git diff for SEC filings — compare a company's latest "
                    "10-K with its prior one. Not investment advice.")
    p.add_argument("ticker", help="Ticker symbol (e.g. AAPL) or company name "
                                  "(e.g. Microsoft), or 'demo' for the bundled "
                                  "offline demo")
    p.add_argument("--form", default="10-K", choices=SUPPORTED_FORMS,
                   help="Filing form to compare (default: 10-K)")
    p.add_argument("--years", type=int, default=1,
                   help="Compare against the filing N years back (default: 1)")
    p.add_argument("--vs-year", type=int, default=None, metavar="YEAR",
                   help="Compare the filing from YEAR (the newer side) "
                        "instead of the latest filing")
    p.add_argument("--base-year", type=int, default=None, metavar="YEAR",
                   help="Compare against the filing from YEAR (the older "
                        "side) instead of the previous filing")
    p.add_argument("--export-csv", default=None, metavar="PATH",
                   help="Also write the XBRL numbers table to PATH as CSV "
                        "(exactly the values shown in the report)")
    p.add_argument("--output", default="secdiff-out",
                   help="Output directory for reports (default: ./secdiff-out)")
    p.add_argument("--section", default=None,
                   help="Diff only one section: risk, mdna, business, legal, "
                        "financials, controls, or an item number (1A, 7, ...)")
    p.add_argument("--risk-only", action="store_true",
                   help="Shortcut for --section risk (Item 1A Risk Factors)")
    p.add_argument("--deletions-only", action="store_true",
                   help="Show only text that was removed")
    p.add_argument("--top", type=int, default=None,
                   help="Show only the N biggest changes overall")
    p.add_argument("--summarize", action="store_true",
                   help="Add a summary of the computed diff (extractive by "
                        "default; uses your own LLM key only if "
                        "SECDIFF_LLM_API_KEY is set)")
    p.add_argument("--email", default=None,
                   help="Contact email for the EDGAR User-Agent "
                        "(or set EDGAR_EMAIL)")
    p.add_argument("--cache-dir", default=None,
                   help="Cache directory (default: ~/.cache/secdiff)")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "serve":
        from .web import main as web_main
        return web_main(argv[1:])
    args = make_parser().parse_args(argv)
    if args.ticker.lower() == "demo":
        return run_demo(args)
    return run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
