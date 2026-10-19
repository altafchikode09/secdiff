"""Summary PDF export for a report — hand-rolled, stdlib only.

The PDF is deliberately a SUMMARY, and says so on its face: the report
header (company, form, both filings), the change counts, the
Change-Intensity Score with its breakdown, and the top deletions —
not a pixel copy of the HTML page (the full report lives on the
website and in the CLI's HTML/Markdown output).

The writer is a minimal, correct PDF 1.4 producer built from the
standard library alone:

- Base-14 fonts (Helvetica / Helvetica-Bold) — referenced, never
  embedded, so no font files are needed.
- Content streams are Flate-compressed (`zlib`).
- The cross-reference table is computed from the real byte offsets of
  every object, so any standard reader (and the test-suite's own xref
  parser) can random-access the file.
- No timestamps, no document IDs: the same report produces the same
  bytes every time.

Text handling: filing prose contains characters outside PDF's
WinAnsi encoding (em dashes, smart quotes, bullets, emoji…). Common
punctuation is transliterated to ASCII equivalents and anything else
unencodable is replaced with "?" — visible, honest degradation of
individual characters, never a corrupted file: every string is then
Latin-1 bytes with ( ) \\ escaped, which is always a valid PDF string.
"""
from __future__ import annotations

import zlib

from .diffing import SectionDiff
from .report import FORM_NOTES, FilingInfo, _counts
from .score import compute_score
from .xbrl import XbrlResult

# Page geometry (US Letter, points) and text layout.
PAGE_W, PAGE_H = 612, 792
MARGIN = 54
TOP_Y = PAGE_H - MARGIN
BOTTOM_Y = MARGIN
USABLE_W = PAGE_W - 2 * MARGIN

TOP_DELETIONS = 25          # the summary's deletion list is capped
_MAX_DELETION_CHARS = 700   # per-deletion excerpt cap inside the PDF

# Transliteration for characters filings use constantly. Anything not
# here and not Latin-1 encodable becomes "?".
_TRANSLATE = {
    "—": "-", "–": "-", "―": "-",
    "‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"',
    "•": "*", "·": "-", "…": "...", "→": "->", "←": "<-",
    " ": " ", "​": "", "﻿": "",
    "🔴": "", "🟡": "", "🟢": "",
}


def pdf_safe_text(text: str) -> str:
    """Make filing text safe for a WinAnsi PDF string: transliterate
    the common non-ASCII punctuation, drop/replace the rest, and
    collapse whitespace. The result is always Latin-1 encodable."""
    out = []
    for ch in str(text):
        if ch in _TRANSLATE:
            out.append(_TRANSLATE[ch])
        elif ord(ch) < 128:
            out.append(ch)
        else:
            try:
                ch.encode("latin-1")
                out.append(ch)
            except UnicodeEncodeError:
                out.append("?")
    return " ".join("".join(out).split())


def _escape_pdf_string(text: str) -> bytes:
    raw = pdf_safe_text(text).encode("latin-1")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(") \
        .replace(b")", b"\\)")


def _chars_per_line(size: float) -> int:
    """Conservative wrap width for Helvetica at `size` points: the
    average glyph is ~0.5 em wide; 0.55 leaves headroom so wrapped
    lines never run past the right margin."""
    return max(10, int(USABLE_W / (0.55 * size)))


def wrap_text(text: str, size: float) -> list[str]:
    """Word-wrap text to the usable page width at the given font size
    (operates on pdf_safe_text output, so widths are honest)."""
    limit = _chars_per_line(size)
    words = pdf_safe_text(text).split()
    lines: list[str] = []
    cur = ""
    for w in words:
        while len(w) > limit:               # a single huge "word"
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:limit])
            w = w[limit:]
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= limit:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


class _Line:
    """One laid-out line: text in font `font` ('F1' regular / 'F2'
    bold) at `size`, followed by `leading` points of vertical space."""
    __slots__ = ("text", "font", "size", "leading")

    def __init__(self, text, font="F1", size=10.0, leading=None):
        self.text = text
        self.font = font
        self.size = size
        self.leading = leading if leading is not None else size * 1.35


def _layout(info: FilingInfo, diffs: list[SectionDiff],
            xbrl: XbrlResult) -> list[_Line]:
    """The summary's content, top to bottom. Pure layout: every value
    comes from the report's own computed diff/XBRL/score — nothing is
    generated here."""
    d, m, a = _counts(diffs)
    score = compute_score(diffs, xbrl)
    lines: list[_Line] = []

    def add(text, font="F1", size=10.0, leading=None, wrap_size=None):
        for ln in wrap_text(text, wrap_size if wrap_size else size):
            lines.append(_Line(ln, font, size, leading))

    add(f"secdiff - {info.company} ({info.ticker}) {info.form}",
        font="F2", size=16, leading=22)
    add("git diff for SEC filings - summary PDF", size=9.5, leading=16)
    add(f"Newer filing: {info.new_date} - accession {info.new_accession}",
        size=9.5)
    add(f"Older filing: {info.old_date} - accession {info.old_accession}",
        size=9.5, leading=18)
    add(f"Changes: {d} deletions - {m} modifications - {a} additions - "
        f"{len(xbrl.rows)} XBRL numbers changed", font="F2", size=11,
        leading=18)
    add(f"Change-intensity score: {score.total}/100 ({score.band})",
        font="F2", size=11)
    add(score.breakdown(), size=9, wrap_size=9)
    add(f"Section-detection confidence: {info.section_confidence}. "
        "Deterministic diff computed by code (no AI).", size=9,
        leading=17)
    if info.form in FORM_NOTES:
        add(f"About this form: {FORM_NOTES[info.form]}", size=9,
            wrap_size=9, leading=17)

    deletions = [(s, h) for s in diffs for h in s.hunks
                 if h.kind == "deletion"]
    add("", leading=6)
    add(f"Top deletions (deletions first; showing "
        f"{min(len(deletions), TOP_DELETIONS)} of {len(deletions)})",
        font="F2", size=12, leading=19)
    if not deletions:
        add("No deletions in this report.")
    for i, (section, h) in enumerate(deletions[:TOP_DELETIONS], 1):
        text = h.old
        suffix = ""
        if len(text) > _MAX_DELETION_CHARS:
            text = text[:_MAX_DELETION_CHARS].rstrip() + " ..."
            suffix = " [truncated - full text in the online report]"
        where = ("full document" if section.item == "FULL"
                 else f"Item {section.item}")
        add(f"{i}. DELETED ({where}): {text}{suffix}", size=9.5,
            wrap_size=9.5, leading=14)

    add("", leading=8)
    add("This is a summary PDF: it carries the header, the score and "
        "the top deletions only. The full report - every change, the "
        "numbers table and the EDGAR source links - is the secdiff "
        "report for this filing pair.", size=9, wrap_size=9)
    add("Generated by secdiff. Data: SEC EDGAR (public filings). "
        "Not investment advice.", size=9, wrap_size=9)
    return lines


def _page_stream(lines: list[_Line]) -> bytes:
    ops = [b"BT"]
    y = TOP_Y
    for ln in lines:
        if ln.text:
            ops.append(b"/%s %g Tf" % (ln.font.encode(), ln.size))
            ops.append(b"1 0 0 1 %d %g Tm" % (MARGIN, y))
            ops.append(b"(" + _escape_pdf_string(ln.text) + b") Tj")
        y -= ln.leading
    ops.append(b"ET")
    return zlib.compress(b"\n".join(ops), 6)


def _paginate(lines: list[_Line]) -> list[list[_Line]]:
    pages: list[list[_Line]] = []
    cur: list[_Line] = []
    y = TOP_Y
    for ln in lines:
        if cur and y - ln.leading < BOTTOM_Y:
            pages.append(cur)
            cur, y = [], TOP_Y
        cur.append(ln)
        y -= ln.leading
    if cur or not pages:
        pages.append(cur)
    return pages


def render_pdf(info: FilingInfo, diffs: list[SectionDiff],
               xbrl: XbrlResult) -> bytes:
    """The summary PDF for one report, as bytes. Deterministic: the
    same report always produces the same file."""
    pages = _paginate(_layout(info, diffs, xbrl))
    streams = [_page_stream(p) for p in pages]

    # Object plan: 1 Catalog, 2 Pages, 3/4 fonts, then per page a Page
    # object followed by its Contents object.
    objects: list[bytes] = []
    page_obj_nums = []
    num = 5
    for _ in pages:
        page_obj_nums.append(num)
        num += 2
    kids = " ".join(f"{n} 0 R" for n in page_obj_nums)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>"
                   .encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                   b"/Encoding /WinAnsiEncoding >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont "
                   b"/Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    for i, stream in enumerate(streams):
        objects.append(
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} "
             f"{PAGE_H}] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
             f"/Contents {page_obj_nums[i] + 1} 0 R >>").encode())
        objects.append(b"<< /Length %d /Filter /FlateDecode >>\nstream\n"
                       % len(stream) + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
            % (len(objects) + 1, xref_at))
    return bytes(out)
