"""Share card: a downloadable PNG summary of one report.

Generated entirely with the standard library: a minimal PNG encoder
(zlib-compressed scanlines) and text drawn with a small built-in 5x7
bitmap font (below) — no image libraries, no external assets. The
card carries only numbers the report itself computed: the same counts,
the same Change-Intensity Score, and the single biggest deletion from
the same diff. Nothing is estimated or filled in; a report with no
deletions says so on the card.

Design: dark navy card, 1200x630 (the standard social-card size),
brand + company + form on top, the three change counts large in the
middle (deletions red, modifications amber, additions green), the
score with a bar, the biggest deletion as a short excerpt, and the
report URL along the bottom.

The font covers A-Z (lowercase renders as uppercase, as is normal for
this card style), digits and the punctuation filing text uses.
Characters outside the font are transliterated where a good ASCII
equivalent exists (– — ' ' " " • …) and otherwise dropped by
`card_text`, so card text is always renderable.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .diffing import SectionDiff
from .report import FilingInfo, _counts
from .score import compute_score
from .xbrl import XbrlResult

WIDTH, HEIGHT = 1200, 630

BG = (11, 18, 32)
ACCENT = (37, 99, 235)
WHITE = (241, 245, 249)
MUTED = (148, 163, 184)
RED = (248, 113, 113)
AMBER = (251, 191, 36)
GREEN = (74, 222, 128)
TRACK = (30, 41, 59)

# ---------------------------------------------------------------------------
# Built-in 5x7 font. Each glyph: 7 rows of 5 pixels ('#' = on).
_FONT: dict[str, tuple[str, ...]] = {
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".####", "#....", "#....", "#....", "#....", "#....", ".####"),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".####", "#....", "#....", "#.###", "#...#", "#...#", ".###."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "#####"),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", ".#.#.", "..#..", "..#..", "..#..", ".#.#.", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("#####", "...#.", "..#..", "...#.", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": (".###.", "#....", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", "..#..", "..#..", "..#.."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "....#", ".###."),
    " ": (".....", ".....", ".....", ".....", ".....", ".....", "....."),
    ".": (".....", ".....", ".....", ".....", ".....", ".##..", ".##.."),
    ",": (".....", ".....", ".....", ".....", ".##..", ".##..", ".#..."),
    ":": (".....", ".##..", ".##..", ".....", ".##..", ".##..", "....."),
    ";": (".....", ".##..", ".##..", ".....", ".##..", ".##..", ".#..."),
    "!": ("..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."),
    "?": (".###.", "#...#", "....#", "..##.", "..#..", ".....", "..#.."),
    "'": ("..#..", "..#..", ".#...", ".....", ".....", ".....", "....."),
    '"': (".#.#.", ".#.#.", ".....", ".....", ".....", ".....", "....."),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
    "+": (".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."),
    "/": ("....#", "....#", "...#.", "..#..", ".#...", "#....", "#...."),
    "(": ("...##", "..#..", ".#...", ".#...", ".#...", "..#..", "...##"),
    ")": ("##...", "..#..", "...#.", "...#.", "...#.", "..#..", "##..."),
    "&": (".##..", "#..#.", "#.#..", ".#...", "#.#.#", "#..#.", ".##.#"),
    "%": ("##..#", "##.#.", "....#", "...#.", "..#..", ".#.##", "#..##"),
    "$": ("..#..", ".####", "#.#..", ".###.", "..#.#", "####.", "..#.."),
    "#": (".#.#.", ".#.#.", "#####", ".#.#.", "#####", ".#.#.", ".#.#."),
    "@": (".###.", "#...#", "#.###", "#.#.#", "#....", ".####", "....."),
    "*": (".....", "#.#.#", ".###.", "#####", ".###.", "#.#.#", "....."),
    "=": (".....", ".....", "#####", ".....", "#####", ".....", "....."),
    "_": (".....", ".....", ".....", ".....", ".....", ".....", "#####"),
    "|": ("..#..", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "<": ("...##", "..#..", ".#...", "#....", ".#...", "..#..", "...##"),
    ">": ("##...", "..#..", "...#.", "....#", "...#.", "..#..", "##..."),
    "~": (".....", ".....", ".##.#", "#..#.", ".....", ".....", "....."),
}

_TRANSLATE = {
    "—": "-", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "•": "*", "·": "-",
    "…": "...", " ": " ",
}


def card_text(text: str) -> str:
    """Renderable card text: upper-cased (the font's case), with
    common non-font characters transliterated and anything else the
    font cannot draw dropped. Whitespace collapsed."""
    out = []
    for ch in str(text):
        ch = _TRANSLATE.get(ch, ch)
        for c in ch:
            up = c.upper()
            if up in _FONT:
                out.append(up)
            elif c.isspace():
                out.append(" ")
    return " ".join("".join(out).split())


def text_width(text: str, scale: int) -> int:
    """Pixel width of (already card_text-cleaned) text at `scale`:
    5 px glyph + 1 px spacing per character."""
    if not text:
        return 0
    return len(text) * 6 * scale - scale


def fit_text(text: str, max_width: int, scale: int) -> str:
    """Truncate text (with a '..' marker) until it fits max_width px.
    Honest truncation: the marker makes the cut visible."""
    if text_width(text, scale) <= max_width:
        return text
    while text and text_width(text + "..", scale) > max_width:
        text = text[:-1]
    return text.rstrip() + ".."


def wrap_card_text(text: str, max_width: int, scale: int,
                   max_lines: int) -> list[str]:
    """Word-wrap cleaned text to max_width px, at most max_lines
    lines; the last line gets a '..' marker if text remains."""
    per_line = max(1, (max_width + scale) // (6 * scale))
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        while len(w) > per_line:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:per_line])
            w = w[per_line:]
            if len(lines) >= max_lines:
                break
        if len(lines) >= max_lines:
            break
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= per_line:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if lines and " ".join(lines) != text:
        lines[-1] = fit_text(lines[-1] + "..", max_width, scale) \
            if not lines[-1].endswith("..") else lines[-1]
    return lines[:max_lines]


class _Canvas:
    def __init__(self, w: int, h: int, bg):
        self.w, self.h = w, h
        self.px = bytearray(bytes(bg) * (w * h))

    def set_px(self, x: int, y: int, color) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(color)

    def rect(self, x: int, y: int, w: int, h: int, color) -> None:
        for yy in range(max(0, y), min(self.h, y + h)):
            base = (yy * self.w) * 3
            for xx in range(max(0, x), min(self.w, x + w)):
                i = base + xx * 3
                self.px[i:i + 3] = bytes(color)

    def text(self, x: int, y: int, text: str, scale: int, color) -> int:
        """Draw cleaned text; returns the x just past the last glyph."""
        cx = x
        for ch in text:
            glyph = _FONT.get(ch)
            if glyph is None:
                glyph = _FONT[" "]
            for row_i, row in enumerate(glyph):
                for col_i, bit in enumerate(row):
                    if bit == "#":
                        self.rect(cx + col_i * scale, y + row_i * scale,
                                  scale, scale, color)
            cx += 6 * scale
        return cx


# ---------------------------------------------------------------------------
# Card data + rendering

@dataclass
class CardData:
    company: str
    ticker: str
    form: str
    new_date: str
    old_date: str
    deletions: int
    modifications: int
    additions: int
    xbrl_changed: int
    score_total: int
    score_band: str
    biggest_deletion: str        # "" when the report has no deletions
    report_url: str


def build_card_data(info: FilingInfo, diffs: list[SectionDiff],
                    xbrl: XbrlResult, report_url: str) -> CardData:
    """Assemble exactly what the card shows from a computed report.
    The biggest deletion is the largest deletion hunk (by the diff's
    own size ranking); ties keep the report's display order."""
    d, m, a = _counts(diffs)
    score = compute_score(diffs, xbrl)
    biggest = ""
    biggest_size = -1
    for s in diffs:
        for h in s.hunks:
            if h.kind == "deletion" and h.size > biggest_size:
                biggest, biggest_size = h.old, h.size
    return CardData(
        company=info.company, ticker=info.ticker, form=info.form,
        new_date=info.new_date, old_date=info.old_date,
        deletions=d, modifications=m, additions=a,
        xbrl_changed=len(xbrl.rows), score_total=score.total,
        score_band=score.band, biggest_deletion=biggest,
        report_url=report_url)


_MARGIN = 60
_CONTENT_W = WIDTH - 2 * _MARGIN


def render_card_png(data: CardData) -> bytes:
    """Render the card to PNG bytes. Deterministic: the same card
    data always produces the same image."""
    c = _Canvas(WIDTH, HEIGHT, BG)
    c.rect(0, 0, WIDTH, 10, ACCENT)                       # top bar
    x = _MARGIN
    c.text(x, 40, "SECDIFF", 5, WHITE)
    sub = "GIT DIFF FOR SEC FILINGS"
    c.text(WIDTH - _MARGIN - text_width(sub, 2), 52, sub, 2, MUTED)
    c.text(x, 92, "WHAT CHANGED - AND WHAT DISAPPEARED", 2, MUTED)

    company = fit_text(card_text(data.company), _CONTENT_W, 6)
    c.text(x, 132, company, 6, WHITE)
    c.text(x, 196, card_text(f"{data.ticker} - {data.form}"), 4,
           (96, 165, 250))
    c.text(x, 238, card_text(f"{data.new_date}  VS  {data.old_date}"),
           3, MUTED)

    # The three counts, big, in their report colours.
    stats = [(str(data.deletions), "DELETIONS", RED),
             (str(data.modifications), "CHANGED", AMBER),
             (str(data.additions), "ADDED", GREEN)]
    col_w = _CONTENT_W // 3
    for i, (num, label, color) in enumerate(stats):
        cx = x + i * col_w
        c.text(cx, 292, num, 10, color)
        c.text(cx, 292 + 7 * 10 + 12, label, 3, color)
    c.text(x, 404, card_text(f"{data.xbrl_changed} NUMBERS CHANGED "
                             f"(XBRL)"), 3, MUTED)

    # Score + bar.
    band_color = RED if data.score_total >= 75 else \
        AMBER if data.score_total >= 50 else \
        (96, 165, 250) if data.score_total >= 25 else MUTED
    c.text(x, 444, card_text(f"SCORE {data.score_total}/100 - "
                             f"{data.score_band}"), 4, WHITE)
    c.rect(x, 486, _CONTENT_W, 16, TRACK)
    c.rect(x, 486, _CONTENT_W * data.score_total // 100, 16, band_color)

    # Biggest deletion excerpt (two lines at most, clear of the URL).
    c.text(x, 518, "BIGGEST DELETION:", 2, MUTED)
    if data.biggest_deletion:
        excerpt = card_text(data.biggest_deletion)
        lines = wrap_card_text(excerpt, _CONTENT_W, 3, 2)
    else:
        lines = ["NO DELETIONS IN THIS REPORT"]
    for i, ln in enumerate(lines):
        c.text(x, 540 + i * 25, ln, 3, WHITE)

    url = fit_text(card_text(data.report_url), _CONTENT_W, 2)
    c.text(x, HEIGHT - 32, url, 2, (96, 165, 250))
    return _encode_png(c)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + kind + payload +
            struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


def _encode_png(canvas: _Canvas) -> bytes:
    raw = bytearray()
    row_bytes = canvas.w * 3
    for y in range(canvas.h):
        raw.append(0)                                    # filter: none
        raw += canvas.px[y * row_bytes:(y + 1) * row_bytes]
    ihdr = struct.pack(">IIBBBBB", canvas.w, canvas.h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) +
            _chunk(b"IDAT", zlib.compress(bytes(raw), 6)) +
            _chunk(b"IEND", b""))
