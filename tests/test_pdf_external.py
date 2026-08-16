"""PDFs written by tools that are not us.

Every other PDF test in this suite builds its fixtures with PyMuPDF and then
reads them back with PyMuPDF, and the recovery "envelope" is characterised by
applying transforms in PIL. That is self-consistent by construction: it can show
the matcher agrees with itself, and it cannot show that a PDF written by a real
exporter is readable at all. This module drives the actual boundary, with
Ghostscript and ImageMagick standing in for the design apps (Affinity,
Illustrator, InDesign) that have no scriptable export.

It found a real defect on first run: Ghostscript promotes an image's soft mask
to a top-level image object, so the same document audited as 6 images under
Ghostscript and 4 under ImageMagick, inflating "substantive but unsourced" with
objects that are not figures.
"""

from __future__ import annotations

import random
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw

from figcite import store
from figcite.pdfdeck import audit
from figcite.provenance import Record, now_stamps

pytestmark = pytest.mark.live

GS = shutil.which("gs")
CONVERT = shutil.which("convert") or shutil.which("magick")

requires_tools = pytest.mark.skipif(
    not (GS and CONVERT), reason="needs ghostscript and imagemagick"
)


def _figure(path, seed, size=(900, 600)):
    """Structured, not a gradient -- a flat image has a degenerate dhash."""
    rng = random.Random(seed)
    im = Image.new("RGB", size, (252, 252, 250))
    d = ImageDraw.Draw(im)
    for _ in range(40):
        x, y = rng.randrange(size[0] - 100), rng.randrange(size[1] - 90)
        d.rectangle(
            [x, y, x + rng.randrange(25, 95), y + rng.randrange(20, 85)],
            fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)),
        )
    im.save(path)
    return path


@pytest.fixture
def deck(tmp_path):
    """Three registered figures plus one that is deliberately NOT registered."""
    u, loc = now_stamps()
    paths = []
    for i in range(3):
        p = _figure(tmp_path / f"fig{i}.png", seed=100 + i)
        store.register_existing(
            p,
            Record(
                doi=f"10.9999/ext.{i}",
                citation=f"External et al. ({2020 + i}).",
                short_cite=f"Ext{i} {2020 + i}",
                source_kind="pdf-crop",
                captured_utc=u,
                captured_local=loc,
                confirmed=True,
            ),
        )
        paths.append(p)
    # The discrimination control. Without it a matcher that matched EVERYTHING
    # would score a perfect result here.
    paths.append(_figure(tmp_path / "zz_control.png", seed=4242))
    return paths


def _imagemagick_pdf(paths, out):
    subprocess.run(
        [CONVERT, *[str(p) for p in paths], str(out)], check=True, timeout=300
    )
    return out


def _ghostscript_pdf(src, out, preset):
    subprocess.run(
        [
            GS,
            "-q",
            "-dNOPAUSE",
            "-dBATCH",
            "-sDEVICE=pdfwrite",
            f"-dPDFSETTINGS={preset}",
            f"-sOutputFile={out}",
            str(src),
        ],
        check=True,
        timeout=300,
    )
    return out


@requires_tools
@pytest.mark.parametrize("preset", ["/ebook", "/screen", "/prepress"])
def test_provenance_survives_a_real_pdf_exporter(deck, tmp_path, preset):
    """The claim the whole PDF route rests on, checked against real exporters.

    /screen is the aggressive one: it downsamples to 72 DPI and re-encodes as
    JPEG, which is roughly what a design app's "PDF for web" preset does.
    """
    src = _imagemagick_pdf(deck, tmp_path / "src.pdf")
    out = _ghostscript_pdf(src, tmp_path / f"out{preset.strip('/')}.pdf", preset)

    rep = audit(str(out))
    matched = [r for r in rep["rows"] if r["record"]]
    assert len(matched) == 3, (
        f"{preset}: recovered {len(matched)}/3 figures through a real exporter"
    )
    assert {r["record"].doi for r in matched} == {
        "10.9999/ext.0",
        "10.9999/ext.1",
        "10.9999/ext.2",
    }
    unmatched = [r for r in rep["rows"] if not r["record"]]
    assert len(unmatched) == 1, (
        "the unregistered control must NOT match -- a matcher that matched "
        "everything would pass the assertion above"
    )


@requires_tools
def test_soft_masks_are_not_counted_as_figures(deck, tmp_path):
    """The defect this pins, measured on real output.

    A figure with transparency is stored as a colour image plus a greyscale
    alpha channel. ImageMagick keeps the mask as a child of its image;
    Ghostscript promotes it to a top-level image object. Counting it as a figure
    made the SAME document audit as 6 images under one writer and 4 under the
    other, and reported phantom unsourced images -- sending you looking for the
    provenance of an object that is not a figure.
    """
    src = _imagemagick_pdf(deck, tmp_path / "src.pdf")
    gsp = _ghostscript_pdf(src, tmp_path / "gs.pdf", "/ebook")

    im_rep = audit(str(src))
    gs_rep = audit(str(gsp))
    assert im_rep["pictures"] == gs_rep["pictures"] == 4, (
        f"image count depends on which tool wrote the PDF: "
        f"ImageMagick={im_rep['pictures']} Ghostscript={gs_rep['pictures']}"
    )
    assert im_rep["untagged_substantive"] == gs_rep["untagged_substantive"] == 1


@requires_tools
def test_a_pdf_we_did_not_write_is_readable_at_all(deck, tmp_path):
    """The floor beneath every other assertion here: PyMuPDF fixtures round-trip
    through PyMuPDF by construction, so 'we can read a PDF' has never actually
    been tested against a foreign writer."""
    src = _imagemagick_pdf(deck, tmp_path / "src.pdf")
    rep = audit(str(src))
    assert rep["pictures"] == 4, "could not enumerate images in a foreign PDF"
