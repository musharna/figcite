"""`decorative` is an AND of two comparisons, and only square figures tested it.

    decorative = w_in < min_inches and h_in < min_inches

It decides whether a picture with no provenance is worth reporting: a 0.3-inch
logo in the corner of every slide is not a missing citation, a 4-inch figure is.
The same expression appears in `deck.audit`, `deck.apply`, `pdfdeck.audit` and
`pdfdeck.apply`.

Seven reduced-ROR mutants survived across those four copies, and they survive
because every fixture picture is roughly square -- so both operands always
agree, and an AND whose operands agree can be observed only as a single bit.
Three shapes separate them:

    SMALL     w < min, h < min   both operands True    -> decorative
    WIDE      w > min, h < min   they DISAGREE         -> not decorative
    TALL      w < min, h > min   they DISAGREE         -> not decorative

The two disagreeing shapes are what the `-> !=` mutants need. For a wide, short
banner `w_in < min` is False and short-circuits, but `w_in != min` is True, so
the mutant goes on to find `h_in < min` True and calls the banner decorative --
a rule graphic across the top of a slide, silently exempted from needing a
source. TALL is the mirror.

SMALL is what the `-> False` mutants need: with the comparison forced False
nothing is ever decorative, so every corner logo becomes a missing-citation
report and the signal drowns.
"""

from __future__ import annotations

import pytest
from PIL import Image

from pptx import Presentation
from pptx.util import Inches

from figcite import deck

MIN = 1.0

# (label, width_in, height_in, expected_decorative)
SHAPES = [
    ("small", 0.4, 0.3, True),
    ("wide", 3.0, 0.3, False),
    ("tall", 0.3, 3.0, False),
    ("large", 3.0, 2.5, False),
]


def _png(path):
    Image.new("RGB", (60, 45), (90, 120, 40)).save(path)
    return path


def _deck_with(tmp_path, w_in, h_in, name="shape.pptx"):
    img = _png(tmp_path / "pic.png")
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture(
        str(img), Inches(0.2), Inches(0.2), width=Inches(w_in), height=Inches(h_in)
    )
    path = tmp_path / name
    prs.save(str(path))
    return path


def test_the_shapes_really_do_straddle_the_threshold():
    """The premise. Two of these shapes exist to make the operands DISAGREE,
    and if a future edit made them all agree the tests below would keep passing
    while the AND collapsed back to a single observable bit."""
    disagree = [label for label, w, h, _ in SHAPES if (w < MIN) != (h < MIN)]
    assert sorted(disagree) == ["tall", "wide"], disagree
    both_true = [label for label, w, h, _ in SHAPES if w < MIN and h < MIN]
    assert both_true == ["small"], both_true
    both_false = [label for label, w, h, _ in SHAPES if not (w < MIN) and not (h < MIN)]
    assert both_false == ["large"], both_false


@pytest.mark.parametrize("label,w_in,h_in,expected", SHAPES)
def test_decorative_needs_both_dimensions_small(tmp_path, label, w_in, h_in, expected):
    """A figure is decorative only when BOTH dimensions are under the
    threshold. A banner is wide, a sidebar rule is tall, and neither is a
    thumbnail."""
    path = _deck_with(tmp_path, w_in, h_in, name=f"{label}.pptx")

    out = deck.audit(path, min_inches=MIN)

    assert len(out["rows"]) == 1, out["rows"]
    got = out["rows"][0]["decorative"]
    assert got is expected, (
        f"a {w_in}x{h_in}in picture was reported decorative={got}; "
        f"expected {expected} (threshold {MIN}in)"
    )


def test_a_substantive_picture_with_no_source_is_still_counted(tmp_path):
    """`decorative` exists to filter `untagged_substantive`, so the tally is
    where a wrong answer actually reaches the user. Positive control for the
    parametrized test: it asserts a field, this asserts the field is used."""
    path = _deck_with(tmp_path, 3.0, 2.5, name="big.pptx")

    out = deck.audit(path, min_inches=MIN)

    assert out["untagged_substantive"] == 1, out


def test_a_decorative_picture_with_no_source_is_not_counted(tmp_path):
    """The other half. Without this, "count everything" satisfies the test
    above and `decorative` could stop being consulted at all."""
    path = _deck_with(tmp_path, 0.4, 0.3, name="tiny.pptx")

    out = deck.audit(path, min_inches=MIN)

    assert out["untagged_substantive"] == 0, out


# --- the same rule again, in the writer -------------------------------------
#
# `deck.apply` carries its own copy of the expression, so it carries its own
# four mutants. There it gates `missing.append(si)`, which becomes the
# "⚠ N image(s) on slide(s) ..." warning on the credits page and the
# `unsourced` count in the return value -- so a banner wrongly called
# decorative is a figure whose missing source is never reported.


@pytest.mark.parametrize("label,w_in,h_in,expected_decorative", SHAPES)
def test_apply_reports_an_unsourced_picture_unless_it_is_decorative(
    tmp_path, label, w_in, h_in, expected_decorative
):
    path = _deck_with(tmp_path, w_in, h_in, name=f"apply-{label}.pptx")
    out = tmp_path / f"apply-{label}.cited.pptx"

    rep = deck.apply(path, out, captions=True, credits=True, min_inches=MIN)

    expected_unsourced = 0 if expected_decorative else 1
    assert rep["unsourced"] == expected_unsourced, (
        f"a {w_in}x{h_in}in picture with no provenance: unsourced="
        f"{rep['unsourced']}, expected {expected_unsourced}"
    )


# --- and twice more, in the PDF writer --------------------------------------
#
# `pdfdeck` carries the same rule with an extra conjunct:
#
#     decorative = bool(r) and w_in < min_inches and h_in < min_inches
#
# PDF geometry is in points, so the shapes are built at 72 * inches. The
# `bool(r)` guard is why an image with no placement rect is never decorative --
# unmeasurable is not the same as small, which is the same three-outcome rule
# this project applies everywhere else.


def _pdf_with(tmp_path, w_in, h_in, name="shape.pdf"):
    import fitz

    img = _png(tmp_path / "pdfpic.png")
    doc = fitz.open()
    page = doc.new_page()
    # keep_proportion=False on purpose. PyMuPDF preserves aspect ratio by
    # default, so a 4:3 source dropped into a 3.0x0.3in box is scaled to FIT --
    # it lands at roughly 0.4x0.3in and really is decorative. The first version
    # of this fixture did that and the wide/tall cases quietly became small
    # ones, which is the fixture testing a shape it did not ask for.
    page.insert_image(
        fitz.Rect(20, 20, 20 + w_in * 72, 20 + h_in * 72),
        filename=str(img),
        keep_proportion=False,
    )
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return path


@pytest.mark.parametrize("label,w_in,h_in,expected", SHAPES)
def test_pdf_decorative_needs_both_dimensions_small(tmp_path, label, w_in, h_in, expected):
    from figcite import pdfdeck

    path = _pdf_with(tmp_path, w_in, h_in, name=f"pdf-{label}.pdf")

    out = pdfdeck.audit(path, min_inches=MIN)

    assert len(out["rows"]) == 1, out["rows"]
    # The geometry actually on the page, not the geometry requested. A fixture
    # whose image was rescaled on insert would otherwise test a different
    # shape than the one it is named for, silently.
    got_w, got_h = out["rows"][0]["size_in"]
    assert abs(got_w - w_in) < 0.1 and abs(got_h - h_in) < 0.1, (
        f"the {label} image was placed at {got_w}x{got_h}in, not {w_in}x{h_in}in"
    )
    got = out["rows"][0]["decorative"]
    assert got is expected, (
        f"a {w_in}x{h_in}in PDF image was reported decorative={got}; "
        f"expected {expected} (threshold {MIN}in)"
    )


@pytest.mark.parametrize("label,w_in,h_in,expected_decorative", SHAPES)
def test_pdf_apply_reports_an_unsourced_image_unless_it_is_decorative(
    tmp_path, label, w_in, h_in, expected_decorative
):
    from figcite import pdfdeck

    path = _pdf_with(tmp_path, w_in, h_in, name=f"pdfapply-{label}.pdf")
    out = tmp_path / f"pdfapply-{label}.cited.pdf"

    rep = pdfdeck.apply(path, out, captions=True, credits=True, min_inches=MIN)

    expected_unsourced = 0 if expected_decorative else 1
    assert rep["unsourced"] == expected_unsourced, (
        f"a {w_in}x{h_in}in PDF image with no provenance: unsourced="
        f"{rep['unsourced']}, expected {expected_unsourced}"
    )


# --- the manifest key filter, which is an INEQUALITY over an open key set ----
#
# Both writers serialise each row as
#
#     {**{k: v for k, v in r.items() if k != "record"},
#      "record": asdict(r["record"]) if r["record"] else None}
#
# The filter keeps the raw Record object out of the comprehension; the explicit
# key puts a serialised one back. Narrowed to an ORDER the filter stops naming
# one key and names a half-line of them, and the JSON manifest quietly loses
# columns:
#
#     k > "record"   drops "matched_by" and "n"      (deck)
#     k < "record"   drops "shape" and "slide"       (pdfdeck)
#
# Nothing asserted the manifest's key SET, only individual values, so a row
# missing half its fields still satisfied every existing test.
#
# (`k != "record"` -> `True` is a genuine equivalence: the explicit "record"
# key overwrites whatever the comprehension let through, so the output dict is
# byte-identical. The filter is defensive, not load-bearing -- it would start
# mattering the moment someone put the `**` expansion after the explicit key.)

# Per writer, because they are NOT the same set -- and the difference decides
# whether the same mutation is a defect or a no-op:
#
#   deck rows    n, slide, shape, matched_by, record
#                "shape" and "slide" sort ABOVE "record", "matched_by" and "n"
#                below, so the key set straddles the filtered name and BOTH
#                narrowings drop something. Both are real.
#
#   pdfdeck rows n, page, matched_by, record
#                every other key sorts BELOW "record" -- there is no "slide" or
#                "shape", only "page". So `k < "record"` admits exactly what
#                `k != "record"` admits and is EQUIVALENT, while `k > "record"`
#                would drop the lot.
#
# Same expression, same mutation, opposite verdicts, purely because one row
# happens to carry a key spelled "slide" and the other one spelled "page".
DECK_MANIFEST_KEYS = {"n", "slide", "shape", "matched_by", "record"}
PDF_MANIFEST_KEYS = {"n", "page", "matched_by", "record"}


def test_the_json_manifest_keeps_every_column(tmp_path):
    import json

    path = _deck_with(tmp_path, 3.0, 2.5, name="manifest.pptx")
    out = tmp_path / "manifest.cited.pptx"

    deck.apply(path, out, captions=True, credits=True, manifest_path=tmp_path / "man")

    rows = json.loads((tmp_path / "man.json").read_text(encoding="utf-8"))
    assert rows, "the manifest is empty"
    for row in rows:
        missing = DECK_MANIFEST_KEYS - set(row)
        assert not missing, (
            f"the JSON manifest row lost {sorted(missing)}; a key filter that "
            f"compares for ORDER rather than identity drops everything on one "
            f"side of 'record'. Row: {sorted(row)}"
        )


def test_the_pdf_json_manifest_keeps_every_column(tmp_path):
    import json

    from figcite import pdfdeck

    path = _pdf_with(tmp_path, 3.0, 2.5, name="pdfmanifest.pdf")
    out = tmp_path / "pdfmanifest.cited.pdf"

    pdfdeck.apply(path, out, captions=True, credits=True, manifest_path=tmp_path / "pdfman")

    rows = json.loads((tmp_path / "pdfman.json").read_text(encoding="utf-8"))
    assert rows, "the manifest is empty"
    for row in rows:
        missing = PDF_MANIFEST_KEYS - set(row)
        assert not missing, f"the PDF JSON manifest row lost {sorted(missing)}. Row: {sorted(row)}"


def test_the_deck_manifest_keys_straddle_the_filtered_one():
    """The premise for the pptx case. Both narrowings drop something only
    because this row carries keys on BOTH sides of "record"."""
    others = DECK_MANIFEST_KEYS - {"record"}
    assert sorted(k for k in others if k < "record") == ["matched_by", "n"]
    assert sorted(k for k in others if k > "record") == ["shape", "slide"]


def test_the_pdf_manifest_keys_all_sort_below_the_filtered_one():
    """And the premise for the PDF case being different.

    Recorded as an assertion rather than a comment because it is the whole
    reason `k < "record"` is equivalent there. Add a "slide"- or "shape"-like
    key to the PDF rows and that stops being true, so this fails and says which
    equivalence went stale.
    """
    others = PDF_MANIFEST_KEYS - {"record"}
    assert all(k < "record" for k in others), sorted(others)
