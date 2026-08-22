"""`figcite audit` on a PDF, tested as rules — the pptx twin's tests do not cover it.

`pdfdeck.audit` is an INDEPENDENT reimplementation of `deck.audit`: pdfdeck.py
does not import deck, and the same three rules are spelled out again. When the
sweep's findings in `deck.py` were fixed (983eb8a, tests/test_deck_audit_tallies.py)
the PDF copy was left exactly as it was, and the same three mutants are still
alive in it:

  - `decorative = bool(r) and w_in < min_inches and h_in < min_inches`
    survives `and -> or`, so a wide, short figure (a panel strip, a timeline)
    is written off as decorative and drops out of the unsourced tally.
  - `unconfirmed = [... if not r["record"].confirmed]` survives dropping `not`.
  - `untagged = [... if r["record"] is None and ...]` survives `is -> is not`.

The existing test that looks like it covers the third one is
`test_pdf_audit_finds_tagged_and_names_the_gap`, and it cannot: its fixture is
one tagged figure and one untagged one, asserting `untagged_substantive == 1`.
**An inverted filter returns 1 from that fixture too.** Same cardinality trap as
F11 in the deck tests — a count cannot tell a set from its complement when both
halves are the same size — so every fixture here is deliberately asymmetric.

The decorative predicate exists in FOUR places (deck.audit, deck.apply,
pdfdeck.audit, pdfdeck.apply) and the two pdfdeck copies additionally guard on
`bool(r)`, because a PDF xref may carry no placement rect where a pptx shape
always has a size. That difference is real, so the copies are not
interchangeable — which is exactly what makes four independently editable
copies of one rule worth pinning at both ends.
"""

from __future__ import annotations

import random

import fitz
import pytest
from PIL import Image, ImageDraw

from figcite import store
from figcite.pdfdeck import audit
from figcite.provenance import Record, embed

CITE = "Shiragaki et al. (2020). Horticulturae 6: 87."
DOI = "10.3390/horticulturae6040087"


@pytest.fixture(autouse=True)
def _own_manifest(tmp_path, monkeypatch):
    """One manifest per test.

    conftest gives the whole SESSION a single FIGCITE_HOME, and `match_image`
    falls back to a fuzzy dhash search, so records filed by an earlier test
    match a deliberately-bare figure here and the tally comes out wrong for a
    reason unrelated to the code. The deck twin was caught exactly this way.
    """
    monkeypatch.setattr(store, "MANIFEST", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)


def _figure(path, seed, size=(520, 360)):
    """Structured, not a flat fill — a degenerate dhash would match anything."""
    rng = random.Random(seed)
    im = Image.new("RGB", size, (250, 250, 248))
    d = ImageDraw.Draw(im)
    for _ in range(28):
        x0, y0 = rng.randrange(size[0] - 60), rng.randrange(size[1] - 60)
        w, h = rng.randrange(20, 90), rng.randrange(15, 70)
        col = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        if rng.random() < 0.5:
            d.rectangle([x0, y0, x0 + w, y0 + h], fill=col)
        else:
            d.ellipse([x0, y0, x0 + w, y0 + h], fill=col)
    im.save(path)
    return path


def _tagged(tmp_path, name, seed, *, confirmed=True):
    raw = _figure(tmp_path / f"raw-{name}", seed)
    rec = Record(
        doi=DOI,
        citation=CITE,
        short_cite="Shiragaki et al. 2020",
        confirmed=confirmed,
        source_kind="pdf-crop",
    )
    out = tmp_path / name
    rec = embed(raw, out, rec)
    store.put(rec)
    return out


def _pdf(tmp_path, placed, name="export.pdf"):
    """`placed` is [(image_path, width_in, height_in)].

    Sizes are the whole point of this file, so each is stated explicitly and
    converted to points here rather than inherited from a shared default.
    """
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    y = 40.0
    for img, w_in, h_in in placed:
        w, h = w_in * 72, h_in * 72
        page.insert_image(fitz.Rect(40, y, 40 + w, y + h), filename=str(img))
        y += h + 12
    p = tmp_path / name
    doc.save(str(p))
    doc.close()
    return p


# --- the decorative rule -------------------------------------------------


def test_a_wide_short_figure_is_substantive_not_decorative(tmp_path):
    """THE finding, in the copy that was never fixed.

    6.0 x 0.6in: small in one dimension, large in the other — a panel strip or
    a timeline, exactly the kind of figure that needs a citation. Under
    `and -> or` it becomes decorative and vanishes from the unsourced count
    while the audit still reports success.
    """
    strip = _figure(tmp_path / "strip.png", seed=11, size=(1200, 120))
    pdf = _pdf(tmp_path, [(strip, 6.0, 0.6)])

    rep = audit(pdf, min_inches=1.0)

    row = rep["rows"][0]
    assert row["decorative"] is False, (
        f"a 6.0x0.6in figure was written off as decorative: {row['size_in']}"
    )
    assert rep["untagged_substantive"] == 1, (
        f"an uncredited 6.0x0.6in figure is missing from the tally: {rep}"
    )


def test_a_figure_small_in_both_dimensions_is_decorative(tmp_path):
    """Positive control. Without it, "nothing is ever decorative" passes the
    test above while every bullet icon floods the report."""
    icon = _figure(tmp_path / "icon.png", seed=3, size=(64, 64))
    pdf = _pdf(tmp_path, [(icon, 0.4, 0.4)])

    rep = audit(pdf, min_inches=1.0)

    assert rep["rows"][0]["decorative"] is True, rep["rows"][0]
    assert rep["untagged_substantive"] == 0, (
        f"a 0.4x0.4in icon was counted as an unsourced figure: {rep}"
    )


def test_a_figure_exactly_at_the_threshold_is_substantive(tmp_path):
    """`<` vs `<=`. At exactly min_inches a figure is not BELOW the threshold,
    so it counts — erring toward naming a figure rather than dropping it."""
    img = _figure(tmp_path / "exact.png", seed=17, size=(300, 300))
    pdf = _pdf(tmp_path, [(img, 1.0, 1.0)])

    rep = audit(pdf, min_inches=1.0)

    assert rep["rows"][0]["decorative"] is False, rep["rows"][0]
    assert rep["untagged_substantive"] == 1, (
        f"a figure exactly at the threshold was dropped from the tally: {rep}"
    )


@pytest.mark.parametrize(
    "w_in,h_in",
    [(1.0, 0.5), (0.5, 1.0)],
    ids=["wide-at-threshold", "tall-at-threshold"],
)
def test_only_one_dimension_at_the_threshold_is_still_substantive(tmp_path, w_in, h_in):
    """The predicate is TWO comparisons, and a square fixture cannot tell them
    apart.

        decorative = bool(r) and w_in < min_inches and h_in < min_inches

    The re-run of the logic sweep found `Lt -> LtE` surviving here (and in the
    deck twin) even with the threshold test above in place. At 1.0x1.0 both
    comparisons are False, so flipping EITHER one to `<=` leaves the `and`
    False and nothing observable changes -- the test passes on the mutant.

    Asymmetric boundaries separate them. At 1.0x0.5 only the first comparison
    sits on the boundary; at 0.5x1.0 only the second. Each case kills exactly
    one of the two mutants, and neither can cover for the other.

    The behaviour being pinned is real: under `<=`, a figure exactly one inch
    wide and half an inch tall -- a panel strip -- becomes decorative and
    drops out of the unsourced tally.
    """
    # The source image's aspect must MATCH the target rect. `insert_image`
    # keeps proportions, so feeding a 2:1 image into a 1:2 rect letterboxes
    # it and the placed height comes back smaller than asked for -- which
    # made this test fail on unmutated code, reporting a fixture bug as a
    # defect. Derive the pixel size from the requested inches instead.
    img = _figure(
        tmp_path / f"edge-{w_in}x{h_in}.png",
        seed=19,
        size=(int(w_in * 600), int(h_in * 600)),
    )
    pdf = _pdf(tmp_path, [(img, w_in, h_in)])

    rep = audit(pdf, min_inches=1.0)

    assert rep["rows"][0]["decorative"] is False, (
        f"a {w_in}x{h_in}in figure with one dimension exactly at the threshold "
        f"was written off as decorative: {rep['rows'][0]}"
    )
    assert rep["untagged_substantive"] == 1, rep


def test_the_two_size_rules_are_applied_per_figure(tmp_path):
    """Both kinds in one PDF, so the tally has to discriminate rather than
    apply one verdict to everything."""
    strip = _figure(tmp_path / "s.png", seed=11, size=(1200, 120))
    icon = _figure(tmp_path / "i.png", seed=3, size=(64, 64))
    big = _figure(tmp_path / "b.png", seed=5)
    pdf = _pdf(tmp_path, [(strip, 6.0, 0.6), (icon, 0.4, 0.4), (big, 3.0, 2.2)])

    rep = audit(pdf, min_inches=1.0)

    assert rep["pictures"] == 3, rep
    assert rep["untagged_substantive"] == 2, (
        f"expected the strip and the big figure, not the icon: {rep}"
    )


# --- the two filters, with asymmetric fixtures ---------------------------


def test_an_unconfirmed_record_is_counted_as_unconfirmed(tmp_path):
    """`unconfirmed` survives dropping its `not`, which inverts the list.

    2-and-1, never 1-and-1: a cardinality assertion cannot discriminate a set
    from its complement when both halves are the same size.
    """
    yes = _tagged(tmp_path, "confirmed.png", seed=7, confirmed=True)
    no1 = _tagged(tmp_path, "unconfirmed-1.png", seed=23, confirmed=False)
    no2 = _tagged(tmp_path, "unconfirmed-2.png", seed=61, confirmed=False)
    pdf = _pdf(tmp_path, [(yes, 3.0, 2.2), (no1, 3.0, 2.2), (no2, 3.0, 2.2)])

    rep = audit(pdf, min_inches=1.0)

    assert rep["tagged"] == 3, f"all three should match the manifest: {rep}"
    assert rep["unconfirmed"] == 2, (
        f"expected the two unconfirmed ones; 1 would mean the filter is inverted: {rep}"
    )


def test_a_tagged_picture_is_not_counted_as_unsourced(tmp_path):
    """`untagged` survives `is -> is not`.

    This is the test the existing `test_pdf_audit_finds_tagged_and_names_the_gap`
    could not be: its 1-and-1 fixture returns 1 under the inverted filter too.
    """
    tagged = _tagged(tmp_path, "has-record.png", seed=7)
    bare1 = _figure(tmp_path / "bare-1.png", seed=41)
    bare2 = _figure(tmp_path / "bare-2.png", seed=83)
    pdf = _pdf(tmp_path, [(tagged, 3.0, 2.2), (bare1, 3.0, 2.2), (bare2, 3.0, 2.2)])

    rep = audit(pdf, min_inches=1.0)

    assert rep["tagged"] == 1, rep
    assert rep["untagged_substantive"] == 2, (
        f"expected the two bare ones; 1 would mean the filter is inverted: {rep}"
    )


# --- the one thing the pptx twin does NOT do -----------------------------


def test_an_image_that_is_never_placed_is_reported_not_dropped(tmp_path):
    """`bool(r) and ...` — the only part of this predicate that differs from
    `deck.audit`, and the part my other fixtures could not reach.

    A PDF can carry an image in a page's /Resources that the content stream
    never paints; `page.get_image_rects()` returns [] for it, so `r` is None
    and `w_in`/`h_in` are 0.0. WITHOUT the `bool(r)` guard, `0.0 < min_inches`
    holds in both dimensions, the image is written off as decorative, and it
    vanishes from the unsourced tally entirely. WITH it, the image is reported.

    Reporting is the right direction and is the rest of this file's rule too:
    an audit that quietly drops something it cannot measure tells you a deck is
    clean when it has an unaccounted-for figure in it. A pptx shape always has
    a size, which is why the twin has no such guard and why unifying the two
    copies would not be behaviour-preserving.
    """
    img = _figure(tmp_path / "unplaced.png", seed=97)
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(40, 40, 240, 240), filename=str(img))
    # Leave the image in /Resources but paint nothing: this is what an
    # exporter that revised a page without garbage-collecting leaves behind.
    for cx in page.get_contents():
        doc.update_stream(cx, b" ")
    pdf = tmp_path / "unplaced.pdf"
    doc.save(str(pdf))
    doc.close()

    rep = audit(pdf, min_inches=1.0)

    assert rep["pictures"] == 1, f"the unplaced image was not seen at all: {rep}"
    assert rep["rows"][0]["size_in"] == [0.0, 0.0], rep["rows"][0]
    assert rep["rows"][0]["decorative"] is False, (
        "an image with no placement rect was written off as decorative, so an "
        "unaccounted-for figure disappears from the audit"
    )
    assert rep["untagged_substantive"] == 1, rep
