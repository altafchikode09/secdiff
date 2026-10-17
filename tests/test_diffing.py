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
