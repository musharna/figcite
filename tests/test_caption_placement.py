"""Where a caption goes, and how many lines a credit needs.

Four survivors, all `>` narrowed to `!=`, and all four flip the NORMAL case
rather than the edge case -- which is why nothing noticed. Each guard reads
"is there room?", so the ordinary answer is "yes, plenty", and `a > b` False
becomes `a != b` True the moment there is any slack at all.

    deck._add_caption    `top + cap_h > prs.slide_height`
                         -> every caption sits ON the image instead of below it

    pdfdeck._caption     `y0 + h > page.rect.y1`
                         -> the same, in the PDF writer

    pdfdeck._measure     `cur + w > width`
                         -> every WORD starts a new line, so a one-line credit
                            is measured as needing as many lines as it has
                            words, and the credits pages multiply

The existing tests assert that a caption EXISTS and that its text is right.
None of them asks where it is, and `_measure` had no direct test at all -- its
output is consumed as a height, which downstream turns into a page count that
nothing pins either.
"""

from __future__ import annotations

import fitz
import pytest
from PIL import Image

from pptx import Presentation
from pptx.util import Inches

from figcite import deck, pdfdeck
from figcite.deck import CAPTION_PREFIX
from figcite.pdfdeck import CAPTION_PT, _measure
from figcite.provenance import Record, embed
from figcite import store

CITE = "Shiragaki et al. 2020"


def _figure(path):
    Image.new("RGB", (120, 90), (40, 90, 140)).save(path)
    return path


def _filed(tmp_path, name="fig.png"):
    """A figure whose provenance is already known, so a caption gets written."""
    raw = _figure(tmp_path / f"raw-{name}")
    img = tmp_path / name
    rec = embed(
        raw, img, Record(citation=CITE, short_cite=CITE, confirmed=True, doi="10.1/x")
    )
    store.put(rec)
    return img


# --- _measure: the arithmetic one -------------------------------------------


def test_words_that_fit_on_one_line_are_measured_as_one_line():
    """`cur + w > width` is False for every word that fits. Narrowed to `!=`
    it is True for every word that does not exactly fill the line, so each one
    increments the line count."""
    one_line = 1 * 10 * 1.35 + 4

    assert _measure("hello", 500, 10) == pytest.approx(one_line)
    assert _measure("hello world again", 500, 10) == pytest.approx(one_line), (
        "three short words on a 500pt line were measured as more than one line"
    )


def test_words_that_do_not_fit_still_wrap():
    """Positive control. "Always one line" passes the test above and makes the
    measurement useless in the other direction."""
    narrow = _measure(" ".join(["word"] * 60), 200, 10)
    wide = _measure(" ".join(["word"] * 60), 5000, 10)

    assert narrow > wide, (narrow, wide)
    assert wide == pytest.approx(1 * 10 * 1.35 + 4), wide


def test_the_measurement_scales_with_the_number_of_lines():
    """Pins the shape of the return value, so 'wrapping happens' cannot be
    satisfied by an arbitrary larger number."""
    per_line = 10 * 1.35
    one = _measure("word", 5000, 10)
    many = _measure(" ".join(["word"] * 60), 200, 10)

    lines = round((many - 4) / per_line)
    assert lines > 1
    assert many == pytest.approx(lines * per_line + 4)
    assert one == pytest.approx(1 * per_line + 4)


# --- caption placement, pptx ------------------------------------------------


def _deck(tmp_path, img, top_in, height_in=2.0, name="d.pptx"):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture(
        str(img), Inches(0.5), Inches(top_in), width=Inches(3), height=Inches(height_in)
    )
    p = tmp_path / name
    prs.save(str(p))
    return p


def _caption_and_picture(path):
    prs = Presentation(str(path))
    cap = pic = None
    for slide in prs.slides:
        for sh in slide.shapes:
            if sh.name.startswith(CAPTION_PREFIX):
                cap = sh
            elif deck._tag(sh) == "pic":
                pic = sh
    return cap, pic, prs


def test_a_caption_goes_BELOW_a_picture_that_has_room(tmp_path):
    """The ordinary case, and the one the mutation breaks."""
    img = _filed(tmp_path)
    src = _deck(tmp_path, img, top_in=0.5, name="room.pptx")
    out = tmp_path / "room.cited.pptx"

    deck.apply(src, out, captions=True, credits=False)

    cap, pic, _ = _caption_and_picture(out)
    assert cap is not None, "no caption was written"
    assert pic is not None
    assert cap.top >= pic.top + pic.height, (
        f"the caption was placed ON the image: caption top {cap.top}, image "
        f"bottom {pic.top + pic.height}"
    )


def test_a_caption_sits_on_a_picture_with_no_room_below(tmp_path):
    """The edge case the guard exists for: a picture at the foot of the slide
    would otherwise get a caption off the bottom of it."""
    img = _filed(tmp_path, name="fig2.png")
    prs = Presentation()
    slide_h_in = prs.slide_height / 914400
    src = _deck(
        tmp_path,
        img,
        top_in=slide_h_in - 2.05,
        height_in=2.0,
        name="noroom.pptx",
    )
    out = tmp_path / "noroom.cited.pptx"

    deck.apply(src, out, captions=True, credits=False)

    cap, pic, prs2 = _caption_and_picture(out)
    assert cap is not None, "no caption was written"
    assert cap.top + cap.height <= prs2.slide_height, (
        f"the caption ran off the bottom of the slide: {cap.top + cap.height} "
        f"> {prs2.slide_height}"
    )
    assert cap.top < pic.top + pic.height, (
        "there was no room below, so the caption should sit on the image"
    )


# --- caption placement, pdf -------------------------------------------------


def _pdf(tmp_path, img, y0_pt, h_pt=140, name="d.pdf"):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(
        fitz.Rect(40, y0_pt, 40 + 200, y0_pt + h_pt),
        filename=str(img),
        keep_proportion=False,
    )
    p = tmp_path / name
    doc.save(str(p))
    doc.close()
    return p


def _caption_y(path, needle):
    doc = fitz.open(str(path))
    try:
        for block in doc[0].get_text("blocks"):
            if needle in block[4]:
                return block[1], doc[0].rect.y1  # y0 of the text block, page bottom
    finally:
        doc.close()
    return None, None


def test_a_pdf_caption_goes_below_an_image_that_has_room(tmp_path):
    img = _filed(tmp_path, name="fig3.png")
    src = _pdf(tmp_path, img, y0_pt=60, name="pdfroom.pdf")
    out = tmp_path / "pdfroom.cited.pdf"

    pdfdeck.apply(src, out, captions=True, credits=False)

    y, page_bottom = _caption_y(out, "Shiragaki")
    assert y is not None, "no caption text was written into the PDF"
    assert y >= 60 + 140, (
        f"the caption was placed at y={y}, which is not below an image ending "
        f"at y={60 + 140}"
    )
    assert y < page_bottom, (y, page_bottom)


def test_a_pdf_caption_stays_on_the_page_when_there_is_no_room(tmp_path):
    """Positive control for the guard: an image at the foot of the page."""
    img = _filed(tmp_path, name="fig4.png")
    doc = fitz.open()
    page_h = doc.new_page().rect.y1
    doc.close()

    src = _pdf(tmp_path, img, y0_pt=page_h - 145, name="pdfnoroom.pdf")
    out = tmp_path / "pdfnoroom.cited.pdf"

    pdfdeck.apply(src, out, captions=True, credits=False)

    y, page_bottom = _caption_y(out, "Shiragaki")
    assert y is not None, "no caption text was written into the PDF"
    assert 0 <= y <= page_bottom - (2 * CAPTION_PT), (
        f"the caption was placed at y={y} on a page ending at {page_bottom}"
    )
