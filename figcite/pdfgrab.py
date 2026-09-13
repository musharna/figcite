"""Crop a figure out of a paper PDF, with the paper's own DOI attached.

The DOI here is read out of the PDF being cropped, so it is grounded evidence
about which document the figure came from -- that is why this path is allowed
to mark records confirmed. What it CANNOT know is whether the figure was itself
reproduced from an earlier paper; use --adapted-from for that.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

from .crossref import find_dois


def discover_doi(pdf: str | os.PathLike, scan_pages: int = 2) -> tuple[Optional[str], str]:
    """(doi, where it was found). Returns (None, reason) when nothing is found."""
    doc = fitz.open(str(pdf))
    try:
        meta = doc.metadata or {}
        for k in ("doi", "subject", "keywords", "title", "producer"):
            v = meta.get(k) or ""
            hits = find_dois(v)
            if hits:
                return hits[0], f"pdf metadata field '{k}'"
        try:
            xmp = doc.xref_xml_metadata()
            hits = find_dois(xmp or "")
            if hits:
                return hits[0], "pdf XMP packet"
        except Exception:
            pass
        for pno in range(min(scan_pages, doc.page_count)):
            hits = find_dois(str(doc[pno].get_text()))
            if hits:
                return hits[0], f"text of page {pno + 1}"
        return None, f"no DOI in metadata, XMP, or the first {scan_pages} pages of text"
    finally:
        doc.close()


def list_images(pdf: str | os.PathLike, page: int) -> list[dict]:
    """Embedded raster images on a 1-based page, with their bboxes in points."""
    doc = fitz.open(str(pdf))
    try:
        if not (1 <= page <= doc.page_count):
            raise ValueError(f"page {page} out of range (pdf has {doc.page_count})")
        out = []
        for i, info in enumerate(doc[page - 1].get_image_info(xrefs=True)):
            b = info.get("bbox")
            out.append(
                {
                    "index": i,
                    "xref": info.get("xref"),
                    "bbox": [round(v, 1) for v in b] if b else None,
                    "width": info.get("width"),
                    "height": info.get("height"),
                }
            )
        return out
    finally:
        doc.close()


def crop(
    pdf: str | os.PathLike,
    page: int,
    out_path: str | os.PathLike,
    *,
    rect: Optional[tuple[float, float, float, float]] = None,
    frac: bool = False,
    dpi: int = 300,
    image_index: Optional[int] = None,
) -> dict:
    """Render a region of a page to PNG. rect is (x0,y0,x1,y1).

    frac=True treats rect as fractions of the page box, which is how a human
    eyeballs "the top-left quarter" without knowing PDF points.
    """
    doc = fitz.open(str(pdf))
    try:
        if not (1 <= page <= doc.page_count):
            raise ValueError(f"page {page} out of range (pdf has {doc.page_count})")
        pg = doc[page - 1]
        clip = None
        if image_index is not None:
            infos = pg.get_image_info(xrefs=True)
            if not (0 <= image_index < len(infos)):
                raise ValueError(
                    f"image index {image_index} out of range ({len(infos)} images on page {page})"
                )
            clip = fitz.Rect(infos[image_index]["bbox"])
        elif rect is not None:
            if frac:
                pr = pg.rect
                x0, y0, x1, y1 = rect
                clip = fitz.Rect(
                    pr.x0 + x0 * pr.width,
                    pr.y0 + y0 * pr.height,
                    pr.x0 + x1 * pr.width,
                    pr.y0 + y1 * pr.height,
                )
            else:
                clip = fitz.Rect(*rect)
        pix = pg.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=clip, alpha=False)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(out_path))
        return {
            "pdf": os.path.abspath(str(pdf)),
            "page": page,
            "rect_points": [round(v, 1) for v in (clip or pg.rect)],
            "dpi": dpi,
            "pixels": [pix.width, pix.height],
            "image_index": image_index,
        }
    finally:
        doc.close()
