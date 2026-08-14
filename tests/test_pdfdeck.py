"""PDF as an output format: Affinity, Illustrator, InDesign, Slides, LaTeX.

The load-bearing claim is that a PDF export destroys layers 1 and 2 but not
layer 3. It is tested with a discrimination control, because a hash that matched
everything would also "survive" export.
"""
import random

import fitz
import pytest
from PIL import Image, ImageDraw

from figcite import store
from figcite.pdfdeck import apply, audit, match_image
from figcite.provenance import (Record, dhash_bytes, embed, hamming,
                                read_embedded, sha256_bytes)

CITE = "Shiragaki et al. (2020). Horticulturae 6: 87."
DOI = "10.3390/horticulturae6040087"


def _figure(path, seed, size=(520, 360)):
    """A structured image -- not a flat gradient, whose dhash would be degenerate."""
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
    rec = Record(doi=DOI, citation=CITE, short_cite="Shiragaki et al. 2020",
                 license_url="https://creativecommons.org/licenses/by/4.0",
                 reuse="reuse-ok-attribution-required", confirmed=confirmed,
                 source_kind="pdf-crop")
    out = tmp_path / name
    rec = embed(raw, out, rec)
    store.put(rec)
    return out, rec


def _pdf_with(tmp_path, images, name="export.pdf"):
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    y = 60
    for img in images:
        page.insert_image(fitz.Rect(60, y, 400, y + 230), filename=str(img))
        y += 250
    p = tmp_path / name
    doc.save(str(p))
    doc.close()
    return p


def test_pdf_export_kills_layers_1_and_2_but_not_layer_3(tmp_path):
    """Sensitive AND specific: the right figure matches, a different one does not."""
    a, reca = _tagged(tmp_path, "A.png", seed=1)
    b, recb = _tagged(tmp_path, "B.png", seed=999)
    assert hamming(reca.dhash, recb.dhash) > 6, \
        "the two test figures are too similar for this test to discriminate"

    pdf = _pdf_with(tmp_path, [a])
    doc = fitz.open(str(pdf))
    blob = doc.extract_image(doc[0].get_images(full=True)[0][0])["image"]
    doc.close()

    assert read_embedded(blob) is None, "expected PDF export to strip embedded metadata"
    assert sha256_bytes(blob) != reca.sha256, "expected PDF export to re-encode the image"
    d = dhash_bytes(blob)
    assert hamming(d, reca.dhash) <= 6, "perceptual hash did not survive PDF export"
    assert hamming(d, recb.dhash) > 6, "perceptual hash matched the WRONG figure"


def test_pdf_audit_finds_tagged_and_names_the_gap(tmp_path):
    tagged, _ = _tagged(tmp_path, "C.png", seed=7)
    stranger = _figure(tmp_path / "stranger.png", seed=4242)
    pdf = _pdf_with(tmp_path, [tagged, stranger])

    rep = audit(pdf)
    assert rep["pictures"] == 2
    assert rep["tagged"] == 1, f"expected the tagged figure to be recovered: {rep['rows']}"
    assert rep["untagged_substantive"] == 1
    assert any("dhash" in r["matched_by"] for r in rep["rows"]), \
        "recovery should have come through the perceptual layer"


def test_pdf_apply_writes_credits_and_manifest(tmp_path):
    tagged, _ = _tagged(tmp_path, "D.png", seed=11)
    stranger = _figure(tmp_path / "stranger2.png", seed=8080)
    pdf = _pdf_with(tmp_path, [tagged, stranger])
    out = tmp_path / "cited.pdf"
    rep = apply(pdf, out, captions=True, credits=True, manifest_path=tmp_path / "man")

    assert rep["cited"] == 1 and rep["unsourced"] == 1
    doc = fitz.open(str(out))
    assert doc.page_count == 2, "a credits page should have been appended"
    text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
    doc.close()
    assert "Image credits" in text
    assert "Shiragaki" in text
    assert "no recorded source" in text, "the unsourced image was not named"
    assert "[1]" in text

    csv_text = open(rep["manifest"]["csv"], encoding="utf-8").read()
    assert DOI in csv_text


def test_pdf_unconfirmed_is_withheld_but_context_is_shown(tmp_path):
    """The citation is withheld; the capture context is not -- it is an observation."""
    raw = _figure(tmp_path / "raw-E.png", seed=21)
    rec = Record(source_kind="clipboard", confirmed=False, source_detail={
        "clipboard_capture": {"process": "firefox", "title": "Some Paper | Nature",
                              "captured_local": "2026-08-13T23:59:00-04:00"},
        "url": "https://example.org/article"})
    img = tmp_path / "E.png"
    rec = embed(raw, img, rec)
    store.put(rec)

    pdf = _pdf_with(tmp_path, [img])
    out = tmp_path / "e.pdf"
    apply(pdf, out, captions=True, credits=True)
    doc = fitz.open(str(out))
    text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
    doc.close()

    assert "SOURCE UNCONFIRMED" in text
    assert "firefox" in text, "the capture context should still be visible"
    assert "Some Paper" in text


def test_textbox_that_does_not_fit_is_never_silently_dropped():
    """PyMuPDF returns a negative float and draws nothing; that ate the heading."""
    from figcite.pdfdeck import _fit_textbox

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    tight = fitz.Rect(48, 48, 564, 48 + 26)   # too short for 16pt

    raw = page.insert_textbox(tight, "Image credits", fontsize=16, fontname="hebo")
    assert raw < 0, "fixture invalid: this box was supposed to be too small"

    used = _fit_textbox(page, tight, "Image credits", 16, "hebo")
    assert used > 0
    assert "Image credits" in page.get_text(), \
        "the helper reported success but nothing was drawn"

    # and it refuses loudly when there is genuinely no room, rather than vanishing
    with pytest.raises(ValueError):
        _fit_textbox(page, fitz.Rect(48, 700, 60, 703), "a much longer heading", 16)
    doc.close()


def test_credits_paginate_without_losing_entries(tmp_path):
    """Many long entries must all appear, across as many pages as it takes."""
    from figcite.pdfdeck import _credits_pages

    doc = fitz.open()
    doc.new_page(width=612, height=792)
    entries = [f"[{i}] Author{i} et al. (20{10 + i % 15}). " + "A rather long title " * 4
               + f"Journal of Testing {i}: 1-20. https://doi.org/10.9999/test.{i}"
               for i in range(1, 31)]
    _credits_pages(doc, entries, "2 image(s) on page(s) 1 have no recorded source.")
    text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
    doc.close()

    for i in (1, 15, 30):
        assert f"10.9999/test.{i}" in text, f"entry {i} was lost in pagination"
    assert "no recorded source" in text


def test_recovery_is_encoding_robust_but_geometry_fragile(tmp_path):
    """Characterises the real envelope, measured on 14 real figures.

    Robust: downsampling (to 64px wide), JPEG down to quality 10, CMYK roundtrip
    -- i.e. everything a PDF exporter does, including Affinity's most aggressive
    'PDF for web' preset at 72 DPI.

    Fragile: any GEOMETRIC change -- crop, rotate, flip -- which moves every
    cell of the difference hash. Crucially it fails SAFE: a cropped figure
    reports no match rather than matching the wrong one.
    """
    import io
    from PIL import Image
    from figcite.provenance import dhash_bytes, hamming

    a = Image.open(_figure(tmp_path / "geo-a.png", seed=3, size=(900, 600)))
    b = Image.open(_figure(tmp_path / "geo-b.png", seed=77, size=(900, 600)))

    def dh(im, q=70, fmt="JPEG"):
        buf = io.BytesIO()
        im.convert("RGB").save(buf, fmt, quality=q)
        return dhash_bytes(buf.getvalue())

    d_a, d_b = dh(a, 95, "PNG"), dh(b, 95, "PNG")
    assert hamming(d_a, d_b) > 6, "fixture figures are too similar to discriminate"

    # encoding transforms: still recovered
    tiny = a.resize((64, 43), Image.Resampling.LANCZOS)
    assert hamming(dh(tiny), d_a) <= 6, "downsample to 64px broke recovery"
    assert hamming(dh(a, q=10), d_a) <= 6, "JPEG q10 broke recovery"
    assert hamming(dh(a.convert("CMYK").convert("RGB")), d_a) <= 6, "CMYK roundtrip broke recovery"

    # geometric transforms: NOT recovered, and must not mis-attribute
    cropped = a.crop((90, 60, 810, 540))          # 10% off each edge
    d_crop = dh(cropped)
    assert hamming(d_crop, d_a) > 6, "fixture invalid: crop was supposed to break the hash"
    assert hamming(d_crop, d_b) > 6, \
        "a cropped figure matched a DIFFERENT figure -- failure must be safe, not wrong"
