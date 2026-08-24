"""deck.py guards: shape identity, layout choice, and credits chunking.

The pptx backend's counterparts to the pdfdeck rules, plus three of its own.
`_cnvpr` decides whether a shape can carry alt text at all; `_blank_layout`
decides what the credits slide looks like; the chunking rules decide whether
a long credits list is labelled and whether the "no recorded source" warning
appears once or on every slide.
"""

from __future__ import annotations

import random

import pytest
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.util import Inches

from figcite import deck, store
from figcite.provenance import Record, embed

CITE = "Shiragaki et al. (2020). Horticulturae 6: 87."
DOI = "10.3390/horticulturae6040087"


@pytest.fixture(autouse=True)
def _own_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MANIFEST", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)


def _image(path, seed, size=(400, 300)):
    rng = random.Random(seed)
    im = Image.new("RGB", size, (250, 250, 248))
    d = ImageDraw.Draw(im)
    for _ in range(24):
        x0, y0 = rng.randrange(size[0] - 60), rng.randrange(size[1] - 60)
        w, h = rng.randrange(20, 80), rng.randrange(15, 60)
        col = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        (d.rectangle if rng.random() < 0.5 else d.ellipse)(
            [x0, y0, x0 + w, y0 + h], fill=col
        )
    im.save(path)
    return path


# ------------------------------------------------- match_image / manifest


def test_a_record_with_only_a_citation_counts_as_embedded_metadata(tmp_path):
    """`if rec is not None and (rec.doi or rec.citation)` survived `Or -> And`
    -- the same rule as pdfdeck's, in the other backend.

    Under `and` a figure carrying a citation but no DOI (a book plate, a
    museum image, anything not in CrossRef) is reported as having no source
    at all.
    """
    for name, kw in (
        ("doi-only.png", dict(doi=DOI, citation="")),
        ("cite-only.png", dict(doi=None, citation=CITE)),
    ):
        raw = _image(tmp_path / f"raw-{name}", hash(name) % 997)
        rec = Record(short_cite="S 2020", confirmed=True, source_kind="pdf-crop", **kw)
        rec = embed(raw, tmp_path / name, rec)

        class _Shape:
            class image:
                blob = (tmp_path / name).read_bytes()

        got, how = deck.match_picture(_Shape(), {})

        assert got is not None and how == "embedded-metadata", (name, how)


def test_an_explicit_manifest_is_used_instead_of_the_global_one(tmp_path):
    """`manifest = store.all_records() if manifest is None else manifest`
    survived `Is -> IsNot`.

    Inverted, a caller-supplied manifest is DISCARDED and the global store is
    read instead -- and a caller passing `None` gets `None` back and dies on
    `sha in manifest`. `audit` passes its own manifest so a whole deck is
    checked against one consistent snapshot; falling back to the live store
    per image would let the answer change halfway through a report.
    """
    raw = _image(tmp_path / "raw-m.png", 5)
    rec = Record(
        doi=DOI,
        citation=CITE,
        short_cite="S 2020",
        confirmed=True,
        source_kind="pdf-crop",
    )
    out = tmp_path / "m.png"
    rec = embed(raw, out, rec)
    # NOT in the global store, and stripped of embedded metadata so only the
    # supplied manifest can identify it.
    plain = tmp_path / "plain.png"
    Image.open(raw).save(plain)
    from figcite.provenance import sha256_bytes

    supplied = {sha256_bytes(plain.read_bytes()): rec}

    class _Shape:
        class image:
            blob = plain.read_bytes()

    got, how = deck.match_picture(_Shape(), supplied)

    assert how == "manifest-sha256", how
    assert got.doi == DOI, got


# ------------------------------------------------- the credits layout


def test_the_blank_layout_is_chosen_for_the_credits_slide():
    """`if "blank" in (lay.name or "").lower()` survived `In -> NotIn`,
    `Or -> And` and mutating the literal.

    A credits slide wants no title placeholder, no bullet frame -- just the
    text figcite puts there. Under `not in` the FIRST non-blank layout is
    picked, which in the default template is "Title Slide": the credits then
    render inside a title placeholder, centred and enormous.
    """
    prs = Presentation()
    lay = deck._blank_layout(prs)

    assert "blank" in (lay.name or "").lower(), lay.name


def test_a_template_with_no_blank_layout_falls_back_to_the_emptiest(monkeypatch):
    """The `or ""` half and the fallback. A layout whose name is None must not
    raise, and a template with no layout called "Blank" still needs the one
    with the fewest placeholders."""

    class _Lay:
        def __init__(self, name, n):
            self.name = name
            self.placeholders = list(range(n))

    class _Prs:
        slide_layouts = [_Lay("Title Slide", 2), _Lay(None, 0), _Lay("Content", 5)]

    got = deck._blank_layout(_Prs())

    assert got.name is None and len(got.placeholders) == 0, got.name


# ------------------------------------------------- credits chunking


def _long_entries(n):
    return [
        f"[{i + 1}] Author {i} et al. ({2000 + i}). A long enough title that the "
        f"credits list has to be split across more than one slide, Journal {i}."
        for i in range(n)
    ]


def test_a_single_credits_slide_is_not_numbered(tmp_path):
    """`f" ({ci + 1}/{len(chunks)})" if len(chunks) > 1 else ""` survived
    `Gt -> GtE` and `1 -> 2`.

    Under `>=` one credits slide is labelled "Image credits (1/1)", which
    tells a reader to look for more. Under `> 2` a two-slide list is not
    numbered at all, so the second slide looks like a duplicate of the first.
    """
    raw = _image(tmp_path / "raw-s.png", 9)
    rec = Record(
        doi=DOI,
        citation=CITE,
        short_cite="S 2020",
        confirmed=True,
        source_kind="pdf-crop",
    )
    tagged = tmp_path / "s.png"
    rec = embed(raw, tagged, rec)
    store.put(rec)

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture(
        str(tagged), Inches(0.5), Inches(0.5), width=Inches(3), height=Inches(2.2)
    )
    src = tmp_path / "in.pptx"
    prs.save(str(src))

    deck.apply(str(src), str(tmp_path / "out.pptx"))

    text = _all_text(tmp_path / "out.pptx")
    assert "Image credits" in text, text[:300]
    assert "(1/1)" not in text, (
        f"a single credits slide was numbered as one of several: {text[:300]!r}"
    )


def _all_text(path):
    prs = Presentation(str(path))
    out = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
    return "\n".join(out)


def test_the_missing_source_warning_appears_once_on_the_last_slide(monkeypatch):
    """`if ci == len(chunks) - 1 and missing_note` survived `And -> Or`.

    Under `or` the note is emitted on EVERY chunk -- so a two-slide credits
    list carries the warning twice and a reader counts the unsourced figures
    twice over. The note is a tally ("3 image(s) ... have no recorded
    source"), so repeating it is not merely noisy, it misstates the number.

    Chunking is forced small so more than one chunk exists at all; with a
    single chunk `ci == len(chunks) - 1` is always true and `or` cannot be
    observed.
    """
    monkeypatch.setattr(deck, "CREDITS_PER_SLIDE", 2)

    prs = Presentation()
    chunks_seen = deck._add_credits_slides(
        prs, _long_entries(4), "! 3 image(s) have no recorded source"
    )
    assert chunks_seen >= 2, f"expected more than one credits slide, got {chunks_seen}"

    text = _all_text_prs(prs)
    assert text.count("no recorded source") == 1, (
        f"the missing-source tally appeared {text.count('no recorded source')} "
        f"times across {chunks_seen} credits slides"
    )


def _all_text_prs(prs):
    out = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
    return "\n".join(out)


# ------------------------------------------------- caption confirmation gate


def test_an_unconfirmed_record_gets_no_citation_caption(tmp_path):
    """`if rec.confirmed or allow_unconfirmed` survived `Or -> And`.

    Under `and` a CONFIRMED record only gets its caption when the caller also
    passes `allow_unconfirmed` -- so the default run, which is the one people
    make, silently drops every citation caption from the deck while still
    reporting the figures as tagged.
    """
    raw = _image(tmp_path / "raw-u.png", 11)
    rec = Record(
        doi=DOI,
        citation=CITE,
        short_cite="Shiragaki et al. 2020",
        confirmed=True,
        source_kind="pdf-crop",
    )
    tagged = tmp_path / "u.png"
    rec = embed(raw, tagged, rec)
    store.put(rec)

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture(
        str(tagged), Inches(0.5), Inches(0.5), width=Inches(3), height=Inches(2.2)
    )
    src = tmp_path / "conf.pptx"
    prs.save(str(src))

    deck.apply(str(src), str(tmp_path / "conf-out.pptx"))

    text = _all_text(tmp_path / "conf-out.pptx")
    assert "Shiragaki et al. 2020" in text, (
        f"a confirmed record got no caption on a default run: {text[:300]!r}"
    )


def test_a_layout_actually_named_blank_beats_one_that_merely_has_no_placeholders(
    monkeypatch,
):
    """`if "blank" in (lay.name or "").lower()` survived mutating the literal.

    In python-pptx's default template the Blank layout is ALSO the one with
    the fewest placeholders, so the name lookup and the fallback return the
    same layout and the literal cannot be observed -- which is why my first
    test of this passed against the mutant. A template where they disagree
    separates them, and real corporate templates disagree all the time.
    """

    class _Lay:
        def __init__(self, name, n):
            self.name = name
            self.placeholders = list(range(n))

    class _Prs:
        slide_layouts = [_Lay("Title Slide", 3), _Lay("Blank", 2), _Lay("Picture", 0)]

    got = deck._blank_layout(_Prs())

    assert got.name == "Blank", (
        f"the layout named Blank was passed over for {got.name!r}"
    )


def test_a_shape_whose_element_has_no_cNvPr_is_skipped_not_dereferenced():
    """`if nv is not None and getattr(nv, "cNvPr", None) is not None` survived
    `And -> Or`.

    Under `or` the second operand alone is enough, so an element that HAS one
    of the four container attributes but no `cNvPr` inside it reaches
    `return nv.cNvPr` and raises AttributeError -- out of the shape walk, so
    a single unusual shape aborts the audit of the whole deck rather than
    being skipped.
    """

    class _NoCnv:
        pass

    class _El:
        nvPicPr = _NoCnv()

    class _Shape:
        _element = _El()

    assert deck._cnvpr(_Shape()) is None


def test_several_credits_slides_are_numbered_one_of_n(monkeypatch):
    """`f" ({ci + 1}/{len(chunks)})" if len(chunks) > 1 else ""` survived
    `1 -> 2` at both of its literals.

    Under `ci + 2` the slides are labelled "(2/2)" and "(3/2)" -- the second
    of which describes a slide that cannot exist. Under `len(chunks) > 2` a
    two-slide list is not numbered at all, so the second slide reads as a
    duplicate of the first.

    My single-slide test could observe neither: with one chunk the whole
    expression is the empty string.
    """
    monkeypatch.setattr(deck, "CREDITS_PER_SLIDE", 2)
    prs = Presentation()

    made = deck._add_credits_slides(prs, _long_entries(4), "")

    assert made == 2, made
    text = _all_text_prs(prs)
    assert "(1/2)" in text and "(2/2)" in text, text[:400]
    assert "(3/2)" not in text, text[:400]


def test_a_credit_used_by_several_pictures_says_how_many(tmp_path):
    """`entry_counts.get(i + 1, 1) > 1` survived `Gt -> GtE` -- deck's copy of
    the rule pdfdeck also carries.

    Asymmetric by construction: one credit covering two pictures and one
    covering one. Under `>=` the single-picture credit gains a pointless
    "(1 figures)"; under `> 2` the shared one loses its count.
    """
    shared = dict(doi=DOI, citation=CITE, short_cite="S 2020")
    other = dict(
        doi="10.1111/nph.71477",
        citation="Other et al. (2021).",
        short_cite="Other 2021",
    )

    paths = []
    for i, kw in enumerate((shared, shared, other)):
        raw = _image(tmp_path / f"raw-c{i}.png", 200 + i)
        rec = Record(confirmed=True, source_kind="pdf-crop", **kw)
        out = tmp_path / f"c{i}.png"
        rec = embed(raw, out, rec)
        store.put(rec)
        paths.append(out)

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    top = 0.3
    for p in paths:
        slide.shapes.add_picture(
            str(p), Inches(0.4), Inches(top), width=Inches(3), height=Inches(2.0)
        )
        top += 2.2
    src = tmp_path / "counts.pptx"
    prs.save(str(src))

    deck.apply(str(src), str(tmp_path / "counts-out.pptx"))

    text = _all_text(tmp_path / "counts-out.pptx")
    assert "(2 figures)" in text, (
        f"a credit standing for two pictures did not say so: {text[-500:]!r}"
    )
    assert "(1 figures)" not in text, text[-500:]


def test_the_pptx_manifest_json_carries_the_record_as_data(tmp_path):
    """`{k: v for k, v in r.items() if k != "record"}` survived `NotEq -> Eq`
    -- deck's copy of pdfdeck's manifest rule, and no test had ever asked
    `apply` for a manifest at all.

    Under `==` every field except the record is dropped, so the manifest
    loses the slide numbers and the shape names -- the only thing that says
    WHERE in the deck each credit belongs.
    """
    import json

    raw = _image(tmp_path / "raw-mf.png", 31)
    rec = Record(
        doi=DOI,
        citation=CITE,
        short_cite="S 2020",
        confirmed=True,
        source_kind="pdf-crop",
    )
    tagged = tmp_path / "mf.png"
    rec = embed(raw, tagged, rec)
    store.put(rec)

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture(
        str(tagged), Inches(0.5), Inches(0.5), width=Inches(3), height=Inches(2.2)
    )
    src = tmp_path / "mf.pptx"
    prs.save(str(src))

    stem = str(tmp_path / "mf-out")
    deck.apply(str(src), stem + ".pptx", manifest_path=stem + ".pptx")

    rows = json.loads(open(stem + ".json", encoding="utf-8").read())
    assert rows and rows[0]["record"]["doi"] == DOI, rows
    assert "slide" in rows[0], (
        f"the non-record fields were dropped from the manifest: {sorted(rows[0])}"
    )
