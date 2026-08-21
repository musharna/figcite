"""The confirmed gate on a PDF, asserted per surface rather than per document.

`pdfdeck.apply` writes a citation to TWO independent places, each with its own
copy of the same gate:

    _caption_text(...)   if rec.confirmed or allow_unconfirmed:   -> the caption
    _credit_line(...)    if rec.confirmed or allow_unconfirmed:   -> the credits page

Both survived `or -> and` in the sweep, and the reason is worth recording
because it is not "no test touches this". `test_pdf_apply_writes_credits_and_manifest`
does exercise both, but it asserts

    text = "\\n".join(doc[i].get_text() for i in range(doc.page_count))
    assert "Shiragaki" in text

over the CONCATENATED document. Mutating one gate leaves the other surface
still printing the name, so the assertion holds and the mutant lives. Mutating
BOTH is caught -- verified by doing exactly that, which is how this was
diagnosed.

**An assertion over concatenated surfaces cannot attribute.** It answers "does
this name appear anywhere" when the property under test is "does each surface
independently honour the gate". Two copies of a rule need two observations, or
either copy can be deleted for free.

The mutation direction here withholds MORE than it should -- a legitimately
confirmed citation renders as "source unconfirmed" on every figure. That is the
safe direction for provenance and the wrong one for the feature: a deck whose
every credit reads "unconfirmed" is broken, and nothing would have failed.
"""

from __future__ import annotations

import random

import fitz
import pytest
from PIL import Image, ImageDraw

from figcite import store
from figcite.pdfdeck import apply
from figcite.provenance import Record, embed

CITE = "Shiragaki et al. (2020). Horticulturae 6: 87."
SHORT = "Shiragaki et al. 2020"
DOI = "10.3390/horticulturae6040087"


@pytest.fixture(autouse=True)
def _own_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MANIFEST", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)


def _figure(path, seed, size=(520, 360)):
    rng = random.Random(seed)
    im = Image.new("RGB", size, (250, 250, 248))
    d = ImageDraw.Draw(im)
    for _ in range(28):
        x0, y0 = rng.randrange(size[0] - 60), rng.randrange(size[1] - 60)
        w, h = rng.randrange(20, 90), rng.randrange(15, 70)
        col = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        d.rectangle([x0, y0, x0 + w, y0 + h], fill=col)
    im.save(path)
    return path


def _staged_pdf(tmp_path, *, confirmed):
    raw = _figure(tmp_path / "raw.png", seed=7)
    rec = Record(
        doi=DOI,
        citation=CITE,
        short_cite=SHORT,
        confirmed=confirmed,
        source_kind="pdf-crop",
    )
    img = tmp_path / "fig.png"
    rec = embed(raw, img, rec)
    store.put(rec)

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(60, 60, 400, 290), filename=str(img))
    pdf = tmp_path / "export.pdf"
    doc.save(str(pdf))
    doc.close()
    return pdf


def _surfaces(out_path):
    """(figure-page text, credits-page text) -- kept apart on purpose.

    Joining these is precisely the mistake this file exists to correct.
    """
    doc = fitz.open(str(out_path))
    assert doc.page_count >= 2, "no credits page was appended"
    figure_page = doc[0].get_text()
    credits_page = doc[doc.page_count - 1].get_text()
    doc.close()
    return figure_page, credits_page


def test_a_confirmed_citation_reaches_both_surfaces_independently(tmp_path):
    """THE finding. Each surface is asserted on its own, so neither can cover
    for the other's gate."""
    pdf = _staged_pdf(tmp_path, confirmed=True)
    out = tmp_path / "cited.pdf"

    apply(pdf, out, captions=True, credits=True)
    figure_page, credits_page = _surfaces(out)

    assert SHORT in figure_page, (
        f"the CAPTION did not carry the confirmed citation: {figure_page!r}"
    )
    assert "unconfirmed" not in figure_page.lower(), figure_page

    assert "Shiragaki" in credits_page, (
        f"the CREDITS PAGE did not carry the confirmed citation: {credits_page!r}"
    )
    assert "unconfirmed" not in credits_page.lower(), credits_page


def test_an_unconfirmed_record_is_withheld_from_both_surfaces(tmp_path):
    """The other polarity, also per surface. Without this the test above is
    satisfied by a build that never consults `confirmed` at all."""
    pdf = _staged_pdf(tmp_path, confirmed=False)
    out = tmp_path / "guess.pdf"

    apply(pdf, out, captions=True, credits=True)
    figure_page, credits_page = _surfaces(out)

    assert SHORT not in figure_page, (
        f"a machine guess was captioned onto the figure: {figure_page!r}"
    )
    assert "unconfirmed" in figure_page.lower(), figure_page

    assert "Shiragaki" not in credits_page, (
        f"a machine guess was credited on the credits page: {credits_page!r}"
    )
    assert "UNCONFIRMED" in credits_page, credits_page


def test_allow_unconfirmed_releases_both_surfaces(tmp_path):
    """The override has to reach both gates. Reaching only one produces a deck
    that captions a citation it will not credit, or the reverse."""
    pdf = _staged_pdf(tmp_path, confirmed=False)
    out = tmp_path / "forced.pdf"

    apply(pdf, out, captions=True, credits=True, allow_unconfirmed=True)
    figure_page, credits_page = _surfaces(out)

    assert SHORT in figure_page, (
        f"--allow-unconfirmed did not reach the caption gate: {figure_page!r}"
    )
    assert "Shiragaki" in credits_page, (
        f"--allow-unconfirmed did not reach the credits gate: {credits_page!r}"
    )


def test_the_two_surfaces_are_switched_independently(tmp_path):
    """`captions=False` must not silence the credits page, and vice versa.
    One shared switch passes every test above."""
    pdf = _staged_pdf(tmp_path, confirmed=True)

    no_caps = tmp_path / "nocaps.pdf"
    apply(pdf, no_caps, captions=False, credits=True)
    figure_page, credits_page = _surfaces(no_caps)
    assert SHORT not in figure_page, f"captions=False still captioned: {figure_page!r}"
    assert "Shiragaki" in credits_page, (
        f"captions=False also emptied the credits page: {credits_page!r}"
    )

    no_cred = tmp_path / "nocred.pdf"
    apply(pdf, no_cred, captions=True, credits=False)
    doc = fitz.open(str(no_cred))
    all_text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
    doc.close()
    assert SHORT in all_text, "credits=False also suppressed the caption"
    assert "Image credits" not in all_text, "credits=False still wrote a credits page"


# --- the unsourced warning is a surface too ------------------------------


def _unsourced_pdf(tmp_path, w_in, h_in, seed, name="bare.pdf"):
    """A PDF holding one figure that is in no manifest, at a stated size."""
    img = _figure(tmp_path / f"bare-{seed}.png", seed=seed, size=(1200, 120))
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(
        fitz.Rect(40, 40, 40 + w_in * 72, 40 + h_in * 72), filename=str(img)
    )
    p = tmp_path / name
    doc.save(str(p))
    doc.close()
    return p


def test_a_wide_short_unsourced_figure_is_still_warned_about(tmp_path):
    """`apply` keeps its OWN copy of the decorative rule (pdfdeck.py:164), and
    it survived `and -> or` separately from the copy in `audit`.

    Here the consequence is on the delivered document rather than on a report:
    a 6.0x0.6in figure with no record becomes "decorative", never reaches
    `missing`, and the credits page stops saying anything about it. The deck
    then reads as fully sourced while carrying an uncredited figure -- which is
    the single outcome this tool exists to prevent.
    """
    pdf = _unsourced_pdf(tmp_path, 6.0, 0.6, seed=131)
    out = tmp_path / "strip.cited.pdf"

    rep = apply(pdf, out, captions=True, credits=True, min_inches=1.0)

    assert rep["unsourced"] == 1, (
        f"a 6.0x0.6in uncredited figure was written off as decorative: {rep}"
    )
    doc = fitz.open(str(out))
    credits_page = doc[doc.page_count - 1].get_text()
    doc.close()
    assert "no recorded source" in credits_page, (
        f"the credits page did not warn about it: {credits_page!r}"
    )


def test_a_small_icon_is_not_warned_about(tmp_path):
    """Positive control. "Warn about everything" passes the test above while
    flooding every deck's credits page with bullet icons."""
    pdf = _unsourced_pdf(tmp_path, 0.4, 0.4, seed=132, name="icon.pdf")
    out = tmp_path / "icon.cited.pdf"

    rep = apply(pdf, out, captions=True, credits=True, min_inches=1.0)

    assert rep["unsourced"] == 0, f"a 0.4x0.4in icon was reported as unsourced: {rep}"
