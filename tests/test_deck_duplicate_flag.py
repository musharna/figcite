"""A figure you credited to one paper that also appears under another DOI.

Reviewing a talk is exactly when you would want to know this, so it belongs on
the audit row. It REPORTS and never rewrites: a hit is a question for the user
("you credited this to A, it also appears in B"), not a correction to apply.
Republication, a reused panel, and a genuine mistake all look identical from
here, and only the user knows which.

Deviation from the plan: the plan gave `_duplicate_dois(image_bytes, ...)`, but
audit rows carry no image bytes -- `deck.audit` discards the blob after
matching. They do carry `record.dhash`, computed at capture time, which is the
only thing the dhash comparison ever needed. Taking the hash avoids re-reading
and re-hashing every picture in the deck to recompute a value already stored.
"""

import pytest

from figcite import corpus, service
from figcite.provenance import Record

FLAT = "0" * 16  # a dhash with no set bits -- what a solid fill produces


class _Row:
    def __init__(self, doi):
        self.doi, self.pmcid, self.label, self.licence = doi, "PMC2", "Figure 1", "CC BY"


def test_a_row_carries_the_other_dois_that_hold_this_figure(monkeypatch):
    monkeypatch.setattr(
        corpus, "duplicates_of_dhash",
        lambda dh, credited_doi: [_Row("10.1/elsewhere")],
    )
    assert service._duplicate_dois("a" * 16, "10.1/credited") == ["10.1/elsewhere"]


def test_no_duplicate_yields_an_empty_list_not_none(monkeypatch):
    """Positive control: the empty case must be a list the UI can iterate."""
    monkeypatch.setattr(corpus, "duplicates_of_dhash", lambda dh, credited_doi: [])
    assert service._duplicate_dois("a" * 16, "10.1/credited") == []


def test_a_corpus_failure_does_not_break_the_audit(monkeypatch):
    """An unbuilt or unreadable corpus must not stop you reviewing a talk.

    This is the one place a swallowed exception is right: the duplicate flag is
    an extra, and losing it costs a hint, whereas raising costs the report.
    """
    def boom(dh, credited_doi):
        raise RuntimeError("no corpus")

    monkeypatch.setattr(corpus, "duplicates_of_dhash", boom)
    assert service._duplicate_dois("a" * 16, "10.1/credited") == []


def test_a_row_with_no_dhash_is_not_looked_up_at_all(monkeypatch):
    """An untagged picture has no hash, and hashing "" would match everything."""
    called = []
    monkeypatch.setattr(
        corpus, "duplicates_of_dhash",
        lambda dh, credited_doi: called.append(dh) or [_Row("10.1/wrong")],
    )
    assert service._duplicate_dois("", "10.1/credited") == []
    assert called == [], "an empty hash was sent to the corpus anyway"


# ------------------------------------------------- the comparison itself


def test_a_featureless_figure_reports_nothing_rather_than_everything(wired_corpus):
    """A flat fill hashes to all zeros, so it collides with every other flat
    fill. Reporting those as duplicates would be a confident, wrong accusation.
    """
    assert corpus.duplicates_of_dhash(FLAT, "10.1/x") == []


def test_a_nearly_blank_query_is_not_matched_to_a_blank_figure(wired_corpus):
    """The case the two featureless guards do NOT cover redundantly.

    A query with only three set bits passes `can_compare_dhash` -- it is not
    flat -- yet it sits hamming 3 from an all-zero hash, inside the threshold
    of 6. Without the per-ROW guard it would be reported as a duplicate of
    every nearly-blank figure in the corpus, which is a confident and wrong
    accusation about someone's citation.

    Found by mutation: removing either guard alone left every test passing,
    because each was masking the other on the flat-vs-flat case both handle.
    This test and its mirror below are what actually pin them.
    """
    near_blank = "0000000000000007"  # 3 set bits, hamming 3 from FLAT
    assert corpus.can_compare_dhash(near_blank), "premise: the query is comparable"
    assert corpus.duplicates_of_dhash(near_blank, "10.1/x") == []


def test_a_blank_query_is_not_matched_to_a_nearly_blank_figure(wired_corpus):
    """The mirror image, which pins the guard on the QUERY side.

    Here the corpus row is the comparable one and the query is the degenerate
    one, so only the entry guard can refuse it.
    """
    conn = corpus.connect()
    corpus.upsert(conn, corpus.FigureRow(
        pmcid="PMC4", doi="10.1/nearly-blank", label="Figure 1", caption="",
        licence="", source_url="", dhash="0000000000000007", width=96, height=96,
        image_path="PMC4/f1.png"))
    conn.close()
    assert corpus.duplicates_of_dhash(FLAT, "10.1/x") == []


def test_a_real_hash_still_finds_its_match(wired_corpus):
    """Positive control: the guard above must not simply refuse everything."""
    found = corpus.duplicates_of_dhash(wired_corpus, "10.1/credited")
    assert [r.doi for r in found] == ["10.1/elsewhere"]


def test_the_credited_paper_is_never_reported_against_itself(wired_corpus):
    found = corpus.duplicates_of_dhash(wired_corpus, "10.1/elsewhere")
    assert [r.doi for r in found] == ["10.1/credited"]


# ------------------------------------------------------ the audit report


def test_the_audit_row_gains_the_duplicate_dois(monkeypatch):
    rec = Record(sha256="b" * 64, dhash="a" * 16, doi="10.1/credited", confirmed=True)
    _stub_deck(monkeypatch, rec)
    monkeypatch.setattr(
        corpus, "duplicates_of_dhash",
        lambda dh, credited_doi: [_Row("10.1/elsewhere")],
    )
    row = service.audit("/x/deck.pptx")["rows"][0]
    assert row["duplicate_of"] == ["10.1/elsewhere"]


def test_an_unremarkable_row_reports_no_duplicates(monkeypatch):
    """Positive control on the report: the field must be able to come back empty."""
    rec = Record(sha256="b" * 64, dhash="a" * 16, doi="10.1/credited", confirmed=True)
    _stub_deck(monkeypatch, rec)
    monkeypatch.setattr(corpus, "duplicates_of_dhash", lambda dh, credited_doi: [])
    assert service.audit("/x/deck.pptx")["rows"][0]["duplicate_of"] == []


def test_an_untagged_row_still_has_the_field(monkeypatch):
    """The UI iterates every row; a missing key would be a template crash."""
    _stub_deck(monkeypatch, None)
    assert service.audit("/x/deck.pptx")["rows"][0]["duplicate_of"] == []


def _stub_deck(monkeypatch, rec):
    monkeypatch.setattr(
        service.deck, "audit",
        lambda p, min_inches=1.0: {
            "pptx": "/x/deck.pptx", "pictures": 1, "tagged": 1,
            "unconfirmed": 0, "untagged_substantive": 0,
            "rows": [{"slide": 1, "shape": "Picture 1", "size_in": [3.0, 3.0],
                      "decorative": False, "matched_by": "manifest-sha256",
                      "record": rec, "alt_text": ""}],
        },
    )


@pytest.fixture()
def wired_corpus(tmp_path, monkeypatch):
    """Two papers publishing the same figure, plus a flat one that collides."""
    monkeypatch.setattr(corpus, "CORPUS_DIR", tmp_path / "c")
    monkeypatch.setattr(corpus, "DB_PATH", tmp_path / "c" / "figures.sqlite")
    monkeypatch.setattr(corpus, "IMAGE_DIR", tmp_path / "c" / "images")
    monkeypatch.setattr(corpus, "DESCRIPTOR_DIR", tmp_path / "c" / "descriptors")

    dh = "3f5a91c02de4b678"
    conn = corpus.connect()
    for pmcid, doi, h in (
        ("PMC1", "10.1/credited", dh),
        ("PMC2", "10.1/elsewhere", dh),
        ("PMC3", "10.1/flat", FLAT),
    ):
        corpus.upsert(conn, corpus.FigureRow(
            pmcid=pmcid, doi=doi, label="Figure 1", caption="", licence="CC BY",
            source_url="", dhash=h, width=96, height=96,
            image_path=f"{pmcid}/f1.png"))
    conn.close()
    return dh


# ------------------------------------------------------------- the template

def _deck_row_source():
    import re

    from figcite.webui import PAGE

    m = re.search(r"function deckRow\(r\) \{(.*?)\n\}", PAGE, re.S)
    assert m, "deckRow is no longer a plain function declaration"
    return m.group(1)


def test_the_deck_row_renders_the_duplicate_flag():
    """Scoped to deckRow: `"duplicate_of" in PAGE` would pass if the flag were
    computed and then never placed in the row."""
    src = _deck_row_source()
    assert "duplicate_of" in src, "the deck row stopped reading the duplicate list"
    assert "${dup}" in src, "the flag is computed but never rendered into the row"


def test_the_flag_is_escaped_and_defaults_to_empty():
    """A DOI is server-supplied text, and a missing key must not throw.

    Verified for real under node during development: an absent `duplicate_of`,
    an empty list, one DOI, two DOIs, and a DOI containing markup all render
    correctly, the last one escaped.
    """
    src = _deck_row_source()
    assert "r.duplicate_of || []" in src, "a row without the key would throw"
    assert ".map(esc)" in src, "duplicate DOIs are interpolated unescaped"
