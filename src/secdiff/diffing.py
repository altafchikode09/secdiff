"""Deterministic sentence-level diff.

No LLM, no randomness: the same two filings always produce the same diff.
Changes are classified as DELETIONS, MODIFICATIONS or ADDITIONS and ranked
by the size of the change, deletions first — deleted language (for example
a dropped risk-factor sentence) is the headline use case for secdiff.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“])")
_WS_RE = re.compile(r"\s+")


def split_sentences(text: str) -> list[str]:
    parts = []
    for chunk in _SENT_RE.split(text.replace("\n", " ")):
        s = _WS_RE.sub(" ", chunk).strip()
        if len(s) >= 25:  # drop table fragments / page numbers / headers
            parts.append(s)
    return parts


def _key(s: str) -> str:
    return _WS_RE.sub(" ", s.lower()).strip()


@dataclass
class Hunk:
    kind: str            # "deletion" | "modification" | "addition"
    old: str = ""        # old sentence ("" for additions)
    new: str = ""        # new sentence ("" for deletions)
    size: int = 0        # ranking score: characters changed


@dataclass
class SectionDiff:
    item: str
    title: str
    hunks: list[Hunk] = field(default_factory=list)
    old_sentences: int = 0
    new_sentences: int = 0

    @property
    def total_change(self) -> int:
        return sum(h.size for h in self.hunks)


_KIND_ORDER = {"deletion": 0, "modification": 1, "addition": 2}
SIMILARITY_FOR_MODIFICATION = 0.55


def diff_section(item: str, title: str, old_text: str, new_text: str,
                 max_hunks: int = 60) -> SectionDiff:
    old_s = split_sentences(old_text)
    new_s = split_sentences(new_text)
    old_k = [_key(s) for s in old_s]
    new_k = [_key(s) for s in new_s]
    sm = difflib.SequenceMatcher(a=old_k, b=new_k, autojunk=False)
    hunks: list[Hunk] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        olds, news = old_s[i1:i2], new_s[j1:j2]
        if tag == "delete":
            hunks += [Hunk("deletion", old=o, size=len(o)) for o in olds]
        elif tag == "insert":
            hunks += [Hunk("addition", new=n, size=len(n)) for n in news]
        else:  # replace — pair sentences greedily by similarity
            remaining_new = list(news)
            for o in olds:
                best, best_ratio = None, 0.0
                for n in remaining_new:
                    r = difflib.SequenceMatcher(None, _key(o), _key(n)).ratio()
                    if r > best_ratio:
                        best, best_ratio = n, r
                if best is not None and best_ratio >= SIMILARITY_FOR_MODIFICATION:
                    hunks.append(Hunk("modification", old=o, new=best,
                                      size=int(abs(len(best) - len(o)) +
                                               (1 - best_ratio) * max(len(o), len(best)))))
                    remaining_new.remove(best)
                else:
                    hunks.append(Hunk("deletion", old=o, size=len(o)))
            hunks += [Hunk("addition", new=n, size=len(n)) for n in remaining_new]
    hunks.sort(key=lambda h: (_KIND_ORDER[h.kind], -h.size))
    return SectionDiff(item=item, title=title, hunks=hunks[:max_hunks],
                       old_sentences=len(old_s), new_sentences=len(new_s))
