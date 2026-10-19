"""Repeat-deletion patterns ("pehle bhi aisa hua tha").

A deletion in the current report is annotated when the SAME sentence
was also deleted in the immediately-earlier comparison pair — e.g. a
sentence that a company's 2023 10-K still had, the 2024 10-K dropped
from one place, and the 2025 10-K dropped from the remaining place.
That pattern is real and common (boilerplate and risk language often
lives in several sections and is removed in stages).

Matching is deliberately strict and fully deterministic — no fuzzy
matching, no guessing:

- A sentence counts only if its normalised form (the diff engine's own
  key: lower-cased, whitespace collapsed) has at least MIN_WORDS words,
  so short fragments and table debris can never "repeat".
- The normalised form must be EXACTLY equal on both sides.
- Only deletion hunks are annotated, and only when the earlier pair
  actually produced that deletion. When the earlier pair cannot be
  computed, nothing is annotated — silence over speculation.
"""
from __future__ import annotations

from .diffing import SectionDiff, _key

MIN_WORDS = 8

NOTE_TEXT = "Also deleted in the previous cycle"


def deleted_keys(diffs: list[SectionDiff]) -> set[str]:
    """Normalised keys of every deletion sentence of >= MIN_WORDS
    words in a diff (across all sections)."""
    keys: set[str] = set()
    for s in diffs:
        for h in s.hunks:
            if h.kind == "deletion":
                k = _key(h.old)
                if len(k.split()) >= MIN_WORDS:
                    keys.add(k)
    return keys


def annotate_repeats(diffs: list[SectionDiff],
                     earlier_diffs: list[SectionDiff],
                     earlier_url: str = "") -> int:
    """Set the repeat note on current deletion hunks whose sentence
    was also deleted in `earlier_diffs`. Returns how many hunks were
    annotated. Hunks are shared objects, so annotations survive the
    report's display filters exactly like the hunks themselves."""
    earlier = deleted_keys(earlier_diffs)
    if not earlier:
        return 0
    n = 0
    for s in diffs:
        for h in s.hunks:
            if h.kind != "deletion":
                continue
            k = _key(h.old)
            if len(k.split()) >= MIN_WORDS and k in earlier:
                h.note = NOTE_TEXT
                h.note_url = earlier_url
                n += 1
    return n
