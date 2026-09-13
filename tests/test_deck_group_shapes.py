"""A figure inside a GROUP is still a figure.

`deck._iter_pictures` walks a slide by XML tag:

    if t == "grpSp":   yield from walk(sh.shapes, sh)     # descend
    elif t == "pic":   yield sh, container

`"grpSp"` survived `-> "MUTANT"` in the string sweep. Under that mutation the
walk never descends, and every picture inside a group becomes invisible:
absent from the audit, uncounted in the unsourced tally, never captioned by
`apply`. The deck then reports itself fully sourced while carrying
uncredited figures -- the single outcome this tool exists to prevent.

Grouping is not exotic. It is what PowerPoint does the moment someone
selects a figure and its label and presses Ctrl+G, and what most
paste-a-panel-and-annotate-it workflows produce.

The module docstring already records that `_iter_pictures` walks by tag
rather than by `shape_type` because a real 81-slide deck had 61 pictures and
0 detected. This is the same failure one level down: right predicate, but
nothing verified it reaches nested shapes.

The nested figure is deliberately given a DIFFERENT record from the
top-level one, so an assertion cannot be satisfied by finding the same
figure twice.
"""

from __future__ import annotations

import random

import pytest
from pptx import Presentation
from pptx.util import Inches

from figcite import store
from figcite.deck import audit
from figcite.provenance import Record, embed

DOI_A = "10.3390/horticulturae6040087"
DOI_B = "10.1234/nested.figure"


@pytest.fixture(autouse=True)
def _own_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MANIFEST", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)


def _figure(path, seed, size=(420, 300)):
    """Structured, so the two figures do not collide on a degenerate dhash."""
    from PIL import Image, ImageDraw

    rng = random.Random(seed)
    im = Image.new("RGB", size, (250, 250, 248))
    d = ImageDraw.Draw(im)
    for _ in range(30):
        x, y = rng.randrange(size[0] - 60), rng.randrange(size[1] - 60)
        box = [x, y, x + rng.randrange(15, 60), y + rng.randrange(15, 55)]
        col = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        if rng.random() < 0.5:
            d.rectangle(box, fill=col)
        else:
            d.ellipse(box, fill=col)
    im.save(path)
    return path


def _tagged(tmp_path, name, seed, doi):
    raw = _figure(tmp_path / f"raw-{name}", seed)
    rec = Record(
        doi=doi,
        citation=f"Citation for {doi}",
        short_cite=f"Cite {seed}",
        confirmed=True,
        source_kind="pdf-crop",
    )
    out = tmp_path / name
    rec = embed(raw, out, rec)
    store.put(rec)
    return out


def _deck_with_a_grouped_figure(tmp_path, loose_img, grouped_img):
    """One picture at slide level, one inside a group.

    python-pptx has no public group-with-picture builder, so the group is
    assembled by moving the picture's XML element under a `grpSp` -- which is
    what PowerPoint itself writes for Ctrl+G.
    """
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    slide.shapes.add_picture(str(loose_img), Inches(0.5), Inches(0.5), width=Inches(3))

    inner = slide.shapes.add_picture(str(grouped_img), Inches(0.5), Inches(3.6), width=Inches(3))
    group = slide.shapes.add_group_shape()
    group._element.append(inner._element)

    p = tmp_path / "grouped.pptx"
    prs.save(str(p))
    return p


def test_a_picture_inside_a_group_is_still_audited(tmp_path):
    """THE finding: without the descent, the nested figure does not exist."""
    loose = _tagged(tmp_path, "loose.png", seed=11, doi=DOI_A)
    nested = _tagged(tmp_path, "nested.png", seed=71, doi=DOI_B)
    deck = _deck_with_a_grouped_figure(tmp_path, loose, nested)

    rep = audit(deck, min_inches=1.0)

    assert rep["pictures"] == 2, (
        f"a figure inside a group was not seen at all: {rep['pictures']} picture(s)"
    )
    dois = {r["record"].doi for r in rep["rows"] if r["record"] is not None}
    assert DOI_B in dois, f"the grouped figure is missing from the audit; only found {dois}"


def test_an_uncredited_figure_inside_a_group_still_counts_as_unsourced(tmp_path):
    """The consequence that matters.

    An invisible figure is not merely missing from a listing -- it drops out
    of `untagged_substantive`, so a deck carrying an uncredited grouped
    figure reports itself clean.
    """
    loose = _tagged(tmp_path, "loose2.png", seed=13, doi=DOI_A)
    bare = _figure(tmp_path / "bare.png", seed=97)
    deck = _deck_with_a_grouped_figure(tmp_path, loose, bare)

    rep = audit(deck, min_inches=1.0)

    assert rep["pictures"] == 2, rep
    assert rep["untagged_substantive"] == 1, (
        f"an uncredited figure inside a group vanished from the tally: {rep}"
    )


def test_a_slide_level_picture_is_still_found(tmp_path):
    """Positive control.

    "Descend into everything and yield everything" would satisfy both tests
    above; so would a walk that only ever looked inside groups. This pins the
    ordinary case that the whole tool rests on.
    """
    loose = _tagged(tmp_path, "only.png", seed=17, doi=DOI_A)
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture(str(loose), Inches(0.5), Inches(0.5), width=Inches(3))
    plain = tmp_path / "plain.pptx"
    prs.save(str(plain))

    rep = audit(plain, min_inches=1.0)

    assert rep["pictures"] == 1, rep
    assert rep["tagged"] == 1, rep
