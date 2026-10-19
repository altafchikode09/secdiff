"""Risk Words Highlighter for report text.

A fixed, published list of risk terms is highlighted (HTML <mark>) in
the changed/deleted sentences of the HTML report, so a reader scanning
a long diff spots the legally/financially loaded words first. The list
is printed as a legend on every report that contains a highlight —
nothing is highlighted by a hidden rule.

Matching discipline:
- Deterministic: one compiled regex, case-insensitive, word boundaries
  on both sides (so "default" does not fire inside "defaults" or
  "defaulted", and "fraud" does not fire inside "fraudulent").
- Multi-word terms ("going concern", "material weakness") match as
  phrases.
- Never inside a URL (http(s)://… or www.… spans are passed through
  untouched) and never inside an HTML tag (<…> spans likewise) — the
  input here is already-escaped report text, and a highlight must
  never corrupt a link or markup.
- A highlight is a reading aid only: the word occurring in a sentence
  is not, by itself, a finding about the company.
"""
from __future__ import annotations

import re

# The published list. Reports print these terms in the legend.
RISK_TERMS: tuple[str, ...] = (
    "going concern",
    "material weakness",
    "bankruptcy",
    "covenant",
    "default",
    "fraud",
    "indictment",
    "investigation",
    "lawsuit",
    "litigation",
    "penalty",
    "restatement",
    "subpoena",
)

# Longest terms first so phrases win over any single-word overlap.
_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in
                      sorted(RISK_TERMS, key=lambda t: (-len(t), t))) + r")\b",
    re.IGNORECASE)

# Spans that must pass through untouched: URLs and HTML tags.
_PROTECT_RE = re.compile(r"(https?://[^\s<]+|www\.[^\s<]+|<[^>]*>)")


def highlight_risk(escaped_text: str) -> tuple[str, int]:
    """Highlight risk terms in already-HTML-escaped text.

    Returns (html_with_marks, number_of_highlights). Text without any
    term is returned unchanged with a count of 0.
    """
    if not escaped_text:
        return escaped_text, 0
    parts = _PROTECT_RE.split(escaped_text)
    count = 0
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:            # a protected span (URL or tag)
            out.append(part)
            continue
        new, n = _PATTERN.subn(r"<mark class='risk'>\g<0></mark>", part)
        count += n
        out.append(new)
    return "".join(out), count


def count_risk_terms(escaped_text: str) -> int:
    """How many highlights highlight_risk() would add (no markup made)."""
    return highlight_risk(escaped_text)[1]
