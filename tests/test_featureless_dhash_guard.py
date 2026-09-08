"""A featureless image is a CONFIDENT FALSE MATCH under dhash.

An 8x8 dhash encodes 64 left-to-right gradient comparisons. A flat fill has no
gradients at all, so it hashes to all zeros -- and so does every OTHER flat
fill, at hamming 0 from each other and within the threshold of anything else
sparse. Found in the field: five distinct blank fixtures (4x4, 4x4, 40x40,
20x20, 200x140, five different md5s) ALL hashed to 0x0000000000000000, and
`figcite whereis` answered each of them with

    found in your corpus [dhash] hamming 6

naming the SAME Nature DOI every time. A confident, wrong answer about whose
figure this is -- which is the one thing this tool exists not to do.

`corpus.can_compare_dhash` already states the rule and already enforces it on
the duplicate-check path. The predicate was simply never consulted where a
distance turns into a VERDICT:

    corpus.duplicates_of_dhash   guarded
    match.by_dhash               UNGUARDED -- `figcite whereis`
    store.find_similar           UNGUARDED -- the deck audit's
                                 `manifest-dhash(d=N)` provenance links

Guarding only `by_dhash` would be a tripwire removal: the deck audit reaches
the identical degeneracy through its own call site. So the tests below cover
BOTH unguarded paths.

Each test carries its POSITIVE CONTROL in the same function: a featureless
query must be refused AND a real, feature-carrying query must still match. A
guard that refuses everything would satisfy the first assertion alone, and
would read as "fixed" while having broken the tool.
"""

import io

import pytest

from figcite import corpus, match, store
from figcite.provenance import Record, dhash_bytes

Image = pytest.importorskip("PIL.Image")


def _png(pixels, size):
    """PNG bytes for a WxH image built from a pixel-producing callable."""
    img = Image.new("L", size)
    img.putdata([pixels(x, y) for y in range(size[1]) for x in range(size[0])])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _flat(size=(40, 40), value=128):
    return _png(lambda x, y: value, size)


def _textured(size=(64, 64), seed=0):
    """Deterministic high-frequency content -- a dhash with real signal."""
    return _png(lambda x, y: (x * 37 + y * 101 + seed * 13) % 256, size)


def _row(dhash, doi):
    return corpus.FigureRow(
        pmcid="PMC1", doi=doi, label="Figure 1", caption="", licence="cc-by",
        source_url="", dhash=dhash, width=64, height=64, image_path="f.png",
    )


def test_the_premise_a_flat_fill_hashes_to_nothing():
    """The degeneracy this guard exists for is real, not hypothetical."""
    a = dhash_bytes(_flat((4, 4), 0))
    b = dhash_bytes(_flat((200, 140), 255))
    assert int(a, 16) == 0 and int(b, 16) == 0, (
        f"expected two unrelated flat fills to both hash to zero, got {a} {b}"
    )
    assert not corpus.can_compare_dhash(a), "can_compare_dhash should refuse a flat fill"
    # The positive half of the premise: real content is NOT refused.
    assert corpus.can_compare_dhash(dhash_bytes(_textured())), (
        "can_compare_dhash rejected an image with real gradient content"
    )


def test_by_dhash_refuses_a_featureless_query():
    """Reproduces the FIELD condition, which needs a blank row in the CORPUS.

    Written first with an all-textured corpus, where a blank query simply lands
    far from everything and the test passed against the unfixed code -- a test
    that could not fail. The real corpus contained featureless fixtures, and
    that is what the blank query was matching at hamming 0-6.
    """
    real = _textured(seed=1)
    rows = [
        _row(dhash_bytes(real), "10.1234/real"),
        _row(dhash_bytes(_flat((4, 4), 0)), "10.1038/s41586-blank"),
    ]

    # NEGATIVE: a blank query must not be handed a DOI.
    verdict = match.by_dhash(_flat(), rows)
    assert not isinstance(verdict, match.Match), (
        f"by_dhash matched a featureless image to {getattr(verdict, 'doi', None)} "
        f"({getattr(verdict, 'score_label', '')}) -- a confident false accusation"
    )

    # POSITIVE CONTROL, same test: a real query still resolves. Without this a
    # guard that refuses everything would look like a fix.
    hit = match.by_dhash(real, rows)
    assert isinstance(hit, match.Match) and hit.doi == "10.1234/real", (
        f"the guard broke a legitimate match: {hit!r}"
    )


def test_find_similar_refuses_a_featureless_query(monkeypatch):
    real = _textured(seed=3)
    recs = {
        "sha_real": Record(sha256="sha_real", dhash=dhash_bytes(real), doi="10.1234/real"),
        "sha_blank": Record(sha256="sha_blank", dhash=dhash_bytes(_flat((4, 4), 0)), doi="10.1234/blank"),
    }
    monkeypatch.setattr(store, "all_records", lambda: recs)

    # NEGATIVE: the deck audit must not link a blank picture to a record.
    assert store.find_similar(dhash_bytes(_flat((200, 140), 255))) is None, (
        "find_similar linked a featureless deck image to a manifest record"
    )

    # POSITIVE CONTROL: a real image still finds its record.
    hit = store.find_similar(dhash_bytes(real))
    assert hit is not None and hit[0].sha256 == "sha_real", (
        f"the guard broke a legitimate deck-audit link: {hit!r}"
    )


def test_a_blank_corpus_row_is_never_the_answer(monkeypatch):
    """The guard is symmetric, as `duplicates_of_dhash` already is.

    A degenerate CANDIDATE is as meaningless as a degenerate query, so a blank
    row must be excluded from scoring rather than merely out-distanced.
    """
    blank_row = _row(dhash_bytes(_flat()), "10.1234/blank-row")

    # Corpus of nothing but blank rows: there is no honest answer here.
    verdict = match.by_dhash(_flat((4, 4), 0), [blank_row])
    assert not isinstance(verdict, match.Match), (
        f"a featureless query was answered from a featureless corpus: {verdict!r}"
    )

    # POSITIVE CONTROL: the blank row does not suppress a legitimate match.
    real = _textured(seed=4)
    hit = match.by_dhash(real, [blank_row, _row(dhash_bytes(real), "10.1234/real")])
    assert isinstance(hit, match.Match) and hit.doi == "10.1234/real", (
        f"excluding blank rows broke a legitimate match: {hit!r}"
    )
