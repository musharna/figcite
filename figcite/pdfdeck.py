"""Provenance for PDF output -- the format every design tool can produce.

Affinity, Illustrator, InDesign, Google Slides, Keynote and LaTeX all export
PDF, so this is the seam that covers them without parsing anyone's proprietary
document format.

Measured behaviour of a PDF export (see tests): it STRIPS embedded image
metadata and re-encodes the pixels, so sha256 no longer matches. The perceptual
hash does survive -- on two real figures from one paper, the exported copy
matched the correct figure at hamming 0 and the other at 23. That is why layer 3
exists, and here it is the only layer left.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator, Optional

import fitz

from . import store
from .provenance import Record, dhash_bytes, read_embedded, sha256_bytes

CAPTION_PT = 7
CREDITS_TITLE_PT = 16
CREDITS_BODY_PT = 9
GRAY = (0.4, 0.4, 0.4)
RED = (0.69, 0.25, 0.25)


def iter_pdf_images(doc) -> Iterator[dict[str, Any]]:
    """Every raster image placed on a page, with its bytes and placement.

    Soft masks are excluded. A transparent figure is stored as two objects -- the
    colour image and a greyscale alpha channel -- and some writers emit that mask
    as a top-level image rather than a child of the image it belongs to. Counting
    it as a figure inflates "substantive but unsourced" with an object that is
    not a figure and can never have provenance, sending you looking for a source
    that does not exist.

    Measured 2026-08-16 on real exporter output: ImageMagick keeps the mask as a
    child (4 objects for 4 figures) while Ghostscript promotes it (6 objects for
    the same 4 figures), so the same document audits differently depending on
    which tool wrote it. `get_images(full=True)` reports each image's own smask
    xref in field 1, so the masks can be identified exactly rather than guessed
    at from colourspace or size.
    """
    for pno in range(doc.page_count):
        page = doc[pno]
        seen: set[int] = set()
        infos = list(page.get_images(full=True))
        masks = {info[1] for info in infos if info[1]}
        for info in infos:
            xref = info[0]
            if xref in seen or xref in masks:
                continue
            seen.add(xref)
            try:
                blob = doc.extract_image(xref)["image"]
            except Exception:
                continue
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            yield {
                "page": pno + 1,
                "xref": xref,
                "blob": blob,
                "rects": [fitz.Rect(r) for r in rects],
            }


def match_image(
    blob: bytes, manifest: dict[str, Record], fuzzy_distance: int = 6
) -> tuple[Optional[Record], str]:
    rec = read_embedded(blob)
    if rec is not None and (rec.doi or rec.citation):
        return rec, "embedded-metadata"
    sha = sha256_bytes(blob)
    if sha in manifest:
        return manifest[sha], "manifest-sha256"
    try:
        dh = dhash_bytes(blob)
    except Exception:
        return None, "no match (image could not be hashed)"
    hit = store.find_similar(dh, max_distance=fuzzy_distance)
    if hit:
        rec, dist = hit
        fuzzy = Record.from_dict(asdict(rec))
        fuzzy.note = (
            fuzzy.note + f" [matched perceptually, hamming={dist}; PDF export "
            "re-encodes images so sha256 cannot match]"
        ).strip()
        return fuzzy, f"manifest-dhash(d={dist})"
    return None, "no match"


def audit(pdf_path: str | Path, min_inches: float = 1.0) -> dict:
    doc = fitz.open(str(pdf_path))
    manifest = store.all_records()
    rows: list[dict] = []
    try:
        for im in iter_pdf_images(doc):
            r = im["rects"][0] if im["rects"] else None
            w_in = (r.width / 72) if r else 0.0
            h_in = (r.height / 72) if r else 0.0
            rec, how = match_image(im["blob"], manifest)
            rows.append(
                {
                    "page": im["page"],
                    "xref": im["xref"],
                    "size_in": [round(w_in, 2), round(h_in, 2)],
                    "decorative": bool(r) and w_in < min_inches and h_in < min_inches,
                    "matched_by": how,
                    "record": rec,
                }
            )
    finally:
        doc.close()
    tagged = [r for r in rows if r["record"] is not None]
    return {
        "file": str(pdf_path),
        "pictures": len(rows),
        "tagged": len(tagged),
        "unconfirmed": len([r for r in tagged if not r["record"].confirmed]),
        "untagged_substantive": len(
            [r for r in rows if r["record"] is None and not r["decorative"]]
        ),
        "rows": rows,
    }


def apply(
    pdf_path: str | Path,
    out_path: str | Path,
    *,
    captions: bool = True,
    credits: bool = True,
    caption_own_work: bool = False,
    manifest_path: Optional[str | Path] = None,
    allow_unconfirmed: bool = False,
    min_inches: float = 1.0,
) -> dict:
    """Stamp captions + a credits page onto an exported PDF."""
    doc = fitz.open(str(pdf_path))
    manifest = store.all_records()
    numbering: dict[str, int] = {}
    entry_counts: dict[int, int] = {}
    entries: list[str] = []
    rows: list[dict] = []
    missing: list[int] = []
    next_n = 1

    try:
        for im in iter_pdf_images(doc):
            page = doc[im["page"] - 1]
            r = im["rects"][0] if im["rects"] else None
            w_in = (r.width / 72) if r else 0.0
            h_in = (r.height / 72) if r else 0.0
            decorative = bool(r) and w_in < min_inches and h_in < min_inches
            rec, how = match_image(im["blob"], manifest)

            if rec is None:
                if not decorative:
                    missing.append(im["page"])
                rows.append(
                    {"page": im["page"], "matched_by": how, "n": None, "record": None}
                )
                continue

            # Dedupe by what the credit will SAY. Keying on the per-image hash
            # gave a real deck 15 separate entries all reading "This work".
            key = rec.doi or rec.citation or rec.short_cite or rec.sha256
            if key not in numbering:
                numbering[key] = next_n
                next_n += 1
                entries.append(_credit_line(numbering[key], rec, allow_unconfirmed))
            n = numbering[key]
            entry_counts[n] = entry_counts.get(n, 0) + 1

            own_work = rec.source_kind == "generated"
            if captions and r is not None and not (own_work and not caption_own_work):
                _caption(page, r, _caption_text(n, rec, allow_unconfirmed))
            rows.append({"page": im["page"], "matched_by": how, "n": n, "record": rec})

        entries = [
            e
            + (
                f"  ({entry_counts.get(i + 1, 1)} figures)"
                if entry_counts.get(i + 1, 1) > 1
                else ""
            )
            for i, e in enumerate(entries)
        ]
        note = ""
        if missing:
            uniq = sorted(set(missing))
            note = (
                f"{len(missing)} image(s) on page(s) "
                f"{', '.join(map(str, uniq))} have no recorded source."
            )
        if credits:
            _credits_pages(doc, entries, note)

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(out_path))
    finally:
        doc.close()

    written = _write_manifest(manifest_path, rows) if manifest_path else None
    return {
        "out": str(out_path),
        "pictures": len(rows),
        "cited": len(entries),
        "unsourced": len(missing),
        "manifest": written,
        "entries": entries,
        "rows": rows,
    }


def _caption_text(n: int, rec: Record, allow_unconfirmed: bool) -> str:
    if rec.confirmed or allow_unconfirmed:
        t = f"[{n}] {rec.short_cite or rec.citation[:60]}"
        return t + (f" · doi:{rec.doi}" if rec.doi else "")
    return f"[{n}] source unconfirmed"


def _credit_line(n: int, rec: Record, allow_unconfirmed: bool) -> str:
    if rec.confirmed or allow_unconfirmed:
        line = f"[{n}] {rec.display()}"
        if rec.license_url:
            line += f"  License: {rec.license_url} ({rec.reuse})"
        return line
    # Withheld from the page for the same reason as in a deck: a labelled guess
    # still reads as an attribution. The capture context is safe to show -- it is
    # an observation, not a citation.
    ctx = rec.context_line()
    return (
        f"[{n}] SOURCE UNCONFIRMED"
        + (f" — {ctx}" if ctx else "")
        + " — resolve with `figcite pending`"
    )


def _fit_textbox(
    page,
    rect,
    text: str,
    fontsize: float,
    fontname: str = "helv",
    color=None,
    min_fontsize: float = 5.0,
) -> float:
    """insert_textbox that cannot fail silently.

    PyMuPDF returns a NEGATIVE float when the text does not fit and draws
    nothing at all -- no exception. That silently dropped the credits-page
    heading. Here the return value is checked, the font is stepped down to make
    it fit, and running out of room raises instead of vanishing.

    Returns the vertical space consumed.
    """
    fs = fontsize
    while fs >= min_fontsize:
        rc = page.insert_textbox(
            rect, text, fontsize=fs, fontname=fontname, color=color
        )
        if rc >= 0:
            return rect.height - rc
        fs -= 0.5
    raise ValueError(
        f"text does not fit in {rect.height:.0f}pt even at {min_fontsize}pt: {text[:60]!r}"
    )


def _caption(page, rect, text: str) -> None:
    """A caption under the image, or just inside its lower edge if there is no room."""
    h = 2 * CAPTION_PT + 6  # room for two wrapped lines
    y0 = rect.y1 + 1
    if y0 + h > page.rect.y1:
        y0 = max(rect.y1 - h - 1, page.rect.y0)
    box = fitz.Rect(rect.x0, y0, max(rect.x1, rect.x0 + 120), y0 + h)
    try:
        _fit_textbox(page, box, text, CAPTION_PT, "helv", GRAY)
    except ValueError:
        # Never let a caption failure be invisible; shorten rather than drop it.
        _fit_textbox(
            page, box, text[:60] + "...", CAPTION_PT, "helv", GRAY, min_fontsize=4.0
        )


def _credits_pages(doc, entries: list[str], missing_note: str) -> int:
    """Append credits page(s). Entries are measured, so nothing is silently lost."""
    last = doc[doc.page_count - 1].rect if doc.page_count else fitz.Rect(0, 0, 612, 792)
    m = 48
    width = last.width - 2 * m
    bottom = last.height - m - 40
    pages = 0
    page = None
    y = 0.0

    def new_page(n):
        nonlocal page, y, pages
        page = doc.new_page(width=last.width, height=last.height)
        pages += 1
        title = "Image credits" + (f" (cont. {n})" if n > 1 else "")
        used = _fit_textbox(
            page, fitz.Rect(m, m, m + width, m + 40), title, CREDITS_TITLE_PT, "hebo"
        )
        y = m + used + 10

    new_page(1)
    for line in entries:
        needed = _measure(line, width, CREDITS_BODY_PT)
        if y + needed > bottom:
            new_page(pages + 1)
        used = _fit_textbox(
            page,
            fitz.Rect(m, y, m + width, y + needed + 4),
            line,
            CREDITS_BODY_PT,
            "helv",
        )
        y += used + 6
    if missing_note:
        note = "! " + missing_note
        needed = _measure(note, width, CREDITS_BODY_PT)
        if y + needed > bottom:
            new_page(pages + 1)
        _fit_textbox(
            page,
            fitz.Rect(m, y, m + width, y + needed + 4),
            note,
            CREDITS_BODY_PT,
            "helv",
            RED,
        )
    return pages


def _measure(text: str, width: float, fontsize: float, fontname: str = "helv") -> float:
    """Height one entry needs, from real glyph widths rather than a guess."""
    font = fitz.Font(fontname=fontname)
    lines, cur = 1, 0.0
    for word in (text or "").split():
        # PyMuPDF's stub says int; the C call takes a float and layout is in points.
        w = font.text_length(word + " ", fontsize=fontsize)  # pyright: ignore[reportArgumentType]
        if cur + w > width:
            lines += 1
            cur = w
        else:
            cur += w
    return lines * fontsize * 1.35 + 4


def _write_manifest(path: str | Path, rows: list[dict]) -> dict:
    stem = str(Path(path).with_suffix(""))
    cols = [
        "n",
        "page",
        "matched_by",
        "confirmed",
        "doi",
        "short_cite",
        "citation",
        "license_url",
        "reuse",
        "source_kind",
        "captured_local",
        "context",
    ]
    with open(stem + ".csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            rec = r["record"]
            w.writerow(
                {
                    "n": r["n"],
                    "page": r["page"],
                    "matched_by": r["matched_by"],
                    "confirmed": getattr(rec, "confirmed", ""),
                    "doi": getattr(rec, "doi", "") or "",
                    "short_cite": getattr(rec, "short_cite", "") or "",
                    "citation": getattr(rec, "citation", "") or "",
                    "license_url": getattr(rec, "license_url", "") or "",
                    "reuse": getattr(rec, "reuse", "") or "",
                    "source_kind": getattr(rec, "source_kind", "") or "",
                    "captured_local": getattr(rec, "captured_local", "") or "",
                    "context": rec.context_line() if rec else "",
                }
            )
    with open(stem + ".json", "w", encoding="utf-8") as fh:
        json.dump(
            [
                {
                    **{k: v for k, v in r.items() if k != "record"},
                    "record": asdict(r["record"]) if r["record"] else None,
                }
                for r in rows
            ],
            fh,
            ensure_ascii=False,
            indent=2,
        )
    return {"csv": stem + ".csv", "json": stem + ".json"}
