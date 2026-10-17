"""Small, dependency-free SEC EDGAR client.

Design rules (SEC fair access):
- Every request carries a declared User-Agent with a contact email.
- Requests are paced to stay well under the SEC's 10 requests/second limit.
- Everything downloaded is cached on disk, so re-runs don't re-hit EDGAR.

Only public EDGAR endpoints are used:
- https://www.sec.gov/files/company_tickers.json      (ticker -> CIK)
- https://www.sec.gov/files/company_tickers_exchange.json (ticker -> CIK, alt)
- https://www.sec.gov/cgi-bin/browse-edgar            (classic ticker lookup)
- https://data.sec.gov/submissions/CIK##########.json (filing history)
- https://www.sec.gov/Archives/edgar/data/...          (filing documents)
- https://data.sec.gov/api/xbrl/companyfacts/...       (XBRL facts)

Ticker resolution is deliberately paranoid: EDGAR's ticker files can point
a ticker at a related entity that never files the form you asked for (e.g.
in Oct 2026 company_tickers.json mapped XOM to ExxonMobil Holdings Corp,
CIK 2115436, which files no 10-Ks; the operating company filing the 10-Ks
is Exxon Mobil Corp, CIK 34088). So a ticker is only accepted once the
resolved entity's own submission history proves it files that form.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

BASE_FILES = "https://www.sec.gov/files"
BASE_DATA = "https://data.sec.gov"
BASE_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

_MIN_INTERVAL = 0.15  # seconds between requests (~6.6 req/s max, under the 10/s cap)


@dataclass
class Filing:
    form: str
    accession: str          # with dashes, e.g. 0000320193-25-000079
    filing_date: str        # YYYY-MM-DD
    report_date: str        # period end, YYYY-MM-DD (may be "")
    primary_document: str   # filename of the main document

    @property
    def accession_nodash(self) -> str:
        return self.accession.replace("-", "")

    def document_url(self, cik: int) -> str:
        return f"{BASE_ARCHIVES}/{cik}/{self.accession_nodash}/{self.primary_document}"

    def index_url(self, cik: int) -> str:
        return f"{BASE_ARCHIVES}/{cik}/{self.accession_nodash}/{self.accession}-index.htm"


class EdgarClient:
    def __init__(self, email: str, cache_dir: Path, app_name: str = "secdiff/0.1.0"):
        self.user_agent = f"{app_name} (research tool; contact: {email})"
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_request = 0.0
        # Set by resolve_ticker() when the input was a company NAME that
        # matched exactly one ticker (see _resolve_by_name); None otherwise.
        # Callers use it to label reports with the real ticker.
        self.name_matched_ticker: str | None = None

    # -- low level ---------------------------------------------------------
    def _fetch(self, url: str, cache_name: str) -> bytes:
        cached = self.cache_dir / cache_name
        if cached.exists() and cached.stat().st_size > 0:
            return cached.read_bytes()
        wait = _MIN_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent,
                                                   "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (fixed SEC hosts)
            data = resp.read()
        self._last_request = time.monotonic()
        cached.write_bytes(data)
        return data

    def _fetch_json(self, url: str, cache_name: str) -> dict:
        return json.loads(self._fetch(url, cache_name).decode("utf-8", "replace"))

    # -- ticker resolution ---------------------------------------------------
    def _submissions(self, cik: int) -> dict:
        return self._fetch_json(f"{BASE_DATA}/submissions/CIK{cik:010d}.json",
                                f"submissions_{cik:010d}.json")

    def form_filing_count(self, cik: int, form: str) -> int:
        """How many recent filings of `form` this entity has on EDGAR."""
        recent = self._submissions(cik).get("filings", {}).get("recent", {})
        return sum(1 for f in recent.get("form", []) if f == form)

    def _candidates_from_tickers_file(self, ticker: str) -> list[tuple[int, str]]:
        data = self._fetch_json(f"{BASE_FILES}/company_tickers.json",
                                "company_tickers.json")
        return [(int(row["cik_str"]), row.get("title", ticker))
                for row in data.values()
                if row.get("ticker", "").upper() == ticker]

    def _candidates_from_exchange_file(self, ticker: str) -> list[tuple[int, str]]:
        data = self._fetch_json(f"{BASE_FILES}/company_tickers_exchange.json",
                                "company_tickers_exchange.json")
        out = []
        for row in data.get("data", []):
            # rows are [cik, name, ticker, exchange]
            if len(row) >= 3 and str(row[2]).upper() == ticker:
                out.append((int(row[0]), str(row[1])))
        return out

    def _candidate_from_browse_edgar(self, ticker: str) -> tuple[int, str] | None:
        """EDGAR's classic company search resolves tickers through a
        different mapping than the JSON ticker files; ask it as a fallback."""
        url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
               f"&ticker={ticker}&owner=include&count=10&output=atom")
        raw = self._fetch(url, f"browse_ticker_{ticker}.xml").decode("utf-8", "replace")
        cik_m = re.search(r"<cik>(\d+)</cik>", raw)
        if not cik_m:
            return None
        name_m = re.search(r"<conformed-name>([^<]+)</conformed-name>", raw)
        return int(cik_m.group(1)), (name_m.group(1).title() if name_m else ticker)

    def _name_matches(self, query: str) -> list[tuple[int, str, str]]:
        """Company-name matches for `query` in company_tickers.json.

        Matching is case-insensitive on the EDGAR company title and only
        ever returns the single best tier: exact title match, then title
        starts-with the query at a word boundary, then the query appears
        inside the title at a word boundary. Returns [(cik, ticker, title)]
        sorted by ticker; empty when no title matches at any tier.
        """
        data = self._fetch_json(f"{BASE_FILES}/company_tickers.json",
                                "company_tickers.json")
        q = " ".join(query.upper().split())
        if not q:
            return []
        word_re = re.compile(r"(?<![A-Z0-9])" + re.escape(q) + r"(?![A-Z0-9])")
        exact: list[tuple[int, str, str]] = []
        starts: list[tuple[int, str, str]] = []
        contains: list[tuple[int, str, str]] = []
        for row in data.values():
            title = " ".join(str(row.get("title", "")).upper().split())
            if not title:
                continue
            entry = (int(row["cik_str"]),
                     str(row.get("ticker", "")).upper(),
                     str(row.get("title", "")))
            if title == q:
                exact.append(entry)
            elif title.startswith(q) and not title[len(q)].isalnum():
                starts.append(entry)
            elif word_re.search(title):
                contains.append(entry)
        for tier in (exact, starts, contains):
            if tier:
                return sorted(tier, key=lambda e: e[1])
        return []

    @staticmethod
    def _ticker_variants(ticker: str) -> list[str]:
        """Spellings to try for a ticker, in order.

        Class shares are typed in market format with a dot (BRK.B), but
        EDGAR lists them with a dash (BRK-B) — and a few with a slash or no
        separator. Try the typed spelling first, then dot->dash, dot->slash,
        dot removed. Every variant still goes through the same verified
        resolution; a variant is never trusted on spelling alone.
        """
        variants = [ticker]
        if "." in ticker:
            for v in (ticker.replace(".", "-"), ticker.replace(".", "/"),
                      ticker.replace(".", "")):
                if v not in variants:
                    variants.append(v)
        return variants

    def _resolve_by_name(self, raw_query: str, form: str, min_count: int,
                         try_candidates, checked: list) -> tuple[int, str]:
        """Fallback for when the input is not a ticker at all: treat it as
        a company NAME and search EDGAR titles (see _name_matches).

        Exactly one best-tier match -> resolve to it (the entity must still
        pass the same form-filing check as a ticker candidate) and note the
        match on stderr. Several matches -> exit listing the candidates;
        a name is never guessed. No matches -> exit with the honest
        not-found error plus the subsidiary/parent hint.
        """
        matches = self._name_matches(raw_query)
        if not matches:
            msg = f"Ticker not found on EDGAR: {raw_query.strip().upper()}"
            msg += (" Note: enter a ticker symbol (e.g. AAPL) or the listed "
                    "company's name. Some brands are subsidiaries with no "
                    "filing of their own — e.g. YouTube is part of Alphabet, "
                    "so use GOOGL.")
            if "." in raw_query:
                msg += (" For class shares EDGAR uses a dash "
                        "(BRK-B not BRK.B).")
            raise SystemExit(msg)
        if len(matches) > 1:
            cands = "; ".join(f"{t} — {title}" for _, t, title in matches[:5])
            if len(matches) > 5:
                cands += f"; … and {len(matches) - 5} more"
            raise SystemExit(
                f"'{raw_query.strip()}' matches more than one listed "
                f"company — not guessing. Candidates: {cands}. "
                "Re-run with the ticker you mean.")
        cik0, tick0, title0 = matches[0]
        hit = try_candidates([(cik0, title0)])
        if hit:
            self.name_matched_ticker = tick0
            print(f"Note: Company name '{raw_query.strip()}' matched "
                  f"{tick0} — {title0}.", file=sys.stderr)
            return hit
        n = checked[-1][2] if checked else 0
        raise SystemExit(
            f"Company name '{raw_query.strip()}' matched {tick0} — {title0} "
            f"(CIK {cik0}), but it files only {n} recent {form} filing(s) "
            f"on EDGAR (need at least {min_count}); no diff can be built.")

    def resolve_ticker(self, ticker: str, form: str = "10-K",
                       min_count: int = 2) -> tuple[int, str]:
        """Resolve a ticker to the entity that actually files `form`.

        The ticker files are only a starting point: each candidate entity is
        accepted only if its own EDGAR submission history contains at least
        `min_count` recent filings of the requested form. Candidates come
        from, in order: company_tickers.json, company_tickers_exchange.json,
        and EDGAR's classic browse-edgar ticker lookup. If the typed spelling
        is not found, separator variants are tried (BRK.B -> BRK-B -> BRK/B
        -> BRKB). If the input is not a ticker at all, it is retried as a
        company NAME against the EDGAR titles (see _resolve_by_name) — but
        only after every ticker spelling has missed, so real tickers behave
        exactly as before. If no entity carrying this ticker files the form,
        exit with an error naming what was found.
        """
        raw_query = ticker
        ticker = ticker.upper().strip()
        self.name_matched_ticker = None
        checked: list[tuple[int, str, int]] = []  # cik, name, form count

        def try_candidates(cands: list[tuple[int, str]]) -> tuple[int, str] | None:
            for cik, name in cands:
                if any(c == cik for c, _, _ in checked):
                    continue
                n = self.form_filing_count(cik, form)
                checked.append((cik, name, n))
                if n >= min_count:
                    if checked[:-1]:
                        skipped = "; ".join(
                            f"{nm} (CIK {c}, {k} recent {form})"
                            for c, nm, k in checked[:-1])
                        print(f"Note: EDGAR's ticker files list {ticker} as "
                              f"{skipped} — not enough {form} filings there, "
                              f"so using {name} (CIK {cik}) instead.",
                              file=sys.stderr)
                    return cik, name
            return None

        for spelling in self._ticker_variants(ticker):
            hit = try_candidates(self._candidates_from_tickers_file(spelling))
            if not hit:
                hit = try_candidates(self._candidates_from_exchange_file(spelling))
            if not hit and " " not in spelling:
                # browse-edgar is a ticker lookup; a spelling with a space
                # is a company name, not a ticker — the name fallback below
                # handles it (and a space would break the lookup URL).
                browse = self._candidate_from_browse_edgar(spelling)
                if browse:
                    hit = try_candidates([browse])
            if hit:
                if spelling != ticker:
                    print(f"Note: EDGAR lists {ticker} as {spelling} — "
                          f"using that spelling.", file=sys.stderr)
                return hit
        if not checked:
            # Not a ticker under any spelling — retry as a company name.
            return self._resolve_by_name(raw_query, form, min_count,
                                         try_candidates, checked)
        found = "; ".join(f"{nm} (CIK {c}, {k} recent {form} filing(s))"
                          for c, nm, k in checked)
        raise SystemExit(
            f"No entity carrying ticker {ticker} files {form} on EDGAR "
            f"(need at least {min_count}). Found: {found}.")

    def cik_for_ticker(self, ticker: str, form: str = "10-K",
                       min_count: int = 2) -> tuple[int, str]:
        """Backwards-compatible name for resolve_ticker()."""
        return self.resolve_ticker(ticker, form=form, min_count=min_count)

    # -- public API ----------------------------------------------------------
    def recent_filings(self, cik: int, form: str, count: int = 2) -> list[Filing]:
        """Return the `count` most recent filings of `form` (e.g. '10-K')."""
        subs = self._submissions(cik)
        recent = subs.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        out: list[Filing] = []
        for i, f in enumerate(forms):
            if f == form:
                out.append(Filing(
                    form=f,
                    accession=recent["accessionNumber"][i],
                    filing_date=recent["filingDate"][i],
                    report_date=recent.get("reportDate", [""])[i] or "",
                    primary_document=recent["primaryDocument"][i],
                ))
            if len(out) == count:
                break
        if len(out) < count:
            raise SystemExit(
                f"Only {len(out)} {form} filing(s) found for CIK {cik}; need {count} to diff.")
        return out

    def filing_text(self, cik: int, filing: Filing) -> str:
        raw = self._fetch(filing.document_url(cik),
                          f"doc_{filing.accession_nodash}.html")
        return html_to_text(raw.decode("utf-8", "replace"))

    def company_facts(self, cik: int) -> dict:
        return self._fetch_json(f"{BASE_DATA}/api/xbrl/companyfacts/CIK{cik:010d}.json",
                                f"companyfacts_{cik:010d}.json")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n\s*\n+")


def html_to_text(html: str) -> str:
    """Very small HTML-to-text: drop scripts/styles/tags, keep line structure.

    Filing HTML is mostly paragraphs/tables; block-level closings become
    newlines so sentence splitting and ITEM heading detection still work.
    """
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)</(p|div|tr|table|h[1-6]|li|br)>", "\n", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = _TAG_RE.sub(" ", html)
    import html as _html_lib
    text = _html_lib.unescape(text)  # &#160; &#8217; &nbsp; etc.
    text = _WS_RE.sub(" ", text)
    text = _BLANK_RE.sub("\n", text)
    return text.strip()
