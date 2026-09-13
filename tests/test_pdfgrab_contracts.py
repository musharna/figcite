"""`figcite grab`'s engine, tested inside the push gate.

`pdfgrab.py` had a 3% mutation kill rate -- the lowest of any module that
touches provenance -- and the reason turned out to be structural rather than
sloppy: its only tests live in `test_live.py`, which is `@pytest.mark.live`
and therefore deselected by `pytest -m "not live"`, which is exactly what
`.githooks/pre-push` runs. So the PDF crop path had effectively no coverage in
the gate at all.

Nothing here needs the network or a real paper: PyMuPDF can write the fixture.
That was the only thing making these live in the first place.

Survivors this pins:
  - `if not (1 <= page <= doc.page_count)` -- dropping the `not` INVERTS the
    bounds check, in both `crop` and `list_images`.
  - `if not (0 <= image_index < len(infos))` -- same shape.
  - `if image_index is not None` / `elif rect is not None` -- `is not -> is`
    swaps which crop mode runs.
  - `return hits[0]` -- which DOI wins when a page names several, which is
    every real paper (its own DOI plus its references).
"""

import fitz
import pytest

from figcite.pdfgrab import crop, discover_doi, list_images

OWN_DOI = "10.1234/this-paper"
CITED_DOI = "10.5555/a-paper-it-cites"


def _pdf(tmp_path, name="paper.pdf", *, pages=1, meta=None, text=None):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 90), text if text is not None else f"page {i + 1}")
        page.draw_rect(fitz.Rect(72, 200, 300, 400), color=(0, 0, 1), fill=(0.2, 0.4, 0.9))
    if meta:
        doc.set_metadata(meta)
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return path


# --- which DOI wins ------------------------------------------------------


def test_the_first_doi_on_the_page_is_the_one_returned(tmp_path):
    """A real paper's first page carries its own DOI and the DOIs it cites.
    `hits[0]` is load-bearing, and `hits[1]` survived the sweep, so the
    fixture has to contain BOTH in a known order."""
    pdf = _pdf(
        tmp_path,
        text=f"https://doi.org/{OWN_DOI} ... see also https://doi.org/{CITED_DOI}",
    )

    doi, where = discover_doi(pdf)

    assert doi == OWN_DOI, f"picked a cited DOI over the paper's own: {doi}"
    assert "page 1" in where, where


def test_metadata_beats_page_text(tmp_path):
    """The precedence order is the point of the function: a DOI the publisher
    stamped into the file is better evidence than one scraped off the page."""
    pdf = _pdf(
        tmp_path,
        meta={"subject": f"doi:{OWN_DOI}"},
        text=f"https://doi.org/{CITED_DOI}",
    )

    doi, where = discover_doi(pdf)

    assert doi == OWN_DOI, f"page text won over metadata: {doi} from {where}"
    assert "metadata" in where, where


def test_no_doi_anywhere_says_where_it_looked(tmp_path):
    """Positive control, and the project's own rule: an absence has to name
    its scope or it reads as 'this paper has no DOI'."""
    pdf = _pdf(tmp_path, text="a page with no identifier on it at all")

    doi, where = discover_doi(pdf)

    assert doi is None
    assert "metadata" in where and "pages" in where, where


def test_scan_pages_bounds_the_search(tmp_path):
    """`scan_pages` survived `2 -> 3`. A DOI on page 3 must NOT be found with
    the default of 2, or the parameter means nothing."""
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 90), f"https://doi.org/{OWN_DOI}" if i == 2 else "x")
    path = tmp_path / "late.pdf"
    doc.save(path)
    doc.close()

    assert discover_doi(path, scan_pages=2)[0] is None
    assert discover_doi(path, scan_pages=3)[0] == OWN_DOI  # positive control


# --- bounds --------------------------------------------------------------


@pytest.mark.parametrize("page", [0, -1, 2])
def test_crop_refuses_a_page_outside_the_document(tmp_path, page):
    """Dropping the `not` inverts this check, so valid pages would be refused
    and invalid ones would reach `doc[page - 1]`."""
    pdf = _pdf(tmp_path, pages=1)
    with pytest.raises(ValueError, match="out of range"):
        crop(pdf, page, tmp_path / "out.png")


def test_crop_accepts_the_only_page(tmp_path):
    """Positive control for the three above: 'refuse everything' passes them
    all while the command is unusable."""
    pdf = _pdf(tmp_path, pages=1)
    out = tmp_path / "out.png"

    detail = crop(pdf, 1, out)

    assert out.exists() and out.stat().st_size > 0
    assert detail["page"] == 1


@pytest.mark.parametrize("page", [0, 2])
def test_list_images_refuses_a_page_outside_the_document(tmp_path, page):
    pdf = _pdf(tmp_path, pages=1)
    with pytest.raises(ValueError, match="out of range"):
        list_images(pdf, page)


def test_an_image_index_outside_the_page_is_refused(tmp_path):
    pdf = _pdf(tmp_path, pages=1)
    with pytest.raises(ValueError, match="image index"):
        crop(pdf, 1, tmp_path / "out.png", image_index=99)


# --- which crop mode runs ------------------------------------------------


def test_a_fractional_rect_is_a_fraction_of_the_page(tmp_path):
    """`frac` survived `False -> True`, and `elif rect is not None` survived
    `is not -> is`. Asserting the RESULTING rect in points is what tells the
    two modes apart -- a test that only checked the file exists cannot."""
    pdf = _pdf(tmp_path, pages=1)

    whole = crop(pdf, 1, tmp_path / "whole.png")
    half = crop(pdf, 1, tmp_path / "half.png", rect=(0.0, 0.0, 0.5, 0.5), frac=True)

    wx0, wy0, wx1, wy1 = whole["rect_points"]
    hx0, hy0, hx1, hy1 = half["rect_points"]
    assert (hx1 - hx0) == pytest.approx((wx1 - wx0) / 2, rel=0.02), (half, whole)
    assert (hy1 - hy0) == pytest.approx((wy1 - wy0) / 2, rel=0.02), (half, whole)


def test_a_points_rect_is_taken_literally(tmp_path):
    """The other branch of the same `frac` flag: without `frac`, the numbers
    are PDF points, so 0..0.5 would be a half-point sliver rather than half
    the page. Same input, different meaning -- which is the whole risk."""
    pdf = _pdf(tmp_path, pages=1)

    detail = crop(pdf, 1, tmp_path / "pts.png", rect=(72, 200, 300, 400), frac=False)

    assert detail["rect_points"] == [72.0, 200.0, 300.0, 400.0], detail
