"""The three primitives everything else rests on: dhash, hamming, and the
PNG metadata keys.

`dhash_bytes` and `hamming` are the whole of the perceptual-match layer, and
`match.by_dhash` treats their output as fact. A dhash whose comparison is
inverted still returns a plausible hex string, and a `hamming` that returns a
distance instead of its 999 sentinel still returns an int -- so both fail
quietly, in the direction of confidently matching the wrong figure.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from figcite.provenance import (
    Record,
    dhash_bytes,
    embed,
    hamming,
    read_embedded,
)


def _png(img) -> bytes:
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


# ------------------------------------------------------------------- dhash


def test_a_flat_image_hashes_to_all_zero_bits():
    """`"1" if left > right else "0"` survived `Gt -> GtE`.

    dhash records, for each adjacent pair of pixels, whether the left is
    BRIGHTER than the right. On a flat image every pair is equal, so every
    bit is 0. Under `>=` every bit becomes 1 and the hash of a blank image is
    ffffffffffffffff.

    That is not cosmetic: equality is the commonest relationship between
    adjacent pixels in a figure with flat backgrounds, panel gutters or axis
    whitespace, so flipping this bit changes the hash of most real figures --
    and every stored hash in an existing manifest was computed the other way.
    A corpus built before the change silently stops matching anything.
    """
    flat = Image.new("RGB", (64, 64), (128, 128, 128))

    assert dhash_bytes(_png(flat)) == "0" * 16, dhash_bytes(_png(flat))


def test_a_left_to_right_gradient_sets_no_bits_and_its_mirror_sets_all():
    """The positive control, and the polarity. A ramp that gets brighter to
    the right has every left < right (bit 0); its mirror has every left >
    right (bit 1). If the comparison were inverted rather than loosened,
    the flat-image test above would still pass."""
    w = h = 64
    ramp = Image.new("L", (w, h))
    ramp.putdata([min(255, x * 4) for _y in range(h) for x in range(w)])

    assert dhash_bytes(_png(ramp.convert("RGB"))) == "0" * 16
    mirrored = ramp.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    assert dhash_bytes(_png(mirrored.convert("RGB"))) == "f" * 16


# ----------------------------------------------------------------- hamming


@pytest.mark.parametrize(
    "a,b,why",
    [
        ("", "0" * 16, "an empty first hash"),
        ("0" * 16, "", "an empty second hash"),
        ("00", "0" * 16, "a short first hash against a full one"),
        # The same mismatch the other way round. `len(a) != len(b)` compares
        # for INEQUALITY and the case above only makes `a` SHORTER, so a guard
        # narrowed to `len(a) < len(b)` still fires for it and the reduced-ROR
        # mutant survived. Reversed, the narrowed guard falls through and
        # `int(a, 16) ^ int(b, 16)` is computed on incomparable widths.
        ("0" * 16, "00", "a full first hash against a short one"),
    ],
    ids=["empty-a", "empty-b", "length-mismatch", "length-mismatch-reversed"],
)
def test_an_uncomparable_pair_returns_the_sentinel_not_a_distance(a, b, why):
    """`if not a or not b or len(a) != len(b)` survived `Or -> And`.

    Three operands, and under `and` all three must hold at once -- which they
    never do, because an empty hash and a length mismatch are alternatives,
    not companions. Each case here makes exactly ONE operand true, so a
    fixture satisfying every operand could not tell which one carries the
    guard.

    The consequence differs per case and all are bad: an empty hash reaches
    `int("", 16)` and raises out of a matcher that is documented never to
    raise, and two hashes of different widths XOR into a NUMBER -- a small
    distance, reported as a close match between figures that were never
    comparable.

    999 is the sentinel meaning "not comparable", chosen to sit far above any
    real 64-bit distance so it can never read as a hit.
    """
    assert hamming(a, b) == 999, why


def test_a_truncated_hash_does_not_report_a_perfect_match():
    """What the reversed length mismatch actually produces, spelled out.

    `hamming("0" * 16, "00")` with the guard narrowed to `<` skips the
    sentinel and evaluates `int("0000000000000000", 16) ^ int("00", 16)`,
    which is 0 -- a distance of ZERO, the strongest possible match, between
    two hashes that were never comparable.

    `by_dhash` sorts candidates by this number and takes the smallest, so a
    truncated or malformed hash would not merely slip through: it would beat
    every genuine figure in the corpus. The sentinel is 999 precisely so that
    "not comparable" can never be mistaken for "identical", and this is the
    direction that was never asked.
    """
    assert hamming("0" * 16, "00") == 999
    assert hamming("00", "0" * 16) == 999
    # And the reason the sentinel is that large: it must lose to every real
    # distance, including the worst possible one.
    assert 999 > hamming("0" * 16, "f" * 16) == 64


def test_two_comparable_hashes_return_a_real_distance():
    """Positive control: "always return 999" passes every case above and
    makes the whole dhash stage report nothing matches, ever."""
    assert hamming("0" * 16, "0" * 16) == 0
    assert hamming("0" * 16, "0" * 15 + "1") == 1
    assert hamming("0" * 16, "f" * 16) == 64


# ------------------------------------------------------- PNG metadata keys


def _rec(**over):
    base = dict(
        doi="10.3390/horticulturae6040087",
        citation="Shiragaki et al. (2020). Horticulturae 6: 87.",
        short_cite="Shiragaki et al. 2020",
        title="Phylogenetic analysis of Capsicum",
        confirmed=True,
        source_kind="pdf-crop",
    )
    base.update(over)
    return Record(**base)


def _text_chunks(path):
    with Image.open(path) as im:
        return dict(im.text or {})


def test_a_record_with_only_a_page_url_still_writes_a_Source_chunk(tmp_path):
    """`if rec.doi_url or rec.url` survived `Or -> And`.

    A record has a DOI-derived URL, a page URL, or both -- rarely both, since
    a figure grabbed from a PDF has the first and one snipped from a web page
    has the second. Under `and` a record carrying only one of them writes no
    Source chunk at all, so the PNG's own embedded pointer back to where it
    came from silently disappears for most real captures.

    Driven from each side alone, because a fixture with both set satisfies
    `and` too.
    """
    src = tmp_path / "in.png"
    Image.new("RGB", (16, 16), "white").save(src)

    only_url = tmp_path / "url.png"
    embed(src, only_url, _rec(doi=None, url="https://example.org/article"))
    assert _text_chunks(only_url).get("Source") == "https://example.org/article", _text_chunks(
        only_url
    )

    only_doi = tmp_path / "doi.png"
    embed(src, only_doi, _rec(url=None))
    assert "doi.org" in _text_chunks(only_doi).get("Source", ""), _text_chunks(only_doi)


def test_a_record_with_neither_url_writes_no_Source_chunk(tmp_path):
    """Positive control: "always write Source" passes the test above and
    stamps an empty pointer into every file."""
    src = tmp_path / "in2.png"
    Image.new("RGB", (16, 16), "white").save(src)
    out = tmp_path / "none.png"

    embed(src, out, _rec(doi=None, url=None))

    assert not _text_chunks(out).get("Source"), _text_chunks(out)
    # ...and the record itself still round-trips, or the assertion above would
    # pass on a file that carries no provenance at all.
    got = read_embedded(out.read_bytes())
    assert got and got.citation == _rec().citation


def test_a_dot_jpeg_destination_takes_the_jpeg_arm(tmp_path):
    """`dst.lower().endswith((".jpg", ".jpeg"))` survived mutating ".jpeg".

    Both spellings are ordinary, and the existing test used only ".jpg" -- so
    the tuple's second entry could be replaced and nothing noticed. A BMP
    source carries no metadata of its own, so the JPEG arm is what gives the
    record somewhere to live; without it the file is converted to PNG and the
    .jpeg the caller asked for is never written.
    """
    src = tmp_path / "in.bmp"
    Image.new("RGB", (16, 16), (10, 20, 30)).save(src, "BMP")
    dst = tmp_path / "out.jpeg"

    embed(src, dst, _rec())

    assert dst.exists(), "the .jpeg the caller asked for was not written"
    with Image.open(dst) as im:
        assert im.format == "JPEG", im.format
