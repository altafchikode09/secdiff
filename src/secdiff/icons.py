"""PWA icons + manifest, generated with the standard library only.

The icons are drawn with the share card's canvas/bitmap font (see
secdiff.card): the dark-navy brand square, a big "S", and two short
bars under it in the diff colours (red for a deletion, green for an
addition). Same size in -> same PNG bytes out, so the icons are
stable across deploys and safe to cache.

The manifest points at the two icon sizes the routes serve
(/icon-192.png, /icon-512.png, plus /apple-touch-icon.png at 192).
There is deliberately no service worker — see secdiff.theme for why.
"""
from __future__ import annotations

import json

from .card import _Canvas, _encode_png
from .theme import PALETTES

ICON_SIZES = (192, 512)

_BG = (11, 18, 32)          # the card's dark navy
_ACCENT = (37, 99, 235)
_WHITE = (241, 245, 249)
_RED = (248, 113, 113)
_GREEN = (74, 222, 128)


def render_icon_png(size: int) -> bytes:
    """The secdiff icon at `size`x`size` px, as PNG bytes.

    Layout scales with the canvas: accent bar on top, the S glyph
    centred, and the red/green diff bars beneath it."""
    if size < 16:
        raise ValueError("icon size must be at least 16px")
    c = _Canvas(size, size, _BG)
    bar_h = max(2, size // 51)
    c.rect(0, 0, size, bar_h, _ACCENT)
    scale = max(1, size // 9)                    # S glyph: 5x7 cells
    glyph_w, glyph_h = 5 * scale, 7 * scale
    x = (size - glyph_w) // 2
    y = (size - glyph_h) // 2 - size // 10
    c.text(x, y, "S", scale, _WHITE)
    bar_y = y + glyph_h + size // 9
    bar_w = (glyph_w - size // 25) // 2
    bar_t = max(2, size // 28)
    c.rect(x, bar_y, bar_w, bar_t, _RED)
    c.rect(x + glyph_w - bar_w, bar_y, bar_w, bar_t, _GREEN)
    return _encode_png(c)


def manifest() -> dict:
    """The web app manifest. Colours come from the theme palettes,
    so the installed app's chrome matches the site's themes."""
    return {
        "name": "secdiff",
        "short_name": "secdiff",
        "description": "git diff for SEC filings — what changed, "
                       "deletions first.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": PALETTES["dark"]["bg"],
        "theme_color": PALETTES["dark"]["bg"],
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": "/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
        ],
    }


def manifest_bytes() -> bytes:
    return (json.dumps(manifest(), indent=2) + "\n").encode("utf-8")
