from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import store
from ._actions import finalize as _finalize_no_print
from ._actions import record_for as _record_for
from .crossref import record_from_doi, search_bibliographic
from .provenance import Record, now_stamps


def _print_filed(rec: Record, dest: Path) -> None:
    """What the CLI says after an image lands in the library.

    M2. This block used to exist twice -- here and copied into cmd_confirm --
    and only one copy had a test, so a mutation to the other stayed green.
    One copy, one net.
    """
    print(f"tagged -> {dest}")
    print(f"  {rec.display()}")
    if rec.license_url:
        print(f"  license: {rec.license_url}  ({rec.reuse})")
    else:
        print(f"  license: not stated by publisher ({rec.reuse})")
    if rec.retracted:
        print("  ** THIS WORK IS FLAGGED AS RETRACTED IN CROSSREF **")
    print(f"  sha256: {rec.sha256[:16]}...  (insert THIS file into your deck)")


def _in_cli_words(msg: str) -> str:
    """Translate the service's kwarg vocabulary into CLI flags.

    I3. The service speaks kwargs because the web UI is its other consumer and
    kwargs are correct there. Passed through verbatim, the CLI told the user to
    "pass doi=, pick=, cite=, or own_work=True" -- names no shell accepts.
    own_work used to be DELETED from the advice rather than renamed, because
    `figcite confirm` had no such flag and advertising an option that does not
    exist is worse than saying nothing. It has one now, so it is translated
    like the others.
    """
    for kwarg, flag in (("doi=", "--doi"), ("pick=", "--pick N"), ("cite=", "--cite"),
                        ("own_work=True", "--own-work")):
        msg = msg.replace(kwarg, flag)
    return msg


def _finalize(src: Path, rec: Record, out: Optional[str], quiet: bool = False) -> Path:
    """CLI-side wrapper: the service's finalize() no longer prints, so the CLI
    prints here instead -- the one place that still needs to."""
    dest = _finalize_no_print(src, rec, out)
    if not quiet:
        _print_filed(rec, dest)
    return dest


# ---------------------------------------------------------------- commands


def cmd_tag(a) -> int:
    src = Path(a.image)
    if not src.exists():
        print(f"no such image: {src}", file=sys.stderr)
        return 2
    if not (a.doi or a.cite or a.url):
        print("need at least one of --doi, --cite, --url", file=sys.stderr)
        return 2
    rec = _record_for(
        a.doi,
        a.cite,
        a.url,
        confirmed=True,
        kind=a.source_kind,
        detail={"original_file": str(src.resolve())},
        adapted_from=a.adapted_from,
        note=a.note or "",
    )
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
    detail = crop(
        pdf, a.page, tmp, rect=rect, frac=a.frac, dpi=a.dpi, image_index=a.image_index
    )

    doi, where = (a.doi, "given on the command line") if a.doi else discover_doi(pdf)
    if not doi:
        print(f"no DOI found: {where}")
        print(f"cropped image left at {tmp}")
        print("re-run with --doi 10.xxxx/yyyy to attach a citation")
        return 1
    detail["doi_evidence"] = where
    rec = _record_for(
        doi,
        None,
        None,
        confirmed=True,
        kind="pdf-crop",
        detail=detail,
        adapted_from=a.adapted_from,
        note=a.note or "",
    )
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

    return watch(
        max_hours=a.hours, poll_ms=a.poll_ms, auto_confirm=not a.no_auto_confirm
    )


def cmd_ui(a) -> int:
    from . import web

    # Returned 0 unconditionally, so a refused bind exited success.
    return web.serve(port=a.port, open_browser=a.open)


def cmd_autostart_install(a) -> int:
    from . import autostart

    res = autostart.install(hours=a.hours, start_now=not a.no_start)
    if not res["ok"]:
        print("error: could not write the startup launcher", file=sys.stderr)
        return 1
    print("installed — the clipboard watcher will start at every logon")
    print(f"  launcher : {res['vbs']}")
    print(f"  log      : {res['log']}")
    print(
        f"  restarts automatically if it exits (own deadline {res['hours']:g}h,"
        f" retry {autostart.RESTART_SECONDS}s)"
    )
    if res["started"]:
        print("  started now; verify with `figcite autostart status`")
    elif not a.no_start:
        print("  already running; left it alone")
    print("remove with: figcite autostart uninstall")
    return 0


def cmd_autostart_status(a) -> int:
    from . import autostart
    from datetime import datetime

    st = autostart.status()
    if not st["installed"]:
        print("autostart: NOT installed  (figcite autostart install)")
    else:
        print(f"autostart: installed   {st['launcher']}")
    # The load-bearing line: the polling process itself. A launcher sitting in
    # Startup is an intention; only this says the clipboard is being read.
    if st["watching"]:
        for p in st["processes"]:
            print(f"WATCHING   clipboard poller pid={p['pid']} since {p['started']}")
    else:
        print("NOT WATCHING — no clipboard poller process is running")
    if st["supervisors"]:
        print(
            f"supervisor {len(st['supervisors'])} restart loop(s) alive "
            f"(pid {', '.join(p['pid'] for p in st['supervisors'])})"
        )
    elif st["installed"]:
        print(
            "supervisor NOT running — starts at next logon, or `figcite "
            "autostart install` to start it now"
        )
    if st["log_mtime"]:
        age = (datetime.now().timestamp() - st["log_mtime"]) / 60.0
        print(
            f"log        {st['log']}  (last write {age:.0f} min ago, "
            f"{st['sessions']} session(s))"
        )
    print(f"staged     {st['staged_pngs']} un-processed png(s)")
    print(f"filed      {st['clipboard_records']} clipboard capture(s) in the manifest")
    if st["log_tail"]:
        print("--- log tail ---")
        for line in st["log_tail"]:
            print(f"  {line}")
    return 0 if st["watching"] else 1


def cmd_autostart_uninstall(a) -> int:
    from . import autostart

    res = autostart.uninstall()
    print(
        "launcher removed" if res["removed_launcher"] else "no launcher was installed"
    )
    print("watcher stopped" if res["ok"] else f"warning: {res['stderr']}")
    print("captures and the manifest were left untouched")
    return 0 if res["ok"] else 1


def cmd_pending(a) -> int:
    from . import service

    items = service.pending_items()
    if not items:
        print("nothing pending (run `figcite watch`, then snip something)")
        return 0
    staged = [i for i in items if i.kind == "staged"]
    filed = [i for i in items if i.kind == "filed"]
    if filed:
        print(f"{len(filed)} filed capture(s) with context but no citation:")
    for it in filed:
        # The service assigns the label, so the terminal and the browser cannot
        # disagree about which capture "m0" names.
        i = it.cli_ref
        print(f"[{i}] {it.context[:100]}")
        # `note` and `doi_evidence` carry the same sentence on an auto-filed
        # capture, so the read path blanks the duplicate -- printing only
        # `note` therefore left rows with no reason at all.
        why = it.doi_evidence or it.note
        if why:
            print(f"      why: {why[:96]}")
        for ci, c in enumerate(it.candidates):
            print(f"      cand {ci}: {c['doi']}  [{c.get('source', '')}]")
            print(
                f"               {c['title'][:80]} "
                f"({c.get('container', '')} {c.get('year', '')})"
            )
        if it.candidates:
            print(f"      resolve: figcite confirm {i} --pick N")
        else:
            print(f"      resolve: figcite confirm {i} --doi 10.x/y")
    if filed:
        print()
    for i, it in enumerate(staged):
        print(f"[{i}] {it.ref.split(':', 1)[1]}  {it.width or '?'}x{it.height or '?'}")
        if it.context:
            print(f"     window: {it.context[:90]}")
        if it.error:
            print(f"     LOOKUP FAILED: {it.error}")
        elif it.doi:
            print(f"     DOI: {it.doi}   (from {it.doi_evidence})")
            print(f"     confirm: figcite confirm {i}")
        elif it.candidates:
            for ci, c in enumerate(it.candidates):
                print(f"     cand {ci}: score {c['score']:>5}  {c['doi']}")
                print(
                    f"               {c['title'][:80]} ({c.get('container', '')} {c.get('year', '')}) [{c.get('type', '')}]"
                )
            print(f"     confirm: figcite confirm {i} --pick <n>   (or --doi 10.x/y)")
        else:
            print(f"     no source inferred: {it.doi_evidence}")
            print(f"     confirm: figcite confirm {i} --doi 10.x/y")
    return 0


def cmd_confirm(a) -> int:
    from . import service

    items = service.pending_items()
    idx = str(a.index)
    if idx.startswith("m"):
        pool = [i for i in items if i.kind == "filed"]
        n = idx[1:]
    else:
        pool = [i for i in items if i.kind == "staged"]
        n = idx
    try:
        ref = pool[int(n)].ref
    except (ValueError, IndexError):
        print(f"no pending item {a.index} (have {len(pool)})", file=sys.stderr)
        return 2

    try:
        res = service.confirm(
            ref,
            doi=a.doi,
            pick=a.pick,
            cite=a.cite,
            own_work=a.own_work,
            adapted_from=a.adapted_from,
            note=a.note or "",
            out=a.out,
        )
    except service.NotGrounded as e:
        print(_in_cli_words(str(e)), file=sys.stderr)
        return 2
    except ValueError as e:
        print(_in_cli_words(str(e)), file=sys.stderr)
        return 2
    except KeyError as e:
        # Not in the brief. Without it `--pick 9` (or any out-of-range
        # candidate) tracebacks, where the old cmd_confirm printed a message
        # and exited 2 -- and the brief's own contract for this task is
        # "output text and exit codes are unchanged".
        print(_in_cli_words(e.args[0] if e.args else str(e)), file=sys.stderr)
        return 2

    rec = res.record
    if res.path is None:
        # A `filed:` ref: the bytes were already in the library, so there is no
        # destination to name -- exactly what the pre-refactor `m` branch said.
        print(f"resolved {a.index}: {rec.display()}")
        print(
            "  (the image file itself is unchanged; the manifest now carries the citation)"
        )
        return 0

    # Ruling 6. Restored verbatim from the pre-refactor `_finalize` printer
    # (a1d6b66^:figcite/cli.py). The sha256 line names WHICH file to insert
    # into the deck, and the retraction line is a correctness warning about
    # the source; neither is decoration.
    _print_filed(rec, res.path)
    return 0


def cmd_register(a) -> int:
    """Record provenance for an image already on disk, leaving the file untouched."""
    src = Path(a.image)
    if not src.exists():
        print(f"no such image: {src}", file=sys.stderr)
        return 2
    detail = {"original_file": str(src.resolve())}
    if a.this_work:
        u, loc = now_stamps()
        commit = None
        try:
            r = subprocess.run(
                ["git", "-C", str(src.parent), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0:
                commit = r.stdout.strip()
        except Exception:
            pass
        detail["git_commit"] = commit
        rec = Record(
            citation=a.cite or "This work",
            short_cite=a.cite or "This work",
            source_kind="generated",
            source_detail=detail,
            captured_utc=u,
            captured_local=loc,
            confirmed=True,
            note=a.note or "",
        )
    else:
        rec = _record_for(
            a.doi,
            a.cite,
            a.url,
            confirmed=True,
            kind=a.source_kind,
            detail=detail,
            adapted_from=a.adapted_from,
            note=a.note or "",
        )
    rec = store.register_existing(src, rec)
    print(f"registered (file unmodified): {src}")
    print(f"  {rec.display()}")
    print(f"  sha256 {rec.sha256[:16]}  dhash {rec.dhash}")
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
        print(
            f"     {c.get('container', '')} {c.get('year', '')} [{c.get('type', '')}]"
        )
    print(
        "\nnote: CrossRef title search ranks reviews/commentaries above the paper "
        "itself surprisingly often -- read the type field before picking."
    )
    return 0


def cmd_zotero_sync(a) -> int:
    from . import zotero

    print("fetching library from the Zotero API...")
    rep = zotero.sync(progress=True)
    print(f"{rep['items']} item(s), {rep['with_doi']} resolvable to a DOI")
    print(f"cached: {rep['cache']}")
    return 0


def cmd_zotero_configure(a) -> int:
    from . import zotero

    path = zotero.save_credentials(a.api_key, a.library_id, a.type)
    print(f"wrote {path} (mode 0600)")
    print(
        "  background processes read this file directly. The clipboard watcher "
        "is started by a Windows launcher whose shell inherits no exports, so a "
        "shell-only export would leave the watcher unable to resolve anything."
    )
    try:
        items = zotero.library(max_age_hours=0)  # force a fetch to prove it works
    except Exception as e:
        print(f"  but the library could not be read: {e}", file=sys.stderr)
        return 1
    print(f"  verified: {len(items)} item(s) readable")
    return 0


def cmd_zotero_status(a) -> int:
    from . import zotero

    if not zotero.configured():
        print("Zotero: NOT CONFIGURED")
        print(
            "  set FIGCITE_ZOTERO_API_KEY, FIGCITE_ZOTERO_LIBRARY_ID and\n"
            "  FIGCITE_ZOTERO_LIBRARY_TYPE (user|group) to enable library-first "
            "resolution"
        )
        return 1
    _key, lib, typ = zotero.credentials()
    print(f"Zotero: configured  -> {typ}s/{lib}")
    try:
        items = zotero.library()
    except Exception as e:
        print(f"  library unavailable: {e}")
        return 1
    with_doi = [i for i in items if i["doi"]]
    from collections import Counter

    src = Counter(i.get("doi_source", "") for i in with_doi)
    # The counts that decide whether this route can ever fire, printed rather
    # than assumed: a library of webpages with no DOIs resolves nothing.
    print(f"  {len(items)} item(s) cached, {len(with_doi)} resolvable to a DOI")
    print(f"    from the DOI field: {src.get('doi-field', 0)}")
    print(f"    from a doi.org URL: {src.get('url', 0)}")
    titles = Counter(zotero.normalize_title(i["title"]) for i in with_doi)
    print(f"  {sum(1 for _t, n in titles.items() if n == 1)} unambiguous title(s)")
    return 0


def cmd_zotero_resolve(a) -> int:
    from . import zotero

    r = zotero.resolve(a.title)
    print(f"query: {a.title}")
    print(f"  DOI: {r['doi'] or '(none)'}   grounded={r['grounded']}")
    print(f"  why: {r['evidence']}")
    for i, c in enumerate(r.get("candidates") or []):
        print(f"  cand {i}: {c['doi'] or '(no doi)'}  {c['title'][:70]}")
    return 0


def _records_for_bib(source: Optional[str]):
    """Records behind one deck, one apply-manifest, or the whole store."""
    from . import store
    from .provenance import Record

    if not source:
        return list(store.all_records().values())
    p = Path(source)
    if not p.exists():
        raise FileNotFoundError(f"no such file: {source}")
    if p.suffix.lower() == ".json":
        # The .json half of an apply manifest: authoritative about what actually
        # went into that deck, including images later removed from the store.
        data = json.loads(p.read_text(encoding="utf-8"))
        return [Record.from_dict(r["record"]) for r in data if r.get("record")]
    if _is_pdf(p):
        from .pdfdeck import audit as pdf_audit

        return [r["record"] for r in pdf_audit(str(p))["rows"] if r["record"]]
    from .deck import audit

    return [r["record"] for r in audit(str(p))["rows"] if r["record"]]


def cmd_bib(a) -> int:
    from . import bibtex

    recs = _records_for_bib(a.source)
    rep = bibtex.records_to_bibtex(recs, include_unconfirmed=a.include_unconfirmed)

    out = a.out
    if not out and a.source and Path(a.source).suffix.lower() != ".json":
        out = str(Path(a.source).with_suffix("")) + ".bib"
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(rep["bibtex"], encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(rep["bibtex"])

    # Every skip is reported. A bibliography that is short because entries
    # vanished looks identical to one that is short because the deck was small.
    print(f"  {rep['included']} entr(ies) written", file=sys.stderr)
    if rep["skipped_unconfirmed"]:
        print(
            f"  {rep['skipped_unconfirmed']} skipped as UNCONFIRMED -- a machine "
            f"guessed which paper those figures came from. ghostcite cannot catch "
            f"that error (the byline would match the DOI perfectly), so resolve "
            f"them with `figcite pending` or pass --include-unconfirmed.",
            file=sys.stderr,
        )
    if rep["skipped_no_doi"]:
        print(
            f"  {rep['skipped_no_doi']} skipped with no DOI (context only)",
            file=sys.stderr,
        )
    if rep["skipped_own_work"]:
        print(f"  {rep['skipped_own_work']} skipped as your own work", file=sys.stderr)

    if not a.check:
        return 0
    if not out:
        print("error: --check needs -o to write a file first", file=sys.stderr)
        return 2
    if rep["included"] == 0:
        # Running a checker over an empty file returns "clean", which is the
        # most dangerous possible answer here.
        print(
            "\nnot running ghostcite: the bibliography is empty, and a checker "
            "over an empty file reports success.",
            file=sys.stderr,
        )
        return 1
    res = bibtex.run_ghostcite(out)
    s = res.get("summary", {})
    print(
        f"\nghostcite: {s.get('total', 0)} entr(ies), {s.get('with_doi', 0)} with a "
        f"DOI, {s.get('findings', 0)} finding(s)"
    )
    for f in res.get("findings", []):
        print(f"  [{f.get('tier', '?')}] {f.get('key') or f.get('doi') or ''}")
        print(f"      {str(f.get('message', ''))[:150]}")
    return 1 if res.get("findings") else 0


def _is_pdf(path) -> bool:
    return str(path).lower().endswith(".pdf")


def cmd_audit(a) -> int:
    if _is_pdf(a.pptx):
        from .pdfdeck import audit as pdf_audit

        rep = pdf_audit(a.pptx, min_inches=a.min_inches)
        print(
            f"{rep['file']}: {rep['pictures']} image(s), {rep['tagged']} with provenance, "
            f"{rep['unconfirmed']} unconfirmed, {rep['untagged_substantive']} substantive "
            f"but unsourced"
        )
        for r in rep["rows"]:
            rec = r["record"]
            if rec is None:
                print(f"  page {r['page']:>3}  NO SOURCE   [{r['matched_by']}]")
            else:
                mark = "ok " if rec.confirmed else "UNC"
                label = (
                    rec.short_cite
                    or rec.doi
                    or rec.context_line()[:60]
                    or "(context only)"
                )
                print(f"  page {r['page']:>3}  {mark} {label}  [{r['matched_by']}]")
        return 0
    from .deck import audit

    rep = audit(a.pptx, min_inches=a.min_inches)
    print(
        f"{rep['pptx']}: {rep['pictures']} picture(s), {rep['tagged']} with provenance, "
        f"{rep['unconfirmed']} unconfirmed, {rep['untagged_substantive']} substantive but unsourced"
    )
    for r in rep["rows"]:
        rec = r["record"]
        tag = "decorative" if r["decorative"] else ""
        if rec is None:
            print(
                f"  slide {r['slide']:>3}  {r['shape'][:28]:28} NO SOURCE   {tag} [{r['matched_by']}]"
            )
        else:
            mark = "ok " if rec.confirmed else "UNC"
            print(
                f"  slide {r['slide']:>3}  {r['shape'][:28]:28} {mark} {rec.short_cite or rec.doi} [{r['matched_by']}]"
            )
    return 0


def cmd_apply(a) -> int:
    if _is_pdf(a.pptx):
        from .pdfdeck import apply as pdf_apply

        out = a.out or str(Path(a.pptx).with_suffix("")) + ".cited.pdf"
        man = (
            a.manifest
            if a.manifest
            else (None if a.no_manifest else str(Path(out).with_suffix("")))
        )
        rep = pdf_apply(
            a.pptx,
            out,
            captions=not a.no_captions,
            credits=not a.no_credits,
            caption_own_work=a.caption_own_work,
            manifest_path=man,
            allow_unconfirmed=a.allow_unconfirmed,
            min_inches=a.min_inches,
        )
        print(f"wrote {rep['out']}")
        print(
            f"  {rep['pictures']} image(s), {rep['cited']} credited, "
            f"{rep['unsourced']} unsourced"
        )
        if rep["manifest"]:
            print(f"  manifest: {rep['manifest']['csv']}")
        for e in rep["entries"]:
            print(f"  {e}")
        return 0
    from .deck import apply

    out = a.out or str(Path(a.pptx).with_suffix("")) + ".cited.pptx"
    man = a.manifest
    if man is None and not a.no_manifest:
        man = str(Path(out).with_suffix(""))
    rep = apply(
        a.pptx,
        out,
        captions=not a.no_captions,
        credits=not a.no_credits,
        caption_own_work=a.caption_own_work,
        manifest_path=man,
        allow_unconfirmed=a.allow_unconfirmed,
        min_inches=a.min_inches,
    )
    print(f"wrote {rep['out']}")
    print(
        f"  {rep['pictures']} picture(s), {rep['cited']} credited, {rep['unsourced']} unsourced"
    )
    if rep["manifest"]:
        print(f"  manifest: {rep['manifest']['csv']}")
        print(f"            {rep['manifest']['json']}")
    for e in rep["entries"]:
        print(f"  {e}")
    if rep["unsourced"]:
        print(
            f"  ⚠ {rep['unsourced']} image(s) had no recorded source; the credits "
            f"slide says so explicitly rather than hiding it"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="figcite",
        description="Keep DOI/citation provenance attached to images through to your slides.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tag", help="attach provenance to an existing image file")
    t.add_argument("image")
    t.add_argument("--doi")
    t.add_argument("--cite", help="freeform citation when there is no DOI")
    t.add_argument("--url")
    t.add_argument(
        "--adapted-from",
        help="DOI of the ORIGINAL source, if this figure was reproduced",
    )
    t.add_argument("--source-kind", default="download")
    t.add_argument("--note", default="")
    t.add_argument("-o", "--out")
    t.set_defaults(func=cmd_tag)

    g = sub.add_parser("grab", help="crop a figure out of a PDF, DOI attached")
    g.add_argument("pdf")
    g.add_argument("--page", type=int, required=True, help="1-based")
    g.add_argument(
        "--rect", help="x0,y0,x1,y1 in PDF points (or fractions with --frac)"
    )
    g.add_argument(
        "--frac", action="store_true", help="treat --rect as 0-1 fractions of the page"
    )
    g.add_argument(
        "--image-index", type=int, help="crop embedded image N (see `figcite images`)"
    )
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
    w.add_argument(
        "--no-auto-confirm",
        action="store_true",
        help="leave even GROUNDED captures pending instead of filing them",
    )
    w.set_defaults(func=cmd_watch)

    ui = sub.add_parser("ui", help="open the browser UI for pending captures and decks")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--open", action="store_true", help="open a browser window too")
    ui.set_defaults(func=cmd_ui)

    pe = sub.add_parser(
        "pending", help="list captured-but-unconfirmed clipboard images"
    )
    pe.set_defaults(func=cmd_pending)

    au = sub.add_parser(
        "autostart", help="keep the clipboard watcher running across logons"
    )
    ausub = au.add_subparsers(dest="action", required=True)
    ai = ausub.add_parser(
        "install", help="install the startup launcher (no admin needed)"
    )
    ai.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="watcher deadline; the launcher restarts it when this lapses",
    )
    ai.add_argument(
        "--no-start", action="store_true", help="install only; do not start it now"
    )
    ai.set_defaults(func=cmd_autostart_install)
    ast_ = ausub.add_parser("status", help="is the clipboard actually being watched?")
    ast_.set_defaults(func=cmd_autostart_status)
    aun = ausub.add_parser("uninstall", help="remove the launcher; captures are kept")
    aun.set_defaults(func=cmd_autostart_uninstall)

    c = sub.add_parser("confirm", help="attach a DOI to a pending capture")
    c.add_argument("index")
    c.add_argument("--doi")
    c.add_argument("--pick", type=int, help="accept candidate N from `figcite pending`")
    c.add_argument("--cite")
    c.add_argument(
        "--own-work",
        action="store_true",
        help="this figure is yours; cite it as 'This work'",
    )
    c.add_argument("--adapted-from")
    c.add_argument("--note", default="")
    c.add_argument("-o", "--out")
    c.set_defaults(func=cmd_confirm)

    rg = sub.add_parser(
        "register", help="record provenance for an existing image WITHOUT modifying it"
    )
    rg.add_argument("image")
    rg.add_argument("--doi")
    rg.add_argument("--cite")
    rg.add_argument("--url")
    rg.add_argument(
        "--this-work",
        action="store_true",
        help="your own figure; records the producing git commit",
    )
    rg.add_argument("--adapted-from")
    rg.add_argument("--source-kind", default="download")
    rg.add_argument("--note", default="")
    rg.set_defaults(func=cmd_register)

    r = sub.add_parser("resolve", help="show the citation + license for a DOI")
    r.add_argument("doi")
    r.set_defaults(func=cmd_resolve)

    s = sub.add_parser("search", help="find a DOI by title (candidates only)")
    s.add_argument("query")
    s.add_argument("--rows", type=int, default=5)
    s.set_defaults(func=cmd_search)

    z = sub.add_parser(
        "zotero", help="resolve captures against your Zotero library first"
    )
    zsub = z.add_subparsers(dest="zotero_cmd", required=True)
    zc = zsub.add_parser(
        "configure", help="store credentials 0600 so the watcher can read them"
    )
    zc.add_argument("--api-key", required=True)
    zc.add_argument("--library-id", required=True)
    zc.add_argument("--type", default="user", choices=["user", "group"])
    zc.set_defaults(func=cmd_zotero_configure)

    zs = zsub.add_parser("sync", help="refresh the local snapshot of the library")
    zs.set_defaults(func=cmd_zotero_sync)
    zt = zsub.add_parser(
        "status", help="is the library configured, and how much of it resolves?"
    )
    zt.set_defaults(func=cmd_zotero_status)
    zr = zsub.add_parser("resolve", help="look one title up in the library")
    zr.add_argument("title")
    zr.set_defaults(func=cmd_zotero_resolve)

    b = sub.add_parser(
        "bib",
        help="emit BibTeX for the works a deck's figures came from, for ghostcite",
    )
    b.add_argument(
        "source",
        nargs="?",
        help="a .pptx, a .pdf, an apply-manifest .json, or omit for the whole store",
    )
    b.add_argument("-o", "--out", help="write here (default: <source>.bib)")
    b.add_argument(
        "--include-unconfirmed",
        action="store_true",
        help="also emit machine-guessed figure-to-DOI links (off: they are the "
        "ghost citations this is meant to prevent, and no checker can catch them)",
    )
    b.add_argument(
        "--check",
        action="store_true",
        help="run ghostcite over the result; exit 1 if it finds anything",
    )
    b.set_defaults(func=cmd_bib)

    a = sub.add_parser("audit", help="report provenance coverage of a .pptx")
    a.add_argument("pptx")
    a.add_argument("--min-inches", type=float, default=1.0)
    a.set_defaults(func=cmd_audit)

    ap = sub.add_parser(
        "apply", help="write alt-text, captions, credits slide, manifest"
    )
    ap.add_argument("pptx")
    ap.add_argument("-o", "--out")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--no-credits", action="store_true")
    ap.add_argument(
        "--caption-own-work",
        action="store_true",
        help="also caption figures you generated (off: captions are for "
        "other people's figures)",
    )
    ap.add_argument("--manifest", help="path stem for the .csv/.json manifest")
    ap.add_argument("--no-manifest", action="store_true")
    ap.add_argument(
        "--allow-unconfirmed",
        action="store_true",
        help="print machine-guessed citations onto slides (off by default)",
    )
    ap.add_argument(
        "--min-inches",
        type=float,
        default=1.0,
        help="pictures smaller than this in both dimensions count as decorative",
    )
    ap.set_defaults(func=cmd_apply)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (LookupError, RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
