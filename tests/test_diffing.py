from secdiff.diffing import diff_section, split_sentences

OLD = (
    "The company faces intense competition in all of its markets today. "
    "Supply shortages could materially harm the business and its results. "
    "Revenue grew during the year because of strong product demand overall."
)
NEW = (
    "The company faces intense competition in all of its markets today. "
    "Revenue grew during the year because of weak product demand overall. "
    "A brand new risk about quantum computing appeared in this filing period."
)


def test_sentence_split_min_length():
    sents = split_sentences("Short. This is a much longer sentence that stays here.")
    assert sents == ["This is a much longer sentence that stays here."]


def test_deletion_detected_and_ranked_first():
    d = diff_section("1A", "Risk Factors", OLD, NEW)
    kinds = [h.kind for h in d.hunks]
    assert "deletion" in kinds
    # deletions sort before modifications and additions
    order = {"deletion": 0, "modification": 1, "addition": 2}
    assert kinds == sorted(kinds, key=lambda k: order[k])
    deleted = [h for h in d.hunks if h.kind == "deletion"]
    assert any("Supply shortages" in h.old for h in deleted)


def test_addition_detected():
    d = diff_section("1A", "Risk Factors", OLD, NEW)
    added = [h for h in d.hunks if h.kind == "addition"]
    assert any("quantum computing" in h.new for h in added)


def test_identical_text_no_hunks():
    d = diff_section("7", "MD&A", OLD, OLD)
    assert d.hunks == []


# -- whole-document fallback (build_diffs) ---------------------------------
# When section splitting finds no ITEM/Proposal headings in EITHER
# document (common for real proxy statements), build_diffs must diff
# the whole documents as one labelled FULL section — an empty report
# would silently claim nothing changed.

from secdiff.cli import build_diffs

_NO_HEADINGS_OLD = (
    "The board recommends every proposal in this statement to all "
    "shareholders of record at the close of business this year. "
    "Executive pay rose for the chief executive during the past year.")
_NO_HEADINGS_NEW = (
    "The board recommends every proposal in this statement to all "
    "shareholders of record at the close of business this year. "
    "A brand new shareholder proposal appears in this statement now.")


def test_full_document_fallback_when_no_sections():
    diffs, conf = build_diffs(_NO_HEADINGS_OLD, _NO_HEADINGS_NEW,
                              "DEF 14A")
    assert conf == "low"
    assert len(diffs) == 1 and diffs[0].item == "FULL"
    kinds = {h.kind for h in diffs[0].hunks}
    assert "deletion" in kinds and "addition" in kinds


def test_no_fallback_when_sections_exist():
    old = ("ITEM 1A. Risk Factors\n"
           "The company faces many serious risks in its markets today "
           "and those risks could materially harm results of operations.")
    new = ("ITEM 1A. Risk Factors\n"
           "The company faces many serious risks in its markets today "
           "and those risks could materially harm results of operations "
           "plus a newly added supply chain risk sentence appears here.")
    diffs, _conf = build_diffs(old, new, "10-K")
    assert diffs and all(d.item != "FULL" for d in diffs)
