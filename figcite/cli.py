from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

from . import store
from .crossref import normalize_doi, record_from_doi, search_bibliographic
from .provenance import Record, dhash_bytes, embed, now_stamps, sha256_bytes


def _slug(s: str, n: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s or "").strip("-")[:n] or "image"


def _library_dest(rec: Record, src: Path) -> Path:
    store._ensure()
    base = _slug(rec.doi or rec.short_cite or src.stem)
    stamp = (rec.captured_local or "")[:19].replace(":", "").replace("-", "")
    return store.LIBRARY / f"{base}--{stamp or 'na'}{src.suffix.lower() or '.png'}"


def _finalize(src: Path, rec: Record, out: Optional[str], quiet: bool = False) -> Path:
    dest = Path(out) if out else _library_dest(rec, src)
    rec = embed(src, dest, rec)
    store.put(rec)
    if not quiet:
        print(f"tagged -> {dest}")
        print(f"  {rec.display()}")
        if rec.license_url:
            print(f"  license: {rec.license_url}  ({rec.reuse})")
        else:
            print(f"  license: not stated by publisher ({rec.reuse})")
        if rec.retracted:
            print("  ** THIS WORK IS FLAGGED AS RETRACTED IN CROSSREF **")
        print(f"  sha256: {rec.sha256[:16]}...  (insert THIS file into your deck)")
    return dest


def _record_for(doi: Optional[str], cite: Optional[str], url: Optional[str],
                confirmed: bool, kind: str, detail: dict,
                adapted_from: Optional[str] = None, note: str = "") -> Record:
    if doi:
        rec = record_from_doi(doi, confirmed=confirmed, source_kind=kind, source_detail=detail)
    else:
        u, loc = now_stamps()
        rec = Record(citation=cite or "", short_cite=(cite or "")[:40], url=url,
                     source_kind=kind, source_detail=detail,
                     captured_utc=u, captured_local=loc,
                     confirmed=bool(cite or url))
    if adapted_from:
        rec.adapted_from = normalize_doi(adapted_from)
    if note:
        rec.note = note
    return rec


# ---------------------------------------------------------------- commands

def cmd_tag(a) -> int:
    src = Path(a.image)
    if not src.exists():
        print(f"no such image: {src}", file=sys.stderr)
        return 2
    if not (a.doi or a.cite or a.url):
        print("need at least one of --doi, --cite, --url", file=sys.stderr)
        return 2
    rec = _record_for(a.doi, a.cite, a.url, confirmed=True,
                      kind=a.source_kind, detail={"original_file": str(src.resolve())},
                      adapted_from=a.adapted_from, note=a.note or "")
    _finalize(src, rec, a.out)
    return 0


def cmd_grab(a) -> int:
    from .pdfgrab import crop, discover_doi
    pdf = Path(a.pdf)
    if not pdf.exists():
        print(f"no such pdf: {pdf}", file=sys.stderr)
        return 2
    rect = None
    if a.rect:
        parts = [float(x) for x in a.rect.replace(" ", "").split(",")]
        if len(parts) != 4:
            print("--rect wants x0,y0,x1,y1", file=sys.stderr)
            return 2
        rect = tuple(parts)  # type: ignore[assignment]
    tmp = Path(a.out) if a.out else Path(store.DATA_DIR) / "tmp-crop.png"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    detail = crop(pdf, a.page, tmp, rect=rect, frac=a.frac, dpi=a.dpi,
                  image_index=a.image_index)

    doi, where = (a.doi, "given on the command line") if a.doi else discover_doi(pdf)
    if not doi:
        print(f"no DOI found: {where}")
        print(f"cropped image left at {tmp}")
        print("re-run with --doi 10.xxxx/yyyy to attach a citation")
        return 1
    detail["doi_evidence"] = where
    rec = _record_for(doi, None, None, confirmed=True, kind="pdf-crop", detail=detail,
                      adapted_from=a.adapted_from, note=a.note or "")
    print(f"DOI {doi}  (found in {where})")
    dest = _finalize(tmp, rec, a.out)
    if not a.out and tmp.exists() and tmp != dest:
        tmp.unlink()
    return 0


def cmd_images(a) -> int:
    from .pdfgrab import list_images
    for im in list_images(a.pdf, a.page):
        print(f"  [{im['index']}] bbox={im['bbox']}  {im['width']}x{im['height']}px")
    return 0


def cmd_watch(a) -> int:
    from .clipboard import watch
    return watch(max_hours=a.hours, poll_ms=a.poll_ms)


def cmd_pending(a) -> int:
    from .clipboard import list_pending
    items = list_pending()
    if not items:
        print("nothing pending (run `figcite watch`, then snip something)")
        return 0
    for i, it in enumerate(items):
        png = Path(it["png"])
        cap = it.get("capture", {})
        inf = it.get("inference", {})
        print(f"[{i}] {png.name}  {cap.get('width','?')}x{cap.get('height','?')}")
        if cap.get("title"):
            print(f"     window: {cap.get('process','?')} — {cap['title'][:90]}")
        if inf.get("doi"):
            print(f"     DOI: {inf['doi']}   (from {inf.get('doi_evidence','')})")
            print(f"     confirm: figcite confirm {i}")
        elif inf.get("candidates"):
            for ci, c in enumerate(inf["candidates"]):
                print(f"     cand {ci}: score {c['score']:>5}  {c['doi']}")
                print(f"               {c['title'][:80]} ({c.get('container','')} {c.get('year','')}) [{c.get('type','')}]")
            print(f"     confirm: figcite confirm {i} --pick <n>   (or --doi 10.x/y)")
        else:
            print(f"     no source inferred: {inf.get('doi_evidence','')}")
            print(f"     confirm: figcite confirm {i} --doi 10.x/y")
    return 0


def cmd_confirm(a) -> int:
    from .clipboard import list_pending
    items = list_pending()
    try:
        it = items[a.index]
    except (IndexError, TypeError):
        print(f"no pending item {a.index} (have {len(items)})", file=sys.stderr)
        return 2
    png = Path(it["png"])
    inf = it.get("inference", {})
    doi = a.doi
    if doi is None and a.pick is not None:
        try:
            doi = inf["candidates"][a.pick]["doi"]
        except Exception:
            print(f"no candidate {a.pick} on item {a.index}", file=sys.stderr)
            return 2
    if doi is None:
        doi = inf.get("doi")
        if doi and not inf.get("grounded"):
            print("that DOI was only guessed; pass --doi explicitly to accept it",
                  file=sys.stderr)
            return 2
    if doi is None and not a.cite:
        print("need --doi, --pick N, or --cite", file=sys.stderr)
        return 2

    detail = {"clipboard_capture": it.get("capture", {}),
              "inference_kind": inf.get("kind", ""),
              "doi_evidence": inf.get("doi_evidence", "")}
    rec = _record_for(doi, a.cite, None, confirmed=True, kind="clipboard",
                      detail=detail, adapted_from=a.adapted_from, note=a.note or "")
    dest = _finalize(png, rec, a.out)
    for suffix in (".pending.json", ".capture.json"):
        p = Path(str(png)[:-4] + suffix)
        if p.exists():
            p.unlink()
    if png.exists() and png != dest:
        png.unlink()
    return 0


def cmd_resolve(a) -> int:
    rec = record_from_doi(a.doi, confirmed=True)
    print(rec.citation)
    print(f"  short: {rec.short_cite}")
    print(f"  license: {rec.license_url or '(none stated)'}  -> {rec.reuse}")
    if rec.retracted:
        print("  ** FLAGGED AS RETRACTED IN CROSSREF **")
    return 0


def cmd_search(a) -> int:
    for i, c in enumerate(search_bibliographic(a.query, rows=a.rows)):
        print(f"[{i}] score {c['score']:>6}  {c['doi']}")
        print(f"     {c['title'][:90]}")
        print(f"     {c.get('container','')} {c.get('year','')} [{c.get('type','')}]")
    print("\nnote: CrossRef title search ranks reviews/commentaries above the paper "
          "itself surprisingly often -- read the type field before picking.")
    return 0


def cmd_audit(a) -> int:
    from .deck import audit
    rep = audit(a.pptx, min_inches=a.min_inches)
    print(f"{rep['pptx']}: {rep['pictures']} picture(s), {rep['tagged']} with provenance, "
          f"{rep['unconfirmed']} unconfirmed, {rep['untagged_substantive']} substantive but unsourced")
    for r in rep["rows"]:
        rec = r["record"]
        tag = "decorative" if r["decorative"] else ""
        if rec is None:
            print(f"  slide {r['slide']:>3}  {r['shape'][:28]:28} NO SOURCE   {tag} [{r['matched_by']}]")
        else:
            mark = "ok " if rec.confirmed else "UNC"
            print(f"  slide {r['slide']:>3}  {r['shape'][:28]:28} {mark} {rec.short_cite or rec.doi} [{r['matched_by']}]")
    return 0


def cmd_apply(a) -> int:
    from .deck import apply
    out = a.out or str(Path(a.pptx).with_suffix("")) + ".cited.pptx"
    man = a.manifest
    if man is None and not a.no_manifest:
        man = str(Path(out).with_suffix(""))
    rep = apply(a.pptx, out,
                captions=not a.no_captions, credits=not a.no_credits,
                manifest_path=man, allow_unconfirmed=a.allow_unconfirmed,
                min_inches=a.min_inches)
    print(f"wrote {rep['out']}")
    print(f"  {rep['pictures']} picture(s), {rep['cited']} credited, {rep['unsourced']} unsourced")
    if rep["manifest"]:
        print(f"  manifest: {rep['manifest']['csv']}")
        print(f"            {rep['manifest']['json']}")
    for e in rep["entries"]:
        print(f"  {e}")
    if rep["unsourced"]:
        print(f"  ⚠ {rep['unsourced']} image(s) had no recorded source; the credits "
              f"slide says so explicitly rather than hiding it")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="figcite",
        description="Keep DOI/citation provenance attached to images through to your slides.")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tag", help="attach provenance to an existing image file")
    t.add_argument("image")
    t.add_argument("--doi")
    t.add_argument("--cite", help="freeform citation when there is no DOI")
    t.add_argument("--url")
    t.add_argument("--adapted-from", help="DOI of the ORIGINAL source, if this figure was reproduced")
    t.add_argument("--source-kind", default="download")
    t.add_argument("--note", default="")
    t.add_argument("-o", "--out")
    t.set_defaults(func=cmd_tag)

    g = sub.add_parser("grab", help="crop a figure out of a PDF, DOI attached")
    g.add_argument("pdf")
    g.add_argument("--page", type=int, required=True, help="1-based")
    g.add_argument("--rect", help="x0,y0,x1,y1 in PDF points (or fractions with --frac)")
    g.add_argument("--frac", action="store_true", help="treat --rect as 0-1 fractions of the page")
    g.add_argument("--image-index", type=int, help="crop embedded image N (see `figcite images`)")
    g.add_argument("--dpi", type=int, default=300)
    g.add_argument("--doi", help="override the DOI discovered in the PDF")
    g.add_argument("--adapted-from")
    g.add_argument("--note", default="")
    g.add_argument("-o", "--out")
    g.set_defaults(func=cmd_grab)

    i = sub.add_parser("images", help="list embedded images on a PDF page with bboxes")
    i.add_argument("pdf")
    i.add_argument("--page", type=int, required=True)
    i.set_defaults(func=cmd_images)

    w = sub.add_parser("watch", help="watch the Windows clipboard for snipped images")
    w.add_argument("--hours", type=float, default=8.0)
    w.add_argument("--poll-ms", type=int, default=800)
    w.set_defaults(func=cmd_watch)

    pe = sub.add_parser("pending", help="list captured-but-unconfirmed clipboard images")
    pe.set_defaults(func=cmd_pending)

    c = sub.add_parser("confirm", help="attach a DOI to a pending capture")
    c.add_argument("index", type=int)
    c.add_argument("--doi")
    c.add_argument("--pick", type=int, help="accept candidate N from `figcite pending`")
    c.add_argument("--cite")
    c.add_argument("--adapted-from")
    c.add_argument("--note", default="")
    c.add_argument("-o", "--out")
    c.set_defaults(func=cmd_confirm)

    r = sub.add_parser("resolve", help="show the citation + license for a DOI")
    r.add_argument("doi")
    r.set_defaults(func=cmd_resolve)

    s = sub.add_parser("search", help="find a DOI by title (candidates only)")
    s.add_argument("query")
    s.add_argument("--rows", type=int, default=5)
    s.set_defaults(func=cmd_search)

    a = sub.add_parser("audit", help="report provenance coverage of a .pptx")
    a.add_argument("pptx")
    a.add_argument("--min-inches", type=float, default=1.0)
    a.set_defaults(func=cmd_audit)

    ap = sub.add_parser("apply", help="write alt-text, captions, credits slide, manifest")
    ap.add_argument("pptx")
    ap.add_argument("-o", "--out")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--no-credits", action="store_true")
    ap.add_argument("--manifest", help="path stem for the .csv/.json manifest")
    ap.add_argument("--no-manifest", action="store_true")
    ap.add_argument("--allow-unconfirmed", action="store_true",
                    help="print machine-guessed citations onto slides (off by default)")
    ap.add_argument("--min-inches", type=float, default=1.0,
                    help="pictures smaller than this in both dimensions count as decorative")
    ap.set_defaults(func=cmd_apply)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (LookupError, RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
