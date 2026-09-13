"""pdfdeck's typography and page-flow guards, and its image extractor.

`pdfdeck` writes the credits page nobody reads until a talk is over, and its
layout is all thresholds: step the font down until the text fits, start a new
page when the next line would cross the bottom, wrap a word when it would
cross the right edge. Eighteen survivors sat in here and most are one-off
boundaries -- the kind that lose the last line of a credits page, silently,
in the artefact you hand to a conference.

The module's own docstring records the failure that motivated `_fit_textbox`:
`insert_textbox` returns a negative number instead of raising when text does
not fit, and an unchecked return dropped the credits heading entirely.
"""

from __future__ import annotations

import json
import random

import fitz
import pytest
from PIL import Image, ImageDraw

from figcite import pdfdeck, store
from figcite.provenance import Record, embed


CITE = "Shiragaki et al. (2020). Horticulturae 6: 87."
DOI = "10.3390/horticulturae6040087"


def _figure(path, seed, size=(520, 360)):
    rng = random.Random(seed)
    im = Image.new("RGB", size, (250, 250, 248))
    d = ImageDraw.Draw(im)
    for _ in range(28):
        x0, y0 = rng.randrange(size[0] - 60), rng.randrange(size[1] - 60)
        w, h = rng.randrange(20, 90), rng.randrange(15, 70)
        col = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        (d.rectangle if rng.random() < 0.5 else d.ellipse)([x0, y0, x0 + w, y0 + h], fill=col)
    im.save(path)
    return path


@pytest.fixture(autouse=True)
def _own_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MANIFEST", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)


def _blank_page():
    doc = fitz.open()
    return doc, doc.new_page(width=612, height=792)


# ------------------------------------------------- _fit_textbox


def test_text_is_still_tried_at_exactly_the_minimum_font_size():
    """`while fs >= min_fontsize` survived `GtE -> Gt`.

    The minimum is the smallest size still ACCEPTABLE, not the first size
    refused. Under `>` the loop exits one step early and raises for text that
    would have fitted at the floor -- so a long citation meant to shrink to
    the floor aborts the whole credits page instead.

    The return value is supplied so the floor is the ONLY size that fits. My
    first version of this test just picked a small box and a low floor; the
    text fitted somewhere above the floor, that step was never the deciding
    one, and the mutant lived.
    """
    doc, page = _blank_page()
    try:
        tried = []

        def fake_insert(rect, text, fontsize=None, fontname=None, color=None):
            tried.append(fontsize)
            return 0.0 if fontsize == 8.0 else -1.0  # fits ONLY at the floor

        page.insert_textbox = fake_insert
        used = pdfdeck._fit_textbox(
            page,
            fitz.Rect(40, 40, 300, 80),
            "text",
            9.0,
            "helv",
            (0, 0, 0),
            min_fontsize=8.0,
        )

        assert used == 40.0, used
        assert tried[-1] == 8.0, f"the floor size was never tried: {tried}"
    finally:
        doc.close()


def test_text_that_fits_at_no_size_raises_rather_than_vanishing():
    """The positive control, and the failure the function exists for:
    `insert_textbox` returns a negative number instead of raising, and an
    unchecked return silently dropped the credits heading."""
    doc, page = _blank_page()
    try:
        page.insert_textbox = lambda *a, **kw: -1.0
        with pytest.raises(ValueError, match="does not fit"):
            pdfdeck._fit_textbox(
                page,
                fitz.Rect(40, 40, 300, 80),
                "text",
                9.0,
                "helv",
                (0, 0, 0),
                min_fontsize=8.0,
            )
    finally:
        doc.close()


def test_text_that_fits_exactly_is_accepted():
    """`if rc >= 0` survived `GtE -> Gt` and `0 -> 1`.

    `insert_textbox` returns the vertical space LEFT OVER, so 0 means "fitted
    with nothing to spare" -- a fit, not a failure. Both mutants reject it and
    step the font down needlessly, or run out of ladder and raise on text that
    fitted.

    Rather than hunt for a box where the leftover is exactly zero, the return
    value is supplied: the guard is what is under test, not fitz's arithmetic.
    """
    doc, page = _blank_page()
    try:
        calls = []

        def fake_insert(rect, text, fontsize=None, fontname=None, color=None):
            calls.append(fontsize)
            return 0.0  # fits exactly, nothing left over

        page.insert_textbox = fake_insert
        used = pdfdeck._fit_textbox(
            page, fitz.Rect(40, 40, 300, 80), "text", 10.0, "helv", (0, 0, 0)
        )

        assert used == 40.0, used
        assert calls == [10.0], f"an exact fit was rejected and the font stepped down: {calls}"
    finally:
        doc.close()


def test_a_negative_return_steps_the_font_down_rather_than_vanishing():
    """The positive control, and the bug the function exists for: a negative
    return means the text did NOT fit, and must not be treated as success."""
    doc, page = _blank_page()
    try:
        calls = []

        def fake_insert(rect, text, fontsize=None, fontname=None, color=None):
            calls.append(fontsize)
            return 1.0 if len(calls) > 2 else -1.0

        page.insert_textbox = fake_insert
        pdfdeck._fit_textbox(page, fitz.Rect(40, 40, 300, 80), "text", 10.0, "helv", (0, 0, 0))

        assert calls == [10.0, 9.5, 9.0], calls
    finally:
        doc.close()


# ------------------------------------------------- word wrap and page flow


def test_a_word_that_exactly_fills_the_line_does_not_wrap():
    """`if cur + w > width` survived `Gt -> GtE`.

    At exactly the available width the word fits. Under `>=` it wraps, and
    `_measure` over-counts the lines a credit needs -- which makes the caller
    start a new page early and, on a long credits list, adds a page of
    whitespace to the handout.
    """

    # _measure returns a HEIGHT (lines * fontsize * 1.35 + 4), so the line
    # count is read back through that formula rather than asserted directly.
    def lines(text, width):
        return round((pdfdeck._measure(text, width, 8.0) - 4) / (8.0 * 1.35))

    font = fitz.Font(fontname="helv")
    one_word = font.text_length("word ", fontsize=8.0)

    assert lines("word", one_word) == 1, "a word occupying exactly the available width was wrapped"
    assert lines("word", one_word - 0.01) == 2, "a word wider than the line did not wrap"


def test_the_first_credits_page_is_not_labelled_a_continuation(tmp_path):
    """`f" (cont. {n})" if n > 1 else ""` survived `Gt -> GtE` and `1 -> 2`.

    Under `>=` the first credits page is titled "Image credits (cont. 1)",
    which tells a reader there is an earlier page of credits that does not
    exist. Under `n > 2` the second page is not marked as a continuation at
    all, so two pages of credits look like two independent lists.
    """
    img = _figure(tmp_path / "raw.png", 3)
    rec = Record(
        doi=DOI,
        citation=CITE,
        short_cite="S 2020",
        confirmed=True,
        source_kind="pdf-crop",
    )
    tagged = tmp_path / "fig.png"
    rec = embed(img, tagged, rec)
    store.put(rec)

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(60, 60, 400, 290), filename=str(tagged))
    src = tmp_path / "in.pdf"
    doc.save(str(src))
    doc.close()

    out = pdfdeck.apply(str(src), str(tmp_path / "out.pdf"))
    assert out

    with fitz.open(str(tmp_path / "out.pdf")) as done:
        text = "\n".join(p.get_text() for p in done)

    assert "Image credits" in text, text[:400]
    assert "cont." not in text, f"a single credits page was labelled a continuation: {text[:400]!r}"


# ------------------------------------------------- the shared audit rules


def test_a_record_with_only_a_doi_counts_as_embedded_metadata(tmp_path):
    """`if rec is not None and (rec.doi or rec.citation)` survived `Or -> And`.

    A record carries a DOI, a citation, or both -- and the two arrive from
    different routes, so a great many carry exactly one. Under `and` those
    are treated as if they had no provenance at all, and a correctly tagged
    figure is reported as unsourced.

    Driven from each side alone: a fixture with both set satisfies `and` too.
    """
    for name, kw in (
        ("doi-only.png", dict(doi=DOI, citation="")),
        ("cite-only.png", dict(doi=None, citation=CITE)),
    ):
        raw = _figure(tmp_path / f"raw-{name}", hash(name) % 999)
        rec = Record(short_cite="S 2020", confirmed=True, source_kind="pdf-crop", **kw)
        rec = embed(raw, tmp_path / name, rec)

        got, how = pdfdeck.match_image((tmp_path / name).read_bytes(), {})

        assert got is not None, f"{name}: a tagged figure read as untagged"
        assert how == "embedded-metadata", (name, how)


def test_a_record_with_neither_a_doi_nor_a_citation_is_not_evidence(tmp_path):
    """Positive control: "accept any record" passes the test above, and a
    context-only record -- which exists precisely to NOT be a citation --
    would be reported as provenance."""
    raw = _figure(tmp_path / "raw-bare.png", 42)
    rec = Record(
        doi=None,
        citation="",
        short_cite="",
        confirmed=False,
        source_kind="clipboard",
        note="a window title, nothing more",
    )
    rec = embed(raw, tmp_path / "bare.png", rec)

    got, how = pdfdeck.match_image((tmp_path / "bare.png").read_bytes(), {})

    assert how != "embedded-metadata", (got, how)


def test_the_manifest_json_carries_the_record_as_data_not_as_an_object(tmp_path):
    """`{k: v for k, v in r.items() if k != "record"}` survived `NotEq -> Eq`
    and mutating the key.

    The row dict holds a `Record` OBJECT, which `json.dump` cannot serialise;
    the comprehension strips it and the line below re-adds it via `asdict`.
    Under `==` every other field is dropped instead and the manifest loses the
    page numbers; under a mutated key the raw object stays in and writing the
    manifest raises partway through `apply`, after the PDF has been written.
    """
    img = _figure(tmp_path / "raw2.png", 7)
    rec = Record(
        doi=DOI,
        citation=CITE,
        short_cite="S 2020",
        confirmed=True,
        source_kind="pdf-crop",
    )
    tagged = tmp_path / "fig2.png"
    rec = embed(img, tagged, rec)
    store.put(rec)

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(60, 60, 400, 290), filename=str(tagged))
    src = tmp_path / "in2.pdf"
    doc.save(str(src))
    doc.close()

    stem = str(tmp_path / "out2")
    # The manifest is written only when a path is asked for -- `apply` returns
    # None for it otherwise, which is why my first version of this test found
    # no file at all.
    pdfdeck.apply(str(src), stem + ".pdf", manifest_path=stem + ".pdf")

    rows = json.loads(open(stem + ".json", encoding="utf-8").read())
    assert rows, rows
    assert rows[0]["record"]["doi"] == DOI, rows[0]
    assert "page" in rows[0], (
        f"the non-record fields were dropped from the manifest: {sorted(rows[0])}"
    )


# ------------------------------------------------- the promoted-mask case


class _FakePage:
    def __init__(self, infos):
        self._infos = infos

    def get_images(self, full=True):
        return self._infos

    def get_image_rects(self, xref):
        return []


class _FakeDoc:
    """A document whose image table is stated rather than produced.

    The case this guards against cannot be built with PyMuPDF: the module's
    own docstring records that GHOSTSCRIPT promotes a soft mask to a
    top-level image while ImageMagick keeps it as a child, and fitz writes
    the child form. Supplying the table is the only way to exercise the
    promoted form without checking a Ghostscript-produced PDF into the repo.
    """

    def __init__(self, infos):
        self._page = _FakePage(infos)
        self.page_count = 1

    def __getitem__(self, i):
        return self._page

    def extract_image(self, xref):
        return {"image": b"\x89PNG\r\n\x1a\n" + bytes([xref]) * 8}


def test_a_soft_mask_promoted_to_a_top_level_image_is_not_a_figure():
    """`masks = {info[1] ...}` survived `1 -> 2`, and `xref in seen or xref in
    masks` survived `Or -> And`.

    Field 1 of `get_images(full=True)` is the image's own soft-mask xref.
    Under `info[2]` that becomes the WIDTH, so the mask set is a set of
    pixel counts and no mask is ever recognised; under `and` the mask has to
    be a repeat as well, which it is not.

    Either way the alpha channel is counted as a figure -- inflating
    "substantive but unsourced" with an object that is not a figure and can
    never have provenance, sending the user looking for a source that does
    not exist.
    """
    infos = [
        (5, 6, 120, 90, 8, "DeviceRGB", "", "Im0", "", 0),  # figure, masked by 6
        (6, 0, 120, 90, 8, "DeviceGray", "", "Im1", "", 0),  # the promoted mask
    ]

    got = list(pdfdeck.iter_pdf_images(_FakeDoc(infos)))

    assert [g["xref"] for g in got] == [5], (
        f"the soft mask was counted as a figure: {[g['xref'] for g in got]}"
    )


def test_the_same_image_placed_twice_is_extracted_once():
    """The `seen` half of the same `or`, alone. A figure placed twice on a
    page has one xref and two rects; extracting it twice would credit it
    twice."""
    infos = [
        (7, 0, 120, 90, 8, "DeviceRGB", "", "Im0", "", 0),
        (7, 0, 120, 90, 8, "DeviceRGB", "", "Im0", "", 0),
    ]

    got = list(pdfdeck.iter_pdf_images(_FakeDoc(infos)))

    assert [g["xref"] for g in got] == [7], [g["xref"] for g in got]


# ------------------------------------------------- credits page flow


def test_an_entry_landing_exactly_on_the_bottom_margin_does_not_start_a_page(
    monkeypatch,
):
    """`if y + needed > bottom` survived `Gt -> GtE`, at both of its sites.

    The bottom margin is the last y an entry may OCCUPY, not the first y that
    overflows. Under `>=` an entry that exactly fills the remaining space
    starts a new page anyway, and a credits list sized to one page becomes
    two -- the second holding a single line.

    `_measure` and `_fit_textbox` are both supplied so `y` is exactly
    predictable; the arithmetic of the flow is what is under test, and real
    glyph metrics cannot be steered onto a boundary on request.
    """
    monkeypatch.setattr(pdfdeck, "_fit_textbox", lambda *a, **kw: 20.0)

    doc = fitz.open()
    doc.new_page(width=612, height=792)
    try:
        m, bottom = 48, 792 - 48 - 40
        y_after_title = m + 20.0 + 10  # margin + title height + gap
        exact = bottom - y_after_title

        monkeypatch.setattr(pdfdeck, "_measure", lambda *a, **kw: exact)
        assert pdfdeck._credits_pages(doc, ["one entry"], "") == 1, (
            "an entry ending exactly on the bottom margin started a new page"
        )

        monkeypatch.setattr(pdfdeck, "_measure", lambda *a, **kw: exact + 0.01)
        assert pdfdeck._credits_pages(doc, ["one entry"], "") == 2, (
            "an entry past the bottom margin did not start a new page"
        )
    finally:
        doc.close()


def test_the_missing_note_obeys_the_same_boundary(monkeypatch):
    """The second copy of that guard, which handles the red "no recorded
    source" note. It is the LAST thing on the page and the most important
    line on it, so silently pushing it onto a page of its own -- or off the
    bottom -- is the failure that matters."""
    monkeypatch.setattr(pdfdeck, "_fit_textbox", lambda *a, **kw: 20.0)

    doc = fitz.open()
    doc.new_page(width=612, height=792)
    try:
        m, bottom = 48, 792 - 48 - 40
        exact = bottom - (m + 20.0 + 10)

        monkeypatch.setattr(pdfdeck, "_measure", lambda *a, **kw: exact)
        assert pdfdeck._credits_pages(doc, [], "2 images have no source") == 1

        monkeypatch.setattr(pdfdeck, "_measure", lambda *a, **kw: exact + 0.01)
        assert pdfdeck._credits_pages(doc, [], "2 images have no source") == 2
    finally:
        doc.close()


def test_a_second_credits_page_is_labelled_a_continuation():
    """`f" (cont. {n})" if n > 1 else ""` survived `1 -> 2` and mutating the
    literal.

    Real measurement, real text: enough long entries to overflow. Under the
    mutants the second page is titled plain "Image credits" and two pages of
    credits read as two independent lists -- so a reader who finds the second
    page assumes the first does not exist.
    """
    doc = fitz.open()
    doc.new_page(width=612, height=792)
    try:
        entries = [
            f"{i + 1}. Author {i} et al. ({2000 + i}). A rather long title that "
            f"wraps across more than one line in the credits column, Journal of "
            f"Things {i}: {i * 7}. https://doi.org/10.1234/example.{i}"
            for i in range(40)
        ]

        pages = pdfdeck._credits_pages(doc, entries, "")
        assert pages >= 2, f"40 long entries fitted on {pages} page(s)"

        titles = [doc[i].get_text()[:60] for i in range(doc.page_count) if i]
        joined = "\n".join(titles)
        assert "cont. 2" in joined, joined
    finally:
        doc.close()


# ------------------------------------------------- the repeated-figure count


def test_a_credit_used_by_several_figures_says_how_many(tmp_path):
    """`entry_counts.get(i + 1, 1) > 1` survived four mutants.

    Credits are deduped by what the line will SAY, so one credit can stand
    for several figures and the count is how a reader knows. Under `i + 2`
    the count is read from the NEXT credit's tally and attached to the wrong
    line; under `> 2` a credit standing for exactly two figures loses its
    count; under `>= 1` every single-figure credit gains a pointless
    "(1 figures)".

    Asymmetric by construction: one credit covering two figures and one
    covering one, so no mutant can produce the same output by accident.
    """
    shared = Record(
        doi=DOI,
        citation=CITE,
        short_cite="S 2020",
        confirmed=True,
        source_kind="pdf-crop",
    )
    other = Record(
        doi="10.1111/nph.71477",
        citation="Other et al. (2021).",
        short_cite="Other 2021",
        confirmed=True,
        source_kind="pdf-crop",
    )

    paths = []
    for i, rec in enumerate((shared, shared, other)):
        raw = _figure(tmp_path / f"raw{i}.png", 100 + i)
        out = tmp_path / f"fig{i}.png"
        filed = embed(raw, out, Record(**{**rec.__dict__, "sha256": "", "dhash": ""}))
        store.put(filed)
        paths.append(out)

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    y = 40
    for p in paths:
        page.insert_image(fitz.Rect(60, y, 400, y + 200), filename=str(p))
        y += 210
    src = tmp_path / "multi.pdf"
    doc.save(str(src))
    doc.close()

    pdfdeck.apply(str(src), str(tmp_path / "multi-out.pdf"))

    with fitz.open(str(tmp_path / "multi-out.pdf")) as done:
        text = "\n".join(p.get_text() for p in done)

    assert "(2 figures)" in text, (
        f"a credit standing for two figures did not say so: {text[-600:]!r}"
    )
    assert "(1 figures)" not in text, (
        f"a credit standing for one figure was annotated with a count: {text[-600:]!r}"
    )


def test_a_caption_that_exactly_fits_below_the_image_stays_below_it():
    """`if y0 + h > page.rect.y1` survived `Gt -> GtE`.

    A caption goes under its figure; only when there is no room does it move
    up inside the figure's lower edge, which overlaps the artwork. At exactly
    the page edge there IS room -- the caption's last pixel is the page's last
    pixel. Under `>=` it is pushed over the figure for no reason, and the
    bottom-most figure on every page gets its caption printed across it.
    """
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    try:
        h = 2 * pdfdeck.CAPTION_PT + 6
        rect = fitz.Rect(60, 400, 400, page.rect.y1 - h - 1)
        pdfdeck._caption(page, rect, "Figure 1. Shiragaki et al. 2020")

        words = page.get_text("words")
        assert words, "no caption was drawn at all"
        top_of_caption = min(w[1] for w in words)

        assert top_of_caption > rect.y1, (
            f"the caption was drawn over the figure (caption top {top_of_caption:.1f} "
            f"vs image bottom {rect.y1:.1f}) even though it fitted below it"
        )
    finally:
        doc.close()


def test_a_caption_with_no_room_below_is_moved_up_rather_than_dropped():
    """Positive control: "always place below" passes the test above and puts
    the caption off the bottom of the page, where it is not rendered at all."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    try:
        rect = fitz.Rect(60, 600, 400, page.rect.y1 - 2)
        pdfdeck._caption(page, rect, "Figure 1. Shiragaki et al. 2020")

        words = page.get_text("words")
        assert words, "the caption was dropped entirely"
        assert min(w[1] for w in words) < rect.y1, (
            "the caption was placed below an image that reaches the page edge"
        )
    finally:
        doc.close()


def test_a_zero_width_image_does_not_disable_the_mask_filter(tmp_path):
    """The equivalence proof I got WRONG, and the case that falsifies it.

    `masks = {info[1] for info in infos if info[1]}` -- the FILTER's `info[1]`
    mutated to `info[2]` (the width). I argued this was equivalent by
    construction: the filter only exists to drop the sentinel 0, and adding 0
    to the mask set is harmless because PDF object 0 is the free-list head, so
    no image can have xref 0.

    That reasoning is sound and INCOMPLETE. It only covers the direction where
    the mutant ADDS 0 to the set. The mutant also REMOVES entries: any image
    whose width is 0 stops contributing its soft-mask xref, so a real mask is
    no longer recognised. `/Width 0` is degenerate but it parses, and
    `get_images(full=True)` reports it faithfully.

    The falsifying document needs both halves at once -- a zero-width image
    carrying an SMask, AND that mask promoted to a top-level XObject (the
    Ghostscript shape this whole function exists for). The zero-width image
    itself then fails extraction and is skipped either way, so the observable
    difference is the MASK: correct code recognises xref 5 as a mask and
    yields nothing, while the mutant does not and yields the alpha channel as
    a figure -- precisely the defect the filter was written to prevent.

    Written by hand rather than through PyMuPDF's writer, which will not
    produce a zero-width image on request.
    """
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
        b"/Resources<</XObject<</Im0 4 0 R/Im1 5 0 R>>>>/Contents 6 0 R>>endobj\n"
        b"4 0 obj<</Type/XObject/Subtype/Image/Width 0/Height 10"
        b"/ColorSpace/DeviceGray/BitsPerComponent 8/SMask 5 0 R/Length 0>>stream\n"
        b"endstream\nendobj\n"
        b"5 0 obj<</Type/XObject/Subtype/Image/Width 10/Height 10"
        b"/ColorSpace/DeviceGray/BitsPerComponent 8/Length 100>>stream\n"
        + b"\x80"
        * 100
        + b"\nendstream\nendobj\n"
        b"6 0 obj<</Length 60>>stream\n"
        b"q 100 0 0 100 10 10 cm /Im0 Do Q q 50 0 0 50 10 120 cm /Im1 Do Q\n"
        b"endstream\nendobj\n"
        b"trailer<</Root 1 0 R/Size 7>>\n"
    )
    p = tmp_path / "promoted.pdf"
    p.write_bytes(pdf)

    with fitz.open(str(p)) as doc:
        infos = list(doc[0].get_images(full=True))
        assert [i[2] for i in infos] == [0, 10], (
            f"the fixture no longer carries a zero-width image: {infos}"
        )

        got = [g["xref"] for g in pdfdeck.iter_pdf_images(doc)]

    assert 5 not in got, (
        f"the promoted soft mask (xref 5) was counted as a figure: {got}. The "
        f"mask filter stopped recognising it because the image carrying it "
        f"has zero width."
    )
    assert got == [], got
