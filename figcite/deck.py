"""Read and write provenance inside a .pptx.

Matching an image on a slide back to a record is tried in three passes:
  1. metadata still embedded in the media bytes
  2. exact sha256 against the manifest
  3. perceptual dhash against the manifest (catches PowerPoint recompression)

Pass 3 results are reported as fuzzy and treated as unconfirmed.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator, Optional

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Emu, Pt

from . import store
from .provenance import Record, dhash_bytes, read_embedded, sha256_bytes

CAPTION_PREFIX = "figcite-caption"
CREDITS_MARKER = "figcite-credits-marker"
CAPTION_PT = 8
CREDITS_TITLE_PT = 24
CREDITS_BODY_PT = 11
CREDITS_PER_SLIDE = 8


# ------------------------------------------------------------- traversal


def _tag(shape) -> str:
    """Local name of the shape's XML element: 'pic', 'sp', 'grpSp', ..."""
    return shape._element.tag.rsplit("}", 1)[-1]


def iter_pictures(slide) -> Iterator[tuple[Any, Any]]:
    """Yield (picture_shape, container) including pictures nested in groups.

    Dispatch is on the ELEMENT TAG, not on shape_type. python-pptx reports
    shape_type == PLACEHOLDER for a picture that occupies a layout placeholder,
    so a shape_type filter silently drops every image in a deck built on the
    stock theme layouts -- measured on a real 81-slide deck: 61 pictures, 0
    detected. The tag answers "is this a picture element", which is the actual
    question; shape_type answers "what role does this shape play in the layout",
    which is a different one.
    """

    def walk(shapes, container):
        for sh in shapes:
            t = _tag(sh)
            if t == "grpSp":
                yield from walk(sh.shapes, sh)
            elif t == "pic":
                yield sh, container

    yield from walk(slide.shapes, slide)


def _cnvpr(shape):
    el = shape._element
    for attr in ("_nvXxPr", "nvPicPr", "nvSpPr", "nvGrpSpPr"):
        nv = getattr(el, attr, None)
        if nv is not None and getattr(nv, "cNvPr", None) is not None:
            return nv.cNvPr
    return None


def get_alt_text(shape) -> str:
    c = _cnvpr(shape)
    return (c.get("descr") or "") if c is not None else ""


def set_alt_text(shape, descr: str, title: str = "") -> bool:
    c = _cnvpr(shape)
    if c is None:
        return False
    c.set("descr", descr)
    if title:
        c.set("title", title)
    return True


# ------------------------------------------------------------- matching


def match_picture(
    shape, manifest: Optional[dict[str, Record]] = None, fuzzy_distance: int = 6
) -> tuple[Optional[Record], str]:
    """(record, how it was matched)."""
    try:
        blob = shape.image.blob
    except Exception as e:
        return None, f"unreadable image ({e})"
    rec = read_embedded(blob)
    if rec is not None and (rec.doi or rec.citation):
        return rec, "embedded-metadata"
    manifest = store.all_records() if manifest is None else manifest
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
        fuzzy.confirmed = False
        fuzzy.note = (
            fuzzy.note + f" [matched perceptually, hamming={dist}; "
            "the deck's copy was re-encoded]"
        ).strip()
        return fuzzy, f"manifest-dhash(d={dist})"
    return None, "no match"


# ------------------------------------------------------------- audit


def audit(pptx_path: str | Path, min_inches: float = 1.0) -> dict:
    prs = Presentation(str(pptx_path))
    manifest = store.all_records()
    rows: list[dict] = []
    for si, slide in enumerate(prs.slides, start=1):
        for pic, _ in iter_pictures(slide):
            w_in = (pic.width or 0) / 914400
            h_in = (pic.height or 0) / 914400
            rec, how = match_picture(pic, manifest)
            rows.append(
                {
                    "slide": si,
                    "shape": pic.name,
                    "size_in": [round(w_in, 2), round(h_in, 2)],
                    "decorative": w_in < min_inches and h_in < min_inches,
                    "matched_by": how,
                    "record": rec,
                    "alt_text": get_alt_text(pic),
                }
            )
    tagged = [r for r in rows if r["record"] is not None]
    unconfirmed = [r for r in tagged if not r["record"].confirmed]
    untagged = [r for r in rows if r["record"] is None and not r["decorative"]]
    return {
        "pptx": str(pptx_path),
        "pictures": len(rows),
        "tagged": len(tagged),
        "unconfirmed": len(unconfirmed),
        "untagged_substantive": len(untagged),
        "rows": rows,
    }


# ------------------------------------------------------------- writing


def _blank_layout(prs):
    for lay in prs.slide_layouts:
        if "blank" in (lay.name or "").lower():
            return lay
    return min(prs.slide_layouts, key=lambda lay: len(lay.placeholders))


def _drop_existing_figcite_shapes(prs) -> int:
    """Make apply() idempotent: strip captions/credits from a previous run."""
    n = 0
    for slide in prs.slides:
        for sh in list(slide.shapes):
            if (sh.name or "").startswith(CAPTION_PREFIX):
                sh._element.getparent().remove(sh._element)
                n += 1
    # remove old credits slides
    id_list = prs.slides._sldIdLst
    for sld_id in list(id_list):
        rid = sld_id.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        )
        slide = prs.slides.part.related_part(rid)
        names = [(sh.name or "") for sh in slide.slide.shapes]
        if any(nm.startswith(CREDITS_MARKER) for nm in names):
            id_list.remove(sld_id)
            prs.slides.part.drop_rel(rid)
            n += 1
    return n


def _add_caption(slide, pic, text: str, index: int, prs) -> None:
    gap = Emu(int(0.04 * 914400))
    cap_h = Emu(int(0.26 * 914400))
    width = pic.width
    left = pic.left
    top = pic.top + pic.height + gap
    if top + cap_h > prs.slide_height:  # no room below: sit on the image
        top = pic.top + pic.height - cap_h
    tb = slide.shapes.add_textbox(left, top, width, cap_h)
    tb.name = f"{CAPTION_PREFIX}-{index}"
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = text
    run.font.size = Pt(CAPTION_PT)
    run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    run.font.italic = True


def _add_credits_slides(prs, entries: list[str], missing_note: str) -> int:
    lay = _blank_layout(prs)
    chunks = [
        entries[i : i + CREDITS_PER_SLIDE]
        for i in range(0, len(entries), CREDITS_PER_SLIDE)
    ] or [[]]
    made = 0
    for ci, chunk in enumerate(chunks):
        slide = prs.slides.add_slide(lay)
        made += 1
        m = Emu(int(0.6 * 914400))
        title = slide.shapes.add_textbox(
            m, m, prs.slide_width - 2 * m, Emu(int(0.7 * 914400))
        )
        title.name = f"{CREDITS_MARKER}-{ci}"
        tp = title.text_frame.paragraphs[0]
        tr = tp.add_run()
        tr.text = "Image credits" + (
            f" ({ci + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        )
        tr.font.size = Pt(CREDITS_TITLE_PT)
        tr.font.bold = True

        body = slide.shapes.add_textbox(
            m,
            m + Emu(int(0.9 * 914400)),
            prs.slide_width - 2 * m,
            prs.slide_height - m - Emu(int(1.5 * 914400)),
        )
        body.name = f"{CREDITS_MARKER}-body-{ci}"
        tf = body.text_frame
        tf.word_wrap = True
        first = True
        for line in chunk:
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            r = p.add_run()
            r.text = line
            r.font.size = Pt(CREDITS_BODY_PT)
        if ci == len(chunks) - 1 and missing_note:
            p = tf.add_paragraph()
            r = p.add_run()
            r.text = missing_note
            r.font.size = Pt(CREDITS_BODY_PT)
            r.font.italic = True
            r.font.color.rgb = RGBColor(0xB0, 0x40, 0x40)
    return made


def apply(
    pptx_path: str | Path,
    out_path: str | Path,
    *,
    captions: bool = True,
    credits: bool = True,
    caption_own_work: bool = False,
    manifest_path: Optional[str | Path] = None,
    allow_unconfirmed: bool = False,
    min_inches: float = 1.0,
) -> dict:
    """Write provenance into a deck. Returns a summary dict."""
    prs = Presentation(str(pptx_path))
    manifest = store.all_records()

    removed = _drop_existing_figcite_shapes(prs)

    numbering: dict[str, int] = {}
    entry_counts: dict[int, int] = {}
    entries: list[str] = []
    rows: list[dict] = []
    missing: list[int] = []
    next_n = 1

    for si, slide in enumerate(prs.slides, start=1):
        for pic, _ in iter_pictures(slide):
            w_in = (pic.width or 0) / 914400
            h_in = (pic.height or 0) / 914400
            decorative = w_in < min_inches and h_in < min_inches
            rec, how = match_picture(pic, manifest)

            if rec is None:
                if not decorative:
                    missing.append(si)
                    set_alt_text(
                        pic,
                        "Source not recorded (figcite found no provenance "
                        "for this image).",
                        "figcite: unsourced",
                    )
                rows.append(
                    {
                        "slide": si,
                        "shape": pic.name,
                        "matched_by": how,
                        "n": None,
                        "record": None,
                    }
                )
                continue

            # Dedupe by what the credit will SAY. Keying on the per-image hash
            # gave a real deck 15 separate entries all reading "This work".
            key = rec.doi or rec.citation or rec.short_cite or rec.sha256
            if key not in numbering:
                numbering[key] = next_n
                next_n += 1
                if rec.confirmed or allow_unconfirmed:
                    line = f"[{numbering[key]}] {rec.display()}"
                    # A license read off an unconfirmed source is unreliable too,
                    # so it only ships alongside a citation we are willing to print.
                    if rec.license_url:
                        line += f"  License: {rec.license_url} ({rec.reuse})"
                    elif rec.reuse and rec.reuse != "unknown":
                        line += f"  Reuse: {rec.reuse}"
                else:
                    # The candidate name is deliberately withheld from the deck: a
                    # deck gets presented and forwarded, and a labelled guess still
                    # reads as an attribution once it is on a screen. It stays
                    # visible in `figcite pending` and in the manifest.
                    line = (
                        f"[{numbering[key]}] SOURCE UNCONFIRMED — resolve with "
                        f"`figcite pending` before this deck leaves your hands"
                    )
                entries.append(line)
            n = numbering[key]
            entry_counts[n] = entry_counts.get(n, 0) + 1

            alt = rec.display()
            if rec.license_url:
                alt += f" License: {rec.license_url} ({rec.reuse})"
            set_alt_text(pic, alt, f"figcite [{n}]")

            own_work = rec.source_kind == "generated"
            if captions and not (own_work and not caption_own_work):
                if rec.confirmed or allow_unconfirmed:
                    cap = f"[{n}] {rec.short_cite or rec.citation[:60]}"
                    if rec.doi:
                        cap += f" · doi:{rec.doi}"
                else:
                    cap = f"[{n}] source unconfirmed"
                _add_caption(slide, pic, cap, n, prs)

            rows.append(
                {
                    "slide": si,
                    "shape": pic.name,
                    "matched_by": how,
                    "n": n,
                    "record": rec,
                }
            )

    entries = [
        e
        + (
            f"  ({entry_counts.get(i + 1, 1)} figures)"
            if entry_counts.get(i + 1, 1) > 1
            else ""
        )
        for i, e in enumerate(entries)
    ]
    missing_note = ""
    if missing:
        uniq = sorted(set(missing))
        missing_note = (
            f"⚠ {len(missing)} image(s) on slide(s) "
            f"{', '.join(map(str, uniq))} have no recorded source."
        )
    if credits:
        _add_credits_slides(prs, entries, missing_note)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))

    written = None
    if manifest_path:
        written = _write_manifest(manifest_path, rows)

    return {
        "out": str(out_path),
        "pictures": len(rows),
        "cited": len(entries),
        "unsourced": len(missing),
        "removed_prior_figcite_shapes": removed,
        "manifest": written,
        "entries": entries,
        "rows": rows,
    }


def _write_manifest(path: str | Path, rows: list[dict]) -> dict:
    path = Path(path)
    stem = path.with_suffix("")
    csv_path = str(stem) + ".csv"
    json_path = str(stem) + ".json"
    cols = [
        "n",
        "slide",
        "shape",
        "matched_by",
        "confirmed",
        "doi",
        "short_cite",
        "citation",
        "license_url",
        "reuse",
        "retracted",
        "adapted_from",
        "source_kind",
        "sha256",
        "captured_local",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            rec: Optional[Record] = r["record"]
            w.writerow(
                {
                    "n": r["n"],
                    "slide": r["slide"],
                    "shape": r["shape"],
                    "matched_by": r["matched_by"],
                    "confirmed": getattr(rec, "confirmed", ""),
                    "doi": getattr(rec, "doi", "") or "",
                    "short_cite": getattr(rec, "short_cite", "") or "",
                    "citation": getattr(rec, "citation", "") or "",
                    "license_url": getattr(rec, "license_url", "") or "",
                    "reuse": getattr(rec, "reuse", "") or "",
                    "retracted": getattr(rec, "retracted", ""),
                    "adapted_from": getattr(rec, "adapted_from", "") or "",
                    "source_kind": getattr(rec, "source_kind", "") or "",
                    "sha256": getattr(rec, "sha256", "") or "",
                    "captured_local": getattr(rec, "captured_local", "") or "",
                }
            )
    with open(json_path, "w", encoding="utf-8") as fh:
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
    return {"csv": csv_path, "json": json_path}
