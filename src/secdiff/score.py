"""The Change-Intensity Score (the "Chhupaya Score").

A transparent, deterministic number shown at the top of every report.
It is computed ONLY from the diff and XBRL result the report already
shows — same inputs in, same score out, always. Nothing is estimated,
learned or generated: the formula below is published verbatim in every
report ("How is this calculated?"), and each component's raw count and
capped points are printed next to the total so anyone can recompute it
by hand from the changes listed on the page.

What it measures: how much language DISAPPEARED or was rewritten in
the places readers most often care about — the Risk Factors and Legal
Proceedings sections — plus tracked XBRL numbers that the newer filing
no longer reports. What it does NOT measure: whether anything improper
happened. Companies rewrite and retag language every year for ordinary
reasons; a high score means "a lot changed here — worth reading", never
"wrongdoing". It is a change-intensity score, NOT investment advice
and NOT a judgement about the company.

Formula (each component capped, total 0–100):
  score = min(40, 5 × deletions in risk/legal sections)
        + min(30, 1 × total deletions)
        + min(20, 5 × disappeared XBRL concepts)
        + min(10, 2 × modifications in risk/legal sections)

"Risk/legal section" = a section whose item is 1A or 3, or whose title
mentions risk or legal matters (covers 10-K, 10-Q Part II "Legal
Proceedings", and heading-based titles on other forms).
"Disappeared XBRL concept" = a tracked concept the OLDER filing
reported and the newer one does not (see xbrl.compute_delta) — often
an innocent retag, listed individually in the report, never merged.
"""
from __future__ import annotations

from dataclasses import dataclass

from .diffing import SectionDiff
from .xbrl import XbrlResult

# Published verbatim in reports (report.py shows this text).
FORMULA_TEXT = (
    "Change-intensity score = min(40, 5 × deletions in Risk Factors / "
    "Legal sections) + min(30, 1 × total deletions) + min(20, 5 × "
    "tracked XBRL numbers reported in the older filing but not the "
    "newer one) + min(10, 2 × modifications in Risk Factors / Legal "
    "sections). Computed deterministically from the changes listed in "
    "this report only. It measures how much changed — it is not "
    "investment advice and not a judgement about the company."
)

_SENSITIVE_ITEMS = {"1A", "3"}
_SENSITIVE_TITLE_WORDS = ("risk", "legal")


def is_sensitive_section(diff: SectionDiff) -> bool:
    """True for Risk Factors / Legal Proceedings style sections."""
    if diff.item in _SENSITIVE_ITEMS:
        return True
    title = diff.title.lower()
    return any(w in title for w in _SENSITIVE_TITLE_WORDS)


@dataclass
class ScoreResult:
    total: int                       # 0..100
    band: str                        # human label for the total
    sensitive_deletions: int = 0     # raw counts behind each component
    total_deletions: int = 0
    disappeared_xbrl: int = 0
    sensitive_modifications: int = 0
    points_sensitive_deletions: int = 0    # capped points per component
    points_total_deletions: int = 0
    points_disappeared_xbrl: int = 0
    points_sensitive_modifications: int = 0

    def breakdown(self) -> str:
        """One line naming every raw count and its capped points, so
        the total can be verified by hand from the report itself."""
        return (
            f"{self.sensitive_deletions} Risk/Legal deletions ×5 = "
            f"{self.points_sensitive_deletions} · "
            f"{self.total_deletions} total deletions ×1 = "
            f"{self.points_total_deletions} · "
            f"{self.disappeared_xbrl} disappeared XBRL numbers ×5 = "
            f"{self.points_disappeared_xbrl} · "
            f"{self.sensitive_modifications} Risk/Legal modifications "
            f"×2 = {self.points_sensitive_modifications}")


def band_for(total: int) -> str:
    if total <= 0:
        return "No change detected"
    if total < 25:
        return "Low"
    if total < 50:
        return "Moderate"
    if total < 75:
        return "High"
    return "Very high"


def compute_score(diffs: list[SectionDiff], xbrl: XbrlResult) -> ScoreResult:
    """The score for exactly the diff a report displays. Pure function
    of its inputs (counts only — hunk sizes and ordering never matter),
    so the same report always scores the same."""
    sensitive_deletions = 0
    sensitive_modifications = 0
    total_deletions = 0
    for s in diffs:
        sensitive = is_sensitive_section(s)
        for h in s.hunks:
            if h.kind == "deletion":
                total_deletions += 1
                if sensitive:
                    sensitive_deletions += 1
            elif h.kind == "modification" and sensitive:
                sensitive_modifications += 1
    disappeared = xbrl.disappeared
    p_sd = min(40, 5 * sensitive_deletions)
    p_td = min(30, total_deletions)
    p_dx = min(20, 5 * disappeared)
    p_sm = min(10, 2 * sensitive_modifications)
    total = p_sd + p_td + p_dx + p_sm
    return ScoreResult(
        total=total, band=band_for(total),
        sensitive_deletions=sensitive_deletions,
        total_deletions=total_deletions,
        disappeared_xbrl=disappeared,
        sensitive_modifications=sensitive_modifications,
        points_sensitive_deletions=p_sd,
        points_total_deletions=p_td,
        points_disappeared_xbrl=p_dx,
        points_sensitive_modifications=p_sm)
