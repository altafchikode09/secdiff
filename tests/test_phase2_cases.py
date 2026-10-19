"""The owner's 100-case suite for Phase 2 (Change-Intensity Score,
Risk Words Highlighter, multi-year numbers charts). Same rule as
Phase 1: run 100 cases, fix the small bugs, run again, fix the big
bugs, run again — go live only at 100/100.

Every case is offline and deterministic: the company-concept API is
stubbed at the client boundary, chart series are crafted dicts, and
score/highlight cases run on hand-built diffs. Case numbers
(P001–P100) are stable so cycle reports can cite them.

Categories:
  A  Change-Intensity Score      P001–P030
  B  Risk Words Highlighter      P031–P065
  C  Chart data (concept API)    P066–P085
  D  Chart SVG rendering         P086–P095
  E  Integration                 P096–P100
"""
import io

import pytest

import secdiff.web as web
from secdiff import cli
from secdiff.charts import (ChartPoint, ChartSeries, build_charts,
                            concept_history, render_chart_svg)
from secdiff.concepts import CONCEPTS
from secdiff.diffing import Hunk, SectionDiff
from secdiff.highlight import RISK_TERMS, highlight_risk
from secdiff.report import FilingInfo, render_html, render_markdown
from secdiff.score import FORMULA_TEXT, compute_score
from secdiff.xbrl import XbrlResult, compute_delta

_BY_LABEL = {c.label: c for c in CONCEPTS}


def _sd(item, title, dels=0, mods=0, adds=0):
    hunks = [Hunk("deletion", old=f"deleted sentence {i}", size=10)
             for i in range(dels)]
    hunks += [Hunk("modification", old="old text", new="new text", size=5)
              for _ in range(mods)]
    hunks += [Hunk("addition", new=f"added sentence {i}", size=10)
              for i in range(adds)]
    return SectionDiff(item=item, title=title, hunks=hunks)


def _x(disappeared=0, appeared=0, rows=(), charts=()):
    return XbrlResult(rows=list(rows), unmapped=[], matched=len(rows),
                      considered=100, disappeared=disappeared,
                      appeared=appeared, charts=list(charts))


_RISK = ("1A", "Risk Factors")
_MDNA = ("7", "Management's Discussion and Analysis (MD&A)")


# ---------------------------------------------------------------------------
# A. Change-Intensity Score (P001–P030)

def test_p001_score_is_deterministic():
    diffs = [_sd(*_RISK, dels=3, mods=1), _sd(*_MDNA, dels=2)]
    a = compute_score(diffs, _x(disappeared=1))
    b = compute_score([_sd(*_RISK, dels=3, mods=1), _sd(*_MDNA, dels=2)],
                      _x(disappeared=1))
    assert a == b


def test_p002_zero_change_scores_zero():
    s = compute_score([], _x())
    assert s.total == 0 and s.band == "No change detected"


def test_p003_one_risk_deletion():
    s = compute_score([_sd(*_RISK, dels=1)], _x())
    assert (s.sensitive_deletions, s.total_deletions) == (1, 1)
    assert s.total == 5 + 1


def test_p004_one_plain_deletion():
    s = compute_score([_sd(*_MDNA, dels=1)], _x())
    assert s.sensitive_deletions == 0 and s.total == 1


def test_p005_legal_item3_deletion_is_sensitive():
    s = compute_score([_sd("3", "Legal Proceedings", dels=1)], _x())
    assert s.sensitive_deletions == 1


def test_p006_legal_title_is_sensitive():
    # 10-Q Part II style: Item 1 titled "Legal Proceedings".
    s = compute_score([_sd("1", "Legal Proceedings", dels=1)], _x())
    assert s.sensitive_deletions == 1


def test_p007_market_risk_title_is_sensitive():
    s = compute_score([_sd("2", "Market Risk Disclosures", dels=1)], _x())
    assert s.sensitive_deletions == 1


def test_p008_full_fallback_section_is_not_sensitive():
    s = compute_score([_sd("FULL", "Full document — no ITEM sections "
                            "detected in either filing", dels=2)], _x())
    assert s.sensitive_deletions == 0 and s.total == 2


def test_p009_sensitive_deletion_points_capped():
    s = compute_score([_sd(*_RISK, dels=20)], _x())
    assert s.points_sensitive_deletions == 40      # not 100


def test_p010_total_deletion_points_capped():
    s = compute_score([_sd(*_MDNA, dels=50)], _x())
    assert s.points_total_deletions == 30          # not 50


def test_p011_disappeared_points_capped():
    s = compute_score([], _x(disappeared=9))
    assert s.points_disappeared_xbrl == 20         # not 45


def test_p012_sensitive_modification_weight():
    s = compute_score([_sd(*_RISK, mods=1)], _x())
    assert s.sensitive_modifications == 1 and s.total == 2


def test_p013_sensitive_modification_points_capped():
    s = compute_score([_sd(*_RISK, mods=9)], _x())
    assert s.points_sensitive_modifications == 10  # not 18


def test_p014_plain_modifications_score_nothing():
    s = compute_score([_sd(*_MDNA, mods=5)], _x())
    assert s.total == 0


def test_p015_additions_score_nothing():
    s = compute_score([_sd(*_RISK, adds=7)], _x())
    assert s.total == 0


def test_p016_band_1_is_low():
    s = compute_score([_sd(*_MDNA, dels=1)], _x())
    assert s.total == 1 and s.band == "Low"


def test_p017_band_24_is_low():
    s = compute_score([_sd(*_RISK, dels=4)], _x())
    assert s.total == 24 and s.band == "Low"


def test_p018_band_25_is_moderate():
    s = compute_score([_sd(*_RISK, dels=4), _sd(*_MDNA, dels=1)], _x())
    assert s.total == 25 and s.band == "Moderate"


def test_p019_band_49_is_moderate():
    s = compute_score([_sd(*_RISK, dels=8), _sd(*_MDNA, dels=1)], _x())
    assert s.total == 49 and s.band == "Moderate"


def test_p020_band_50_is_high():
    s = compute_score([_sd(*_RISK, dels=8), _sd(*_MDNA, dels=2)], _x())
    assert s.total == 50 and s.band == "High"


def test_p021_band_74_is_high():
    s = compute_score([_sd(*_RISK, dels=8), _sd(*_MDNA, dels=21)],
                      _x(disappeared=1))
    assert s.total == 74 and s.band == "High"


def test_p022_band_75_is_very_high():
    s = compute_score([_sd(*_RISK, dels=8), _sd(*_MDNA, dels=22)],
                      _x(disappeared=1))
    assert s.total == 75 and s.band == "Very high"


def test_p023_maximum_score_is_100():
    diffs = [_sd(*_RISK, dels=20, mods=9), _sd(*_MDNA, dels=30)]
    s = compute_score(diffs, _x(disappeared=9))
    assert s.total == 100 and s.band == "Very high"


def test_p024_proxy_without_xbrl_scores_from_text_only():
    s = compute_score([_sd("1", "Election of Directors", dels=3)],
                      XbrlResult(rows=[], unmapped=[]))
    assert s.disappeared_xbrl == 0 and s.total == 3


def test_p025_hunk_sizes_do_not_change_score():
    big = [SectionDiff("1A", "Risk Factors",
                       [Hunk("deletion", old="x" * 5000, size=5000)])]
    small = [SectionDiff("1A", "Risk Factors",
                         [Hunk("deletion", old="x", size=1)])]
    assert compute_score(big, _x()).total == compute_score(small, _x()).total


def test_p026_section_order_does_not_change_score():
    a = [_sd(*_RISK, dels=2), _sd(*_MDNA, dels=1)]
    b = [_sd(*_MDNA, dels=1), _sd(*_RISK, dels=2)]
    assert compute_score(a, _x()).total == compute_score(b, _x()).total


def test_p027_breakdown_names_counts_and_points():
    s = compute_score([_sd(*_RISK, dels=2)], _x())
    assert "2 Risk/Legal deletions ×5 = 10" in s.breakdown()
    assert "2 total deletions ×1 = 2" in s.breakdown()


def test_p028_formula_text_is_honest_and_complete():
    t = FORMULA_TEXT.lower()
    assert "min(40, 5 ×" in t and "min(30, 1 ×" in t
    assert "not investment advice" in t
    assert "deterministically" in t
    assert "not a judgement" in t


def test_p029_score_shown_in_html_with_formula():
    diffs = [_sd(*_RISK, dels=1)]
    html_out = render_html(_info(), diffs, _x())
    assert "Change-intensity score" in html_out
    assert "<span class='big'>6/100</span>" in html_out
    assert "How is this score calculated?" in html_out
    assert "6 Risk/Legal" not in html_out  # breakdown shows raw counts
    assert "1 Risk/Legal deletions ×5 = 5" in html_out


def test_p030_score_shown_in_markdown():
    md = render_markdown(_info(), [_sd(*_RISK, dels=1)], _x())
    assert "Change-intensity score: 6/100 (Low)" in md
    assert "not investment advice" in md.lower()


def _info(form="10-K", ticker="EXMP"):
    return FilingInfo(
        ticker=ticker, company="Example Corp", form=form, cik=12345,
        new_accession="0000012345-25-000111", new_date="2025-10-31",
        new_url="https://www.sec.gov/new", new_index_url="https://www.sec.gov/new-idx",
        old_accession="0000012345-24-000099", old_date="2024-11-01",
        old_url="https://www.sec.gov/old", old_index_url="https://www.sec.gov/old-idx")


# ---------------------------------------------------------------------------
# B. Risk Words Highlighter (P031–P065)

def _hits(text):
    out, n = highlight_risk(text)
    return out, n


def test_p031_lawsuit_hit():
    out, n = _hits("The company faces a lawsuit over the plant.")
    assert n == 1 and "<mark class='risk'>lawsuit</mark>" in out


def test_p032_capitalised_hit():
    out, n = _hits("Lawsuit risk is disclosed.")
    assert n == 1 and "<mark class='risk'>Lawsuit</mark>" in out


def test_p033_uppercase_hit():
    out, n = _hits("A LAWSUIT was filed.")
    assert n == 1 and "<mark class='risk'>LAWSUIT</mark>" in out


def test_p034_litigation_hit():
    _o, n = _hits("Ongoing litigation may cost money.")
    assert n == 1


def test_p035_investigation_hit():
    _o, n = _hits("An investigation by the regulator began.")
    assert n == 1


def test_p036_going_concern_phrase_hit():
    out, n = _hits("There is doubt about the going concern assumption.")
    assert n == 1 and "<mark class='risk'>going concern</mark>" in out


def test_p037_going_alone_no_hit():
    _o, n = _hits("The going rate for loans rose.")
    assert n == 0


def test_p038_concern_alone_no_hit():
    _o, n = _hits("A concern was raised by staff.")
    assert n == 0


def test_p039_material_weakness_phrase_hit():
    out, n = _hits("A material weakness in controls was found.")
    assert n == 1 and "<mark class='risk'>material weakness</mark>" in out


def test_p040_default_hit():
    _o, n = _hits("The borrower fell into default on the loan.")
    assert n == 1


def test_p041_defaults_no_hit():
    _o, n = _hits("Two defaults were recorded last year.")
    assert n == 0


def test_p042_defaulted_no_hit():
    _o, n = _hits("The bond defaulted in March.")
    assert n == 0


def test_p043_fraud_hit():
    _o, n = _hits("The filing mentions fraud at the unit.")
    assert n == 1


def test_p044_fraudulent_no_hit():
    _o, n = _hits("No fraudulent activity was found.")
    assert n == 0


def test_p045_bankruptcy_hit():
    _o, n = _hits("The subsidiary filed for bankruptcy protection.")
    assert n == 1


def test_p046_restatement_hit():
    _o, n = _hits("A restatement of prior results was announced.")
    assert n == 1


def test_p047_covenant_hit():
    _o, n = _hits("Each covenant was tested quarterly.")
    assert n == 1


def test_p048_subpoena_hit():
    _o, n = _hits("The firm received a subpoena in June.")
    assert n == 1


def test_p049_indictment_hit():
    _o, n = _hits("The indictment was unsealed on Monday.")
    assert n == 1


def test_p050_penalty_hit():
    _o, n = _hits("A penalty of two million was imposed.")
    assert n == 1


def test_p051_url_never_highlighted():
    text = "See https://example.com/lawsuit for the docket."
    out, n = _hits(text)
    assert n == 0 and out == text


def test_p052_term_outside_url_still_highlighted():
    out, n = _hits("A lawsuit (https://example.com/lawsuit) was filed.")
    assert n == 1
    assert "https://example.com/lawsuit" in out
    assert "<mark class='risk'>lawsuit</mark> (https" in out


def test_p053_www_url_protected():
    text = "Filed at www.example.com/default today."
    out, n = _hits(text)
    assert n == 0 and out == text


def test_p054_tag_attributes_untouched():
    text = "<a href='https://x.com/fraud'>the docket</a>"
    out, n = _hits(text)
    assert n == 0 and out == text


def test_p055_link_text_is_highlighted():
    out, n = _hits("<a href='https://x.com/'>fraud</a>")
    assert n == 1 and "<mark class='risk'>fraud</mark>" in out


def test_p056_punctuation_adjacency():
    out, n = _hits("It ended (lawsuit), sadly.")
    assert n == 1 and "(<mark class='risk'>lawsuit</mark>)," in out


def test_p057_every_occurrence_highlighted():
    _o, n = _hits("fraud and fraud and fraud")
    assert n == 3


def test_p058_several_terms_one_sentence():
    _o, n = _hits("Fraud, litigation and bankruptcy followed.")
    assert n == 3


def test_p059_empty_string():
    assert highlight_risk("") == ("", 0)


def test_p060_no_terms_identity():
    text = "Revenue grew in every region this year."
    assert highlight_risk(text) == (text, 0)


def test_p061_escaped_entity_untouched():
    out, n = _hits("Fish &amp; fraud claims rose.")
    assert n == 1 and "&amp;" in out and "&amp;amp;" not in out


def test_p062_term_at_start_and_end():
    _o, n = _hits("Fraud opens and closes with fraud")
    assert n == 2


def test_p063_hyphen_is_a_boundary():
    _o, n = _hits("A fraud-related scheme was found.")
    assert n == 1


def test_p064_hyphenated_phrase_not_matched():
    _o, n = _hits("A going-concern warning was issued.")
    assert n == 0


def test_p065_published_list_covers_the_spec_terms():
    spec = {"lawsuit", "litigation", "investigation", "going concern",
            "default", "bankruptcy", "restatement", "material weakness",
            "covenant", "subpoena", "fraud"}
    assert spec <= set(RISK_TERMS)


# ---------------------------------------------------------------------------
# C. Chart data — company-concept API stubbed at the client (P066–P085)

class _ConceptClient:
    """company_concept stub: {(taxonomy, tag): payload-or-exception}."""

    def __init__(self, data):
        self.data = data

    def company_concept(self, cik, taxonomy, tag):
        v = self.data.get((taxonomy, tag))
        if v is None:
            raise Exception("404 Not Found")
        if isinstance(v, Exception):
            raise v
        return v


def _fact(end, val, form="10-K", filed="2025-11-01", start=None, fp="FY"):
    e = {"end": end, "val": val, "form": form, "filed": filed,
         "fp": fp, "accn": "0000000000-25-000001"}
    if start is not None:
        e["start"] = start
    return e


def _fy(end, val, **kw):
    """A fiscal-year flow fact ending on `end` (starts ~1 year prior)."""
    y = int(end[:4]) - 1
    return _fact(end, val, start=f"{y}-10-01", **kw)


def _payload(unit_entries):
    return {"units": unit_entries}


_REV = _BY_LABEL["Revenue"]
_REV_A1 = _REV.aliases[0]
_REV_A2 = _REV.aliases[1]


def _rev_client(entries_by_alias):
    return _ConceptClient({("us-gaap", alias): _payload({"USD": entries})
                           for alias, entries in entries_by_alias.items()})


def test_p066_five_annual_points():
    entries = [_fy("2021-09-25", 1.0), _fy("2022-09-24", 2.0),
               _fy("2023-09-30", 3.0), _fy("2024-09-28", 4.0),
               _fy("2025-09-27", 5.0)]
    s = concept_history(_rev_client({_REV_A1: entries}), 1, _REV)
    assert s is not None and len(s.points) == 5
    assert [p.value for p in s.points] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert s.alias == _REV_A1 and s.unit == "USD"


def test_p067_three_points():
    entries = [_fy("2023-09-30", 3.0), _fy("2024-09-28", 4.0),
               _fy("2025-09-27", 5.0)]
    s = concept_history(_rev_client({_REV_A1: entries}), 1, _REV)
    assert s is not None and len(s.points) == 3


def test_p068_two_points_is_enough():
    entries = [_fy("2024-09-28", 4.0), _fy("2025-09-27", 5.0)]
    s = concept_history(_rev_client({_REV_A1: entries}), 1, _REV)
    assert s is not None and len(s.points) == 2


def test_p069_one_point_no_chart():
    s = concept_history(_rev_client({_REV_A1: [_fy("2025-09-27", 5.0)]}),
                        1, _REV)
    assert s is None


def test_p070_zero_points_no_chart():
    s = concept_history(_rev_client({_REV_A1: []}), 1, _REV)
    assert s is None


def test_p071_missing_concept_no_chart():
    s = concept_history(_ConceptClient({}), 1, _REV)
    assert s is None


def test_p072_alias_fallback():
    entries = [_fy("2023-09-30", 3.0), _fy("2024-09-28", 4.0),
               _fy("2025-09-27", 5.0)]
    s = concept_history(_rev_client({_REV_A2: entries}), 1, _REV)
    assert s is not None and s.alias == _REV_A2 and len(s.points) == 3


def test_p073_alias_with_most_points_wins():
    a1 = [_fy("2024-09-28", 4.0), _fy("2025-09-27", 5.0)]
    a2 = [_fy("2022-09-24", 2.0), _fy("2023-09-30", 3.0),
          _fy("2024-09-28", 4.0), _fy("2025-09-27", 5.0)]
    s = concept_history(_rev_client({_REV_A1: a1, _REV_A2: a2}), 1, _REV)
    assert s is not None and s.alias == _REV_A2 and len(s.points) == 4


def test_p074_alias_tie_keeps_priority():
    pts = [_fy("2023-09-30", 3.0), _fy("2024-09-28", 4.0),
           _fy("2025-09-27", 5.0)]
    s = concept_history(_rev_client({_REV_A1: pts, _REV_A2: pts}), 1, _REV)
    assert s is not None and s.alias == _REV_A1


def test_p075_quarterly_facts_are_not_annual():
    q = [_fact("2025-03-29", 1.0, start="2024-12-29", form="10-Q"),
         _fact("2025-06-28", 2.0, start="2025-03-30", form="10-Q"),
         _fact("2025-09-27", 3.0, start="2025-06-29", form="10-Q")]
    s = concept_history(_rev_client({_REV_A1: q}), 1, _REV)
    assert s is None


def test_p076_10q_facts_excluded_even_with_year_span():
    entries = [_fact("2024-09-28", 4.0, start="2023-10-01", form="10-Q"),
               _fact("2025-09-27", 5.0, start="2024-09-29", form="10-Q")]
    s = concept_history(_rev_client({_REV_A1: entries}), 1, _REV)
    assert s is None


def test_p077_restated_value_latest_filed_wins():
    entries = [_fy("2024-09-28", 100.0, filed="2024-11-01"),
               _fy("2024-09-28", 110.0, filed="2025-11-01"),
               _fy("2025-09-27", 120.0, filed="2025-11-01")]
    s = concept_history(_rev_client({_REV_A1: entries}), 1, _REV)
    assert s is not None
    by_fy = {p.fy: p.value for p in s.points}
    assert by_fy == {"FY2024": 110.0, "FY2025": 120.0}


def test_p078_duplicate_same_value_dedupes():
    entries = [_fy("2024-09-28", 100.0, filed="2024-11-01"),
               _fy("2024-09-28", 100.0, filed="2025-11-01"),
               _fy("2025-09-27", 120.0, filed="2025-11-01")]
    s = concept_history(_rev_client({_REV_A1: entries}), 1, _REV)
    assert s is not None and len(s.points) == 2
    assert {p.fy: p.value for p in s.points}["FY2024"] == 100.0


def test_p079_negative_values_series():
    ni = _BY_LABEL["Net income"]
    entries = [_fy("2024-09-28", -5.0), _fy("2025-09-27", -2.0),
               _fy("2023-09-30", 3.0)]
    client = _ConceptClient({("us-gaap", ni.aliases[0]):
                              _payload({"USD": entries})})
    s = concept_history(client, 1, ni)
    assert s is not None and [p.value for p in s.points] == [3.0, -5.0, -2.0]


def test_p080_stock_concept_instant_facts():
    ta = _BY_LABEL["Total assets"]
    entries = [_fact("2023-09-30", 300.0), _fact("2024-09-28", 350.0),
               _fact("2025-09-27", 400.0)]
    client = _ConceptClient({("us-gaap", ta.aliases[0]):
                              _payload({"USD": entries})})
    s = concept_history(client, 1, ta)
    assert s is not None and len(s.points) == 3


def test_p081_capped_at_five_most_recent_years():
    entries = [_fy(f"{y}-09-30", float(y)) for y in range(2019, 2026)]
    s = concept_history(_rev_client({_REV_A1: entries}), 1, _REV)
    assert s is not None and len(s.points) == 5
    assert s.points[0].fy == "FY2021" and s.points[-1].fy == "FY2025"


def test_p082_fy_label_from_period_end():
    entries = [_fy("2024-09-28", 4.0), _fy("2025-09-27", 5.0)]
    s = concept_history(_rev_client({_REV_A1: entries}), 1, _REV)
    assert [p.fy for p in s.points] == ["FY2024", "FY2025"]


def test_p083_substitute_unit_never_used():
    entries = [_fy("2024-09-28", 4.0), _fy("2025-09-27", 5.0)]
    client = _ConceptClient({("us-gaap", _REV_A1):
                               _payload({"EUR": entries})})
    assert concept_history(client, 1, _REV) is None


def test_p084_preferred_unit_chosen_among_several():
    usd = [_fy("2024-09-28", 4.0), _fy("2025-09-27", 5.0)]
    eur = [_fy("2023-09-30", 30.0), _fy("2024-09-28", 40.0),
           _fy("2025-09-27", 50.0)]
    client = _ConceptClient({("us-gaap", _REV_A1):
                               _payload({"USD": usd, "EUR": eur})})
    s = concept_history(client, 1, _REV)
    assert s is not None and s.unit == "USD"
    assert [p.value for p in s.points] == [4.0, 5.0]


def test_p085_client_without_concept_api():
    assert concept_history(object(), 1, _REV) is None
    assert build_charts(object(), 1) == []


# ---------------------------------------------------------------------------
# D. Chart SVG rendering (P086–P095)

def _series(values, label="Revenue", unit="USD"):
    pts = [ChartPoint(fy=f"FY{2023 + i}", end=f"{2023 + i}-09-30",
                      value=float(v)) for i, v in enumerate(values)]
    return ChartSeries(label=label, alias="Revenues", taxonomy="us-gaap",
                       unit=unit, points=pts,
                       source_url="https://data.sec.gov/x")


def test_p086_svg_labels_years_and_values():
    svg = render_chart_svg(_series([3.83e11, 3.94e11]))
    assert "FY2023" in svg and "FY2024" in svg
    assert "$383.00B" in svg and "$394.00B" in svg


def test_p087_negative_bar_in_red_with_value():
    svg = render_chart_svg(_series([3.0, -5.0e9]))
    assert "#dc2626" in svg and "-$5.00B" in svg


def test_p088_equal_values_render():
    svg = render_chart_svg(_series([100.0, 100.0]))
    assert svg.count("<rect") == 2


def test_p089_all_zero_values_render():
    svg = render_chart_svg(_series([0.0, 0.0]))
    assert svg.startswith("<svg") and svg.count("<rect") == 2


def test_p090_trillions_formatted_like_the_table():
    svg = render_chart_svg(_series([3.91e12, 4.0e12]))
    assert "$3,910.00B" in svg


def test_p091_label_is_escaped():
    svg = render_chart_svg(_series([1.0, 2.0], label="A<B & C>"))
    assert "&lt;" in svg and "<B" not in svg


def test_p092_svg_is_deterministic():
    s = _series([1.5, -2.5, 3.5])
    assert render_chart_svg(s) == render_chart_svg(s)


def test_p093_two_points_two_bars():
    assert render_chart_svg(_series([1.0, 2.0])).count("<rect") == 2


def test_p094_five_points_five_bars():
    svg = render_chart_svg(_series([1.0, 2.0, 3.0, 4.0, 5.0]))
    assert svg.count("<rect") == 5


def test_p095_viewbox_uses_requested_size():
    svg = render_chart_svg(_series([1.0, 2.0]), width=300, height=150)
    assert "viewBox='0 0 300 150'" in svg


# ---------------------------------------------------------------------------
# E. Integration (P096–P100)

def _call(method, path, query="", body=b"", cookie=""):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path,
               "QUERY_STRING": query, "CONTENT_LENGTH": str(len(body)),
               "HTTP_COOKIE": cookie, "wsgi.input": io.BytesIO(body)}
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = headers

    chunks = web.application(environ, start_response)
    return captured["status"], captured["headers"], \
        b"".join(chunks).decode("utf-8")


def _web_result(charts=()):
    info = _info()
    diffs = [SectionDiff(item="1A", title="Risk Factors", hunks=[
        Hunk(kind="deletion",
             old="The company faces a lawsuit over the plant closure.",
             size=50)])]
    x = _x(charts=charts)
    return info, diffs, x


def _stub_web(monkeypatch, tmp_path, result):
    monkeypatch.setattr(web, "_STORE", web.UsageStore(tmp_path / "u.json"))
    monkeypatch.setattr(web, "_resolve_company",
                        lambda params: (12345, "Example Corp"))
    monkeypatch.setattr(web, "live_pipeline", lambda args: result)


def test_p096_web_report_has_score_highlight_and_chart(
        monkeypatch, tmp_path):
    series = _series([3.83e11, 3.94e11])
    _stub_web(monkeypatch, tmp_path, _web_result(charts=[series]))
    status, _h, text = _call("POST", "/report", body=b"ticker=EXMP")
    assert status == "200 OK"
    assert "Change-intensity score" in text
    assert "<mark class='risk'>lawsuit</mark>" in text
    assert "<svg" in text and "FY2023" in text
    assert "going concern" in text and "subpoena" in text   # the legend
    assert "reading aid only" in text


def test_p097_markdown_report_has_trend_table():
    series = ChartSeries(
        label="Revenue", alias="Revenues", taxonomy="us-gaap", unit="USD",
        points=[ChartPoint("FY2024", "2024-09-28", 3.94e11)],
        source_url="https://data.sec.gov/api/xbrl/companyconcept/x")
    md = render_markdown(_info(), [_sd(*_RISK, dels=1)],
                         _x(charts=[series]))
    assert "## Multi-year trends" in md
    assert "| FY2024 | $394.00B |" in md
    assert "companyconcept" in md


def test_p098_html_report_has_chart_section_and_source():
    series = _series([3.83e11, 3.94e11])
    series.source_url = ("https://data.sec.gov/api/xbrl/companyconcept/"
                         "CIK0000012345/us-gaap/Revenues.json")
    html_out = render_html(_info(), [_sd(*_RISK, dels=1)],
                           _x(charts=[series]))
    assert "Multi-year trends" in html_out and "<svg" in html_out
    assert "companyconcept" in html_out
    assert "as most recently reported" in html_out


class _PipelineClient(_ConceptClient):
    name_matched_ticker = None

    def __init__(self):
        entries = [_fy("2023-09-30", 3.0), _fy("2024-09-28", 4.0),
                   _fy("2025-09-27", 5.0)]
        super().__init__({("us-gaap", _REV_A1): _payload({"USD": entries})})

    def resolve_ticker(self, ticker, form="10-K", min_count=2):
        return 999, "Fake Co"

    def all_filings(self, cik, form):
        from secdiff.edgar import Filing
        rows = {"10-K": [("K25", "2025-10-31"), ("K24", "2024-11-01")],
                "DEF 14A": [("P25", "2025-04-10"), ("P24", "2024-04-11")]}
        return [Filing(form=form, accession=a, filing_date=d,
                       report_date=d, primary_document="f.htm")
                for a, d in rows[form]]

    def recent_filings(self, cik, form, count=2):
        got = self.all_filings(cik, form)
        if len(got) < count:
            raise SystemExit("too few")
        return got[:count]

    def filing_text(self, cik, filing):
        return (f"The company reports its results for the period in "
                f"filing {filing.accession} with full detail included "
                f"here for readers.")

    def company_facts(self, cik):
        return {"facts": {}}


def test_p099_charts_built_for_10k_but_not_for_proxy(monkeypatch, tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(cli, "EdgarClient",
                        lambda email=None, cache_dir=None: _PipelineClient())

    def run(form):
        args = SimpleNamespace(
            ticker="FAKE", form=form, years=1, vs_year=None,
            base_year=None, email="t@e.com", cache_dir=str(tmp_path))
        return cli.live_pipeline(args)

    _info10k, _d, x10k = run("10-K")
    assert len(x10k.charts) == 1 and x10k.charts[0].label == "Revenue"
    assert len(x10k.charts[0].points) == 3
    _infop, _dp, xp = run("DEF 14A")
    assert xp.charts == []


def test_p100_disappeared_and_appeared_counts_are_structured():
    def e(val, accn):
        return {"start": "2023-10-01", "end": "2024-09-30", "val": val,
                "accn": accn, "fp": "FY"}

    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [e(100, "OLD"), e(120, "NEW")]}},
        "GrossProfit": {"units": {"USD": [e(40, "OLD")]}},
        "CostOfRevenue": {"units": {"USD": [e(60, "NEW")]}},
    }}}
    x = compute_delta(facts, "OLD", "NEW")
    assert len(x.rows) == 1 and x.rows[0].label == "Revenue"
    assert x.disappeared == 1          # GrossProfit: older filing only
    assert x.appeared == 1             # Cost of revenue: newer filing only
    assert len(x.unmapped) == 2        # both still listed, never merged
