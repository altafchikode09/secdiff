"""Multi-year XBRL trend charts for the report's key numbers.

For a few headline concepts (Revenue, Net income, Total assets,
Long-term debt) the report shows the company's last 3–5 ANNUAL values
as a small bar chart, so a two-filing delta can be read against the
longer trend. Data comes from EDGAR's company-concept API
(https://data.sec.gov/api/xbrl/companyconcept/…), fetched through the
same paced, cached EdgarClient as everything else.

Honesty rules (the same spirit as xbrl.py):
- ONE alias and ONE unit per chart. Aliases are tried in the
  catalogue's priority order and the alias with the most usable annual
  points wins (ties keep priority order) — series from different tags
  are never merged, and a unit series other than the concept's
  expected unit (e.g. EUR for a money concept) is never used.
- Annual filings only: facts whose form is 10-K / 10-K/A. Flow
  concepts additionally require a fiscal-year duration (~300–400
  days), exactly as the delta table does, so a quarter is never
  labelled a year.
- One value per fiscal period end. When several filings report the
  same period (comparative columns, amendments, restatements), the
  most recently FILED value wins and the chart is labelled "as most
  recently reported" — restatements are never silently blended.
- Fewer than 2 usable points -> no chart at all. A missing chart
  means "not enough comparable annual data", never a guessed line.
- Every point is labelled with its fiscal year (the period-end year)
  and its formatted value; the concept's EDGAR source is linked.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

from .concepts import CONCEPTS, Concept
from .edgar import BASE_DATA
from .xbrl import _UNIT_PREFERENCE, _span_days, fmt_money

# The headline concepts that get a trend chart, by catalogue label.
CHART_LABELS: tuple[str, ...] = (
    "Revenue",
    "Net income",
    "Total assets",
    "Long-term debt (noncurrent)",
)

MAX_YEARS = 5


@dataclass
class ChartPoint:
    fy: str          # "FY2024" — fiscal-year label from the period end
    end: str         # period end date, YYYY-MM-DD
    value: float


@dataclass
class ChartSeries:
    label: str                       # catalogue label, e.g. "Revenue"
    alias: str                       # the single us-gaap/dei tag used
    taxonomy: str
    unit: str                        # the single unit of every point
    points: list[ChartPoint] = field(default_factory=list)
    source_url: str = ""             # EDGAR company-concept API URL


def concept_api_url(cik: int, taxonomy: str, alias: str) -> str:
    return (f"{BASE_DATA}/api/xbrl/companyconcept/CIK{cik:010d}/"
            f"{taxonomy}/{alias}.json")


def _annual_points(entries: list[dict], kind: str,
                   max_years: int) -> list[ChartPoint]:
    """Usable annual points from one alias's one unit series.

    One value per period end; the most recently filed fact wins a
    period (restated values replace earlier ones — the chart says so).
    """
    by_end: dict[str, tuple[str, float]] = {}
    for e in entries:
        if not str(e.get("form", "")).startswith("10-K"):
            continue                        # annual filings only
        end = e.get("end")
        val = e.get("val")
        if not end or val is None:
            continue
        if kind == "flow":
            if not e.get("start"):
                continue
            span = _span_days(e)
            if span is None or not (300 <= span <= 400):
                continue                    # not a fiscal-year duration
        filed = str(e.get("filed", ""))
        try:
            value = float(val)
        except (TypeError, ValueError):
            continue
        prev = by_end.get(end)
        if prev is None or filed >= prev[0]:
            by_end[end] = (filed, value)
    ends = sorted(by_end)[-max_years:]
    return [ChartPoint(fy=f"FY{end[:4]}", end=end, value=by_end[end][1])
            for end in ends]


def concept_history(client, cik: int, concept: Concept,
                    max_years: int = MAX_YEARS) -> ChartSeries | None:
    """The multi-year annual series for one concept, or None when
    fewer than 2 comparable points exist. Fetch failures for one
    alias simply move on to the next alias — a chart is an
    enhancement, never a reason to fail a report."""
    fetch = getattr(client, "company_concept", None)
    if fetch is None:
        return None
    preferred = _UNIT_PREFERENCE.get(concept.unit, [])
    if not preferred:
        return None
    taxonomies = [concept.taxonomy] + \
        [t for t in ("us-gaap", "dei") if t != concept.taxonomy]
    for taxonomy in taxonomies:
        best: ChartSeries | None = None
        for alias in concept.aliases:
            try:
                data = fetch(cik, taxonomy, alias)
            except Exception:
                continue                    # unreachable/404 alias: skip
            units = (data or {}).get("units", {})
            unit = next((u for u in preferred if u in units), None)
            if unit is None:
                continue                    # never a substitute unit
            points = _annual_points(units.get(unit, []), concept.kind,
                                    max_years)
            if len(points) >= 2 and \
                    (best is None or len(points) > len(best.points)):
                best = ChartSeries(
                    label=concept.label, alias=alias, taxonomy=taxonomy,
                    unit=unit, points=points,
                    source_url=concept_api_url(cik, taxonomy, alias))
                if len(points) >= max_years:
                    return best             # cannot do better
        if best is not None:
            return best
    return None


def build_charts(client, cik: int,
                 max_years: int = MAX_YEARS) -> list[ChartSeries]:
    """Charts for the headline concepts that have enough annual data.

    Never raises: any failure yields a shorter (possibly empty) list.
    """
    by_label = {c.label: c for c in CONCEPTS}
    out: list[ChartSeries] = []
    for label in CHART_LABELS:
        concept = by_label.get(label)
        if concept is None:
            continue
        try:
            series = concept_history(client, cik, concept, max_years)
        except Exception:
            series = None
        if series is not None:
            out.append(series)
    return out


# ---------------------------------------------------------------------------
# Server-side SVG rendering (stdlib only, no JavaScript)

def render_chart_svg(series: ChartSeries, width: int = 460,
                     height: int = 200) -> str:
    """A deterministic bar chart: same series in -> same SVG out.

    Every bar carries its formatted value and its fiscal-year label;
    negative values hang below the zero axis in red.
    """
    e = html.escape
    pts = series.points
    top_pad, bottom_pad, side_pad = 24, 34, 8
    plot_h = height - top_pad - bottom_pad
    vals = [p.value for p in pts]
    lo = min(0.0, min(vals))
    hi = max(0.0, max(vals))
    if hi == lo:
        hi = lo + 1.0

    def y(v: float) -> float:
        return top_pad + (hi - v) / (hi - lo) * plot_h

    axis_y = y(0.0)
    slot = (width - 2 * side_pad) / max(1, len(pts))
    bar_w = min(64.0, slot * 0.6)
    out = [f"<svg viewBox='0 0 {width} {height}' width='{width}' "
           f"height='{height}' role='img' "
           f"aria-label='{e(series.label)} annual trend' "
           f"xmlns='http://www.w3.org/2000/svg'>"]
    out.append(f"<title>{e(series.label)} — annual values "
               f"({e(series.unit)})</title>")
    for i, p in enumerate(pts):
        cx = side_pad + slot * i + slot / 2
        x = cx - bar_w / 2
        yv = y(p.value)
        y0 = min(yv, axis_y)
        h = max(1.5, abs(axis_y - yv))
        color = "#dc2626" if p.value < 0 else "#2563eb"
        out.append(f"<rect x='{x:.1f}' y='{y0:.1f}' width='{bar_w:.1f}' "
                   f"height='{h:.1f}' fill='{color}' rx='2'/>")
        label = fmt_money(p.value, series.unit)
        if p.value < 0:
            ly = yv + 13
        else:
            ly = yv - 5
        out.append(f"<text x='{cx:.1f}' y='{ly:.1f}' text-anchor='middle' "
                   f"font-size='11' fill='#1a1a2e'>{e(label)}</text>")
        out.append(f"<text x='{cx:.1f}' y='{height - 8}' "
                   f"text-anchor='middle' font-size='11' "
                   f"fill='#6b7280'>{e(p.fy)}</text>")
    out.append(f"<line x1='{side_pad}' y1='{axis_y:.1f}' "
               f"x2='{width - side_pad}' y2='{axis_y:.1f}' "
               f"stroke='#9ca3af' stroke-width='1'/>")
    out.append("</svg>")
    return "".join(out)
