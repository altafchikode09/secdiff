"""Search inside a computed report ("search-in-report").

A visitor can filter a report's text changes to only the hunks that
mention a word or phrase (for example "lawsuit" or "going concern").
The search is a plain, deterministic, case-insensitive substring match
over the hunk text the report already computed — it runs AFTER the
section / risk-only / deletions-only / top-N filters, on exactly the
hunks those filters produced, and it invents nothing: a hunk either
contains the term or it does not.

Two different jobs live here:

- Filtering (`filter_diffs_by_term`): returns the diffs reduced to the
  matching hunks plus the match count, so the page can say honestly
  "N changes mention 'X'" (N = matching hunks; a hunk mentioning the
  term three times still counts once — it is one change).
- Marking (`mark_search_term`): wraps the term in <mark class='search'>
  in already-HTML-escaped text, with the same discipline as the risk
  highlighter: never inside a URL and never inside an HTML tag, so a
  mark can never corrupt a link or markup.

Term hygiene (`clean_search_term`) is the URL/HTML safety boundary:
the raw `q` parameter is length-capped, stripped of control
characters and of the characters that could break out of an HTML
attribute or a URL query value (< > " ` \\ ^ { | }), and a term with
no letter or digit at all is rejected. What survives is always shown
HTML-escaped and always re-encoded with urlencode when it is put back
into a link.
"""
from __future__ import annotations

import re

from .diffing import Hunk, SectionDiff

# A search is a word or short phrase; 80 chars is generous and keeps
# URLs and pages sane.
MAX_TERM_LENGTH = 80

# Characters that must never reach a page or a link through a search
# term: markup/quote breakers and control characters. Everything else
# printable (letters, digits, spaces, . , & ' ( ) - / : ; ! ? % $ # @
# * + = ~ and non-ASCII letters) is accepted and escaped on output.
_FORBIDDEN_RE = re.compile(r"[\x00-\x1f\x7f<>\"`\\^{}|]")
_HAS_WORD_CHAR_RE = re.compile(r"[0-9A-Za-zÀ-ɏ]")


def clean_search_term(raw: str | None) -> str:
    """Validate and normalise a raw search term.

    Returns "" when no search was requested (None/blank). Raises
    ValueError with a visitor-friendly message for a term that is too
    long, contains forbidden characters, or has no searchable word in
    it — the web layer turns that into the usual honest 400 page.
    """
    if raw is None:
        return ""
    term = " ".join(str(raw).split())          # collapse all whitespace
    if not term:
        return ""
    if len(term) > MAX_TERM_LENGTH:
        raise ValueError(
            f"Search terms are limited to {MAX_TERM_LENGTH} characters — "
            "please search for a shorter word or phrase.")
    if _FORBIDDEN_RE.search(term):
        raise ValueError(
            "That search term contains characters that can't be used "
            "in a search (such as <, > or \"). Please search for a "
            "plain word or phrase instead.")
    if not _HAS_WORD_CHAR_RE.search(term):
        raise ValueError(
            "Please enter a word or phrase to search for (letters or "
            "numbers) — punctuation alone can't be searched.")
    return term


def hunk_matches(hunk: Hunk, term: str) -> bool:
    """True when the term occurs (case-insensitively) in the hunk's
    old or new text. Whole-phrase: the term is matched as one string,
    never word-by-word."""
    needle = term.lower()
    return needle in hunk.old.lower() or needle in hunk.new.lower()


def count_matches(diffs: list[SectionDiff], term: str) -> int:
    """How many hunks (changes) mention the term."""
    return sum(1 for s in diffs for h in s.hunks if hunk_matches(h, term))


def filter_diffs_by_term(diffs: list[SectionDiff], term: str,
                         ) -> tuple[list[SectionDiff], int]:
    """Reduce diffs to the hunks that mention `term`.

    Section order and hunk order are preserved; sections left with no
    matching hunk are dropped (the renderer skips empty sections in
    exactly the same way). Returns (filtered_diffs, match_count).
    """
    out: list[SectionDiff] = []
    matches = 0
    for s in diffs:
        kept = [h for h in s.hunks if hunk_matches(h, term)]
        if kept:
            matches += len(kept)
            out.append(SectionDiff(item=s.item, title=s.title, hunks=kept,
                                   old_sentences=s.old_sentences,
                                   new_sentences=s.new_sentences))
    return out, matches


# Spans that must pass through untouched: URLs and HTML tags — the same
# protection the risk highlighter uses.
_PROTECT_RE = re.compile(r"(https?://[^\s<]+|www\.[^\s<]+|<[^>]*>)")


def mark_search_term(escaped_text: str, term: str) -> tuple[str, int]:
    """Wrap occurrences of `term` in <mark class='search'> inside
    already-HTML-escaped text. Returns (html, number_of_marks).

    Literal match (the term is regex-escaped, so "C++" or "a.b" match
    themselves, never a pattern), case-insensitive. Protected spans
    (URLs, tags) are never marked.
    """
    if not escaped_text or not term:
        return escaped_text, 0
    pattern = re.compile(re.escape(term), re.IGNORECASE)
    parts = _PROTECT_RE.split(escaped_text)
    count = 0
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:            # a protected span (URL or tag)
            out.append(part)
            continue
        new, n = pattern.subn(r"<mark class='search'>\g<0></mark>", part)
        count += n
        out.append(new)
    return "".join(out), count
