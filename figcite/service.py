"""Actions, without a front end attached.

Both the CLI and the web UI call these. Nothing here prints, parses argv, or
knows about HTTP. The reason this module exists at all: two implementations of
"attach a citation to an image" is the one drift this codebase cannot survive.
"""

from __future__ import annotations

import io
import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from PIL import Image

from . import clipboard, corpus, deck, match, pdfdeck, session_tabs, store
from ._actions import finalize, record_for
from .provenance import Record, sidecar_path


class NotGrounded(Exception):
    """An inferred DOI was offered for confirmation without being named."""


class LibraryFileMissing(LookupError):
    """The manifest names a record whose image file is gone from the library.

    Deliberately NOT a KeyError: a KeyError out of `_resolve_ref_to_path`
    means "this ref names nothing real", which is the security property
    callers rely on. This is the opposite case -- the ref resolved to a
    genuine manifest record -- but the library and the manifest have drifted
    apart, which is a storage-integrity failure, not a bad address. Kept
    distinct so a caller (an HTTP handler, eventually) can tell "no such ref"
    apart from "this ref is real but its bytes are gone" instead of both
    collapsing into one catch, and so it never reaches a caller as a bare
    FileNotFoundError out of Image.open().
    """


_skipped: list[str] = []  # deferred for this process only; never persisted

# Round-4 finding I1. The web server is threaded, so "file this capture into
# the library" needs to be atomic against another thread doing the same thing.
# That used to be arranged one layer up, by `web.py` holding its module lock
# across the whole `/api/confirm` route -- but the CrossRef lookup happens
# INSIDE `confirm()`, so that lock spanned a 25s-timeout HTTP request plus a
# 429 retry that sleeps up to 15s and re-requests (~65s worst case, against a
# handler socket timeout of 30). A lock serializes SHARED-STATE MUTATION, not
# slow work; the shared state here is the library + manifest, and it is
# touched only in the final few milliseconds of a confirm.
#
# So the lock lives here, where the state is, and covers only the mutation:
# `finalize()` (embed + `store.put`'s manifest append) and `_clear_staged()`.
# The network round-trip that builds the Record runs OUTSIDE it. Deliberately
# NOT pushed further down into `store.put` itself: a `threading.Lock` there
# would imply the manifest is protected against concurrent writers in general,
# and it is not -- `figcite watch` is a separate PROCESS, which no in-process
# lock can serialize. Its scope is honestly "this process's threads".
_WRITE_LOCK = threading.RLock()


@dataclass
class PendingItem:
    ref: str  # opaque; "staged:<png name>" or "filed:<sha256>"
    kind: str  # "staged" | "filed"
    context: str  # app, window title, timestamp
    width: Optional[int] = None
    height: Optional[int] = None
    doi: Optional[str] = None
    doi_evidence: str = ""
    grounded: bool = False  # True => the DOI is evidence, not a guess
    candidates: list[dict] = field(default_factory=list)
    error: Optional[str] = None  # None + empty candidates == "looked, found nothing"
    note: str = ""  # why the capture is still unresolved, as figcite recorded it
    # The short name a human types: "m0" for filed, "0" for staged. Assigned
    # here, after the skip-ordering sort, because the CLI used to derive it by
    # enumerating its own re-filtered lists -- so the label the terminal prints
    # existed nowhere the browser could read, and the two front ends could
    # disagree about which item "m0" meant.
    cli_ref: str = ""


@dataclass
class ConfirmResult:
    """What confirming produced: the record, and where the image ended up.

    Ruling 6. `confirm()` used to return the bare Record, so every front end
    lost the destination path -- and with it the "insert THIS file into your
    deck" hint, which is the whole point of filing the image.
    """

    record: Record
    path: Optional[Path] = None  # where the image was filed; None for `filed:`
    # refs, whose bytes are already in the library


def pending_items() -> list[PendingItem]:
    out: list[PendingItem] = []

    for rec in store.all_records().values():
        if rec.confirmed or rec.source_kind != "clipboard":
            continue
        detail = rec.source_detail or {}
        cap = detail.get("clipboard_capture") or {}
        evidence = detail.get("doi_evidence", "") or ""
        # An auto-filed capture stores the same sentence in both `note` and
        # `doi_evidence`, so surfacing the evidence made the card print its own
        # failure reason twice, verbatim, one line apart. Nothing asserted on
        # it; it was visible only by looking at the rendered page.
        note = rec.note or ""
        if note.strip() == evidence.strip():
            note = ""
        out.append(
            PendingItem(
                ref=f"filed:{rec.sha256}",
                kind="filed",
                context=rec.context_line(),
                # All already on disk and previously dropped on the floor. The
                # card lays its thumbnail out from the dimensions, and the
                # evidence string is the user's only account of why nothing
                # resolved -- an inference improvement upstream is inert until
                # these are read back.
                width=cap.get("width"),
                height=cap.get("height"),
                doi_evidence=evidence,
                candidates=list(detail.get("candidates") or []),
                # `doi` and `grounded` are deliberately NOT read back. A filed
                # record is in this list precisely because no human accepted
                # it, so a stored grounded=True must not be able to become an
                # auto-confirm on the read path.
                error=None,
                note=note,
            )
        )

    for it in clipboard.list_pending():
        png = Path(it["png"])
        cap = it.get("capture", {}) or {}
        inf = it.get("inference", {}) or {}
        out.append(
            PendingItem(
                ref=f"staged:{png.name}",
                kind="staged",
                context=_context_of(cap),
                width=cap.get("width"),
                height=cap.get("height"),
                doi=inf.get("doi"),
                doi_evidence=inf.get("doi_evidence", "") or "",
                grounded=bool(inf.get("grounded")),
                candidates=list(inf.get("candidates") or []),
                error=inf.get("error"),
            )
        )
    out.sort(key=lambda i: i.ref in _skipped)
    # After the sort, because skipping moves an item to the back of the queue
    # and therefore renumbers everything behind it.
    filed_n = staged_n = 0
    for it in out:
        if it.kind == "filed":
            it.cli_ref = f"m{filed_n}"
            filed_n += 1
        else:
            it.cli_ref = str(staged_n)
            staged_n += 1
    return out


def _context_of(cap: dict[str, Any]) -> str:
    bits = [b for b in (cap.get("process"), cap.get("title"), cap.get("when")) if b]
    return " - ".join(str(b) for b in bits)


def skip(ref: str) -> None:
    # Check-then-act on a process-global list, so it is shared state like the
    # manifest is. Nothing here does I/O, so holding the lock costs nothing.
    with _WRITE_LOCK:
        if ref not in _skipped:
            _skipped.append(ref)


def confirm(
    ref,
    *,
    doi=None,
    pick=None,
    cite=None,
    own_work=False,
    adapted_from=None,
    note=None,
    out=None,
):
    # Controller Ruling 1: zero selectors is VALID and means "use this item's
    # own grounded DOI" -- the `figcite confirm 0` invocation cmd_pending prints
    # for a grounded capture. What guards a guess is the NotGrounded check
    # below, not an arity check up here. Requiring exactly one would both break
    # that invocation and make the NotGrounded branch unreachable dead code.
    selectors = [doi is not None, pick is not None, cite is not None, bool(own_work)]
    if sum(selectors) > 1:
        raise ValueError(
            "confirm takes at most one of doi=, pick=, cite=, own_work=True "
            f"(got {sum(selectors)})"
        )

    item = _item_for(ref)
    if item is None:
        raise KeyError(f"no pending item {ref!r}")

    if item.kind == "filed":
        return _confirm_filed(
            item,
            doi=doi,
            pick=pick,
            cite=cite,
            own_work=own_work,
            adapted_from=adapted_from,
            note=note,
        )

    raw = _raw_staged(ref)
    inf = raw.get("inference", {}) or {}

    if pick is not None:
        try:
            doi = inf["candidates"][pick]["doi"]
        except (KeyError, IndexError, TypeError):
            raise KeyError(f"no candidate {pick} on {ref}")
    elif own_work:
        cite = "This work"
    elif doi is None and cite is None:
        doi = inf.get("doi")
        if doi and not inf.get("grounded"):
            raise NotGrounded(
                "that DOI was only guessed; name it explicitly to accept it"
            )

    # Controller Ruling 4. cmd_confirm has TWO guards; Ruling 1 removed only the
    # arity one. Without this second check, zero selectors on an item with no
    # inferable DOI skips NotGrounded (because doi is FALSY, not because it is
    # grounded) and falls through to record_for(None, None, None) -- filing an
    # uncited record while _clear_staged deletes the pending.json holding the
    # candidates, the inference kind, and the error field. Irreversible.
    if doi is None and cite is None:
        raise ValueError(
            "nothing to confirm with: pass doi=, pick=, cite=, or own_work=True"
        )

    detail = {
        "clipboard_capture": raw.get("capture", {}),
        "inference_kind": inf.get("kind", ""),
        "doi_evidence": inf.get("doi_evidence", ""),
    }
    # I1: this is the slow part -- `record_for` reaches CrossRef for anything
    # with a DOI. It reads no shared state and writes none, so it runs before
    # the critical section rather than inside it.
    rec = record_for(
        doi,
        cite,
        None,
        confirmed=True,
        kind="clipboard",
        detail=detail,
        adapted_from=adapted_from,
        note=note or "",
    )
    png = Path(raw["png"])
    with _WRITE_LOCK:
        # Re-resolve under the lock. `raw` was read before a network call that
        # can take a minute; another thread confirming the same ref meanwhile
        # would already have filed this capture and deleted its pending.json,
        # and filing it twice means two library files and two manifest records
        # for one image. `_raw_staged` raises KeyError when that has happened
        # -- the same KeyError an unknown ref raises, which the CLI and the
        # web route already turn into "no such pending item".
        _raw_staged(ref)
        dest = finalize(png, rec, out)
        _clear_staged(png, dest)
    return ConfirmResult(record=rec, path=dest)


def _item_for(ref: str):
    for it in pending_items():
        if it.ref == ref:
            return it
    return None


def _raw_staged(ref: str) -> dict:
    """The original pending dict behind a staged ref."""
    name = ref.split(":", 1)[1]
    for raw in clipboard.list_pending():
        if Path(raw["png"]).name == name:
            return raw
    raise KeyError(f"no staged capture {ref!r}")


def _clear_staged(png: Path, dest: Path) -> None:
    """Exactly the deletions cmd_confirm performed, no more."""
    for suffix in (".pending.json", ".capture.json"):
        p = Path(str(png)[:-4] + suffix)
        if p.exists():
            p.unlink()
    if png.exists() and png != dest:
        png.unlink()


def _confirm_filed(
    item, *, doi, pick=None, cite=None, own_work=False, adapted_from, note
):
    """A capture already in the manifest, still unconfirmed.

    The image bytes are already filed, so there is nothing to finalize -- the
    manifest simply gains a citation for that sha. Everything that resolves a
    staged capture resolves one of these too: a typed DOI, a candidate chosen
    from the ones the item is displaying, a free-text citation, or "this is
    mine". Taking only a DOI meant a plot of your own, snipped with a capture
    tool, had no path through either front end -- and it made the web card hide
    a button rather than the service grow one.
    """
    if pick is not None:
        try:
            doi = item.candidates[pick]["doi"]
        except (IndexError, KeyError, TypeError):
            raise KeyError(f"no candidate {pick} on {item.ref}")
    elif own_work:
        cite = "This work"
    if not doi and not cite:
        raise ValueError("a filed capture needs doi=, pick=, cite=, or own_work=True")
    target = store.get(item.ref.split(":", 1)[1])
    if target is None:
        raise KeyError(f"no filed record {item.ref!r}")
    # I1, as in confirm(): the CrossRef lookup inside `record_for` runs before
    # the critical section, which covers only the manifest append.
    rec = record_for(
        doi,
        cite,
        None,
        confirmed=True,
        kind="clipboard",
        detail=target.source_detail,
        adapted_from=adapted_from,
        note=note or "",
    )
    rec.sha256, rec.dhash = target.sha256, target.dhash
    rec.captured_utc, rec.captured_local = target.captured_utc, target.captured_local
    with _WRITE_LOCK:
        store.put(rec)
    # No path: the bytes were already in the library, so nothing was filed here
    # and there is no new file for the caller to point the user at.
    return ConfirmResult(record=rec, path=None)


def thumbnail(ref: str, max_px: int = 480) -> tuple[bytes, str]:
    """PNG bytes for a small preview of whatever `ref` addresses.

    `ref` is the only addressing scheme a caller (eventually, a browser hitting
    an HTTP endpoint) gets: "staged:<png name>", "filed:<sha256>", or
    "sha:<sha256>". `_resolve_ref_to_path` is what makes that safe -- see its
    docstring. Raises KeyError for a ref naming no pending item or manifest
    record, and LibraryFileMissing when the manifest is right but the file
    backing it is gone from the library.
    """
    src = _resolve_ref_to_path(ref)
    im = Image.open(src)
    im.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="PNG")
    return buf.getvalue(), "image/png"


def _resolve_ref_to_path(ref: str) -> Path:
    """Look `ref` up and return the path THAT LOOKUP found -- never a path
    built out of `ref`'s own characters.

    This is the whole security property: a ref is opaque input from a
    browser, so nothing here may ever do `Path(...) / <bit of ref>`.
    - "staged:<name>" goes through `_raw_staged`, which already matches
      against `Path(raw["png"]).name` (a bare filename with no directory
      separators in it) rather than joining `name` onto a directory. A
      ref like "staged:../../../../etc/passwd" can never equal a `.name`,
      so the loop in `_raw_staged` runs out and raises KeyError before any
      filesystem path is touched.
    - "filed:<sha256>" and "sha:<sha256>" are looked up in
      `store.all_records()`, keyed by the sha itself -- a dict lookup, not
      a path join -- and then handed to `_library_path_for`, which finds
      the matching file by reading the sidecars `provenance.write_sidecar`
      already wrote (matching on the sidecar's own recorded sha256), again
      never by constructing a filename from the sha string.
    """
    if ref.startswith("staged:"):
        raw = _raw_staged(ref)  # KeyError for anything unknown
        return Path(raw["png"])

    if ref.startswith("filed:") or ref.startswith("sha:"):
        sha = ref.split(":", 1)[1]
        rec = store.all_records().get(sha)
        if rec is None:
            raise KeyError(f"no manifest record {ref!r}")
        try:
            return _library_path_for(rec.sha256)
        except LibraryFileMissing:
            # `figcite register` and the matplotlib hook record provenance for
            # an image WITHOUT copying it -- that is the documented point of
            # register -- so these records have no library file and never will.
            # Treating "not in the library" as "gone" made the deck screen
            # render "(image unavailable)" on every row for the figures the
            # user made themselves, while the files sat untouched at the path
            # the record itself stores.
            #
            # The path comes from our own manifest, not from the request: the
            # caller supplies only a sha256, so this cannot be steered.
            original = (rec.source_detail or {}).get("original_file")
            if original and Path(original).is_file():
                return Path(original)
            raise

    raise KeyError(f"unrecognized ref {ref!r}")


def _library_path_for(sha: str) -> Path:
    """Find the library file whose sidecar carries this sha256.

    Records don't store their own file path -- `_actions.library_dest`
    derives the on-disk filename from the record's DOI/citation, not from
    the hash -- so recovering it from `sha` alone would mean either
    recomputing that slug (fragile: it depends on the source file's
    original stem, which is gone once filed) or re-hashing every image in
    the library on every lookup. Reading the sidecars `provenance.embed`
    already writes next to each library file is a real, existing pointer:
    checking their own recorded sha256 field, not building a path out of
    `sha`.
    """
    if store.LIBRARY.exists():
        suffix = sidecar_path("")  # ".figcite.json", read from provenance.py
        # itself so the two never drift apart.
        for sidecar in sorted(store.LIBRARY.glob("*" + suffix)):
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("sha256") == sha:
                return Path(str(sidecar)[: -len(suffix)])
    raise LibraryFileMissing(
        f"manifest has a record for sha256={sha!r} but no library file carries it"
    )


def _is_pdf(path) -> bool:
    return str(path).lower().endswith(".pdf")


def audit(path, min_inches: float = 1.0) -> dict:
    """One report shape for a deck OR a PDF, so the UI needs none of its own.

    `deck.audit()` and `pdfdeck.audit()` disagree on their own key names
    (pptx/file, slide+shape/page+xref) -- this is where that disagreement
    ends. See `figcite/deck.py:audit` and `figcite/pdfdeck.py:audit`.
    """
    if _is_pdf(path):
        raw, kind = pdfdeck.audit(path, min_inches=min_inches), "pdf"

        def where(r):
            return f"page {r['page']}"

        def label(r):
            return f"xref {r['xref']}"
    else:
        raw, kind = deck.audit(path, min_inches=min_inches), "pptx"

        def where(r):
            return f"slide {r['slide']}"

        def label(r):
            return r["shape"]

    rows = []
    for r in raw["rows"]:
        rec = r["record"]
        if rec is None:
            status = "no-source"
        elif rec.confirmed:
            status = "ok"
        else:
            status = "unconfirmed"
        rows.append(
            {
                "ref": f"sha:{rec.sha256}" if rec is not None else "",
                "location": where(r),
                "label": label(r),
                "status": status,
                "matched_by": r["matched_by"],
                "decorative": r["decorative"],
                # Review fix I3: the JPEG-EXIF recovery path
                # (provenance.py's `read_embedded`) sets ONLY `citation` --
                # no `short_cite`, no `doi` -- so a figure carrying a
                # genuine embedded credit used to render an empty citation
                # cell here. Fix B's own defect class, one column over.
                "citation": (
                    (rec.short_cite or rec.doi or rec.citation or "") if rec else ""
                ),
                "doi": (rec.doi or "") if rec else "",
                "license_url": (rec.license_url or "") if rec else "",
                "reuse": (rec.reuse or "unknown") if rec else "",
                "retracted": bool(rec.retracted) if rec else False,
                # Review fix I5: lets the front end tell "own work" (no
                # third-party licence to ask about) apart from "genuinely
                # unclassified" -- both currently carry reuse=="unknown",
                # and only source_kind distinguishes them. Matches
                # deck.py's own `own_work = rec.source_kind == "generated"`.
                "source_kind": (rec.source_kind or "") if rec else "",
            }
        )
    return {
        "path": raw.get("pptx") or raw.get("file"),
        "kind": kind,
        "pictures": raw["pictures"],
        "tagged": raw["tagged"],
        "unconfirmed": raw["unconfirmed"],
        "untagged_substantive": raw["untagged_substantive"],
        "rows": rows,
    }


def apply(path, out=None, force: bool = False, **opts) -> dict:
    """Write provenance into a deck or PDF. Never honours a caller's
    allow_unconfirmed -- there is no path from the web UI to it, ever -- and
    never overwrites its own input.

    Round-2 web-security review. `out` staying a caller-chosen path (rather
    than being removed the way `confirm()`'s `out` was) is a deliberate,
    accepted design: producing an output file *is* what `apply` does, and the
    CLI's own `-o/--out` already accepts any path, so a local caller gains
    nothing new. The web route's `Origin`/`Host` checks are what keep this
    off-limits to a browser page that merely got the user to visit it. What
    they don't cover is a plain data-loss hazard for the ordinary same-origin
    user: an `out` that already names a real file. Two invariants close that,
    enforced here (a service-layer property, not a wire concern) rather than
    only in the route:
      - `out`'s suffix must match `path`'s -- a mismatched suffix is a signal
        of caller error, not something to "helpfully" honour.
      - an existing `out` is refused unless `force=True` is passed explicitly.

    Task-8 web-UI verification finding: `deck.apply()`/`pdfdeck.apply()`
    (the CLI's own consumers, which only ever print from the result -- see
    `cli.cmd_apply`) return `rows` with a live `Record` instance under
    `"record"` for every matched picture. This function used to return that
    dict unchanged. `web.py`'s `/api/apply` route feeds the result straight
    to `json.dumps()`, which cannot serialize a dataclass instance, so the
    route 500'd on every real deck (one with at least one matched figure) --
    exactly the case the existing test suite's only successful-apply test
    never exercised, because its fixture presentation has zero pictures and
    so `rows` is always `[]`. `audit()` above already normalizes its own
    `rows` for the same reason; `apply()` never had, until now.
    """
    opts.pop("allow_unconfirmed", None)  # no UI path to it, ever
    out = out or _default_out(path)
    out_path, in_path = Path(out), Path(path)
    if out_path.resolve() == in_path.resolve():
        raise ValueError("apply refuses to overwrite its input")
    if out_path.suffix.lower() != in_path.suffix.lower():
        raise ValueError(
            f"apply refuses an output suffix {out_path.suffix!r} that does "
            f"not match the input's {in_path.suffix!r}"
        )
    if out_path.exists() and not force:
        raise ValueError(f"{out} already exists; pass force=True to overwrite it")
    fn = pdfdeck.apply if _is_pdf(path) else deck.apply
    result = fn(path, out, allow_unconfirmed=False, **opts)
    result = dict(result)
    if "rows" in result:  # real deck.apply()/pdfdeck.apply() always set this;
        # guarded rather than assumed so a caller's own stand-in return value
        # (tests/test_service_deck.py's allow_unconfirmed-forwarding check
        # monkeypatches deck.apply to return {}) isn't required to shape
        # itself around a normalization step it has nothing to do with.
        result["rows"] = [
            {**r, "record": (asdict(r["record"]) if r["record"] is not None else None)}
            for r in result["rows"]
        ]
    return result


def _default_out(path) -> str:
    p = Path(path)
    return str(p.with_name(p.stem + ".cited" + p.suffix))


def whereis(ref_or_path) -> dict:
    """Where might this figure have come from?

    dhash first (free), ORB second (handles crops), open tabs last. The verdict
    distinguishes "searched and found nothing" from "could not look", because
    they license different next actions: the first means the figure is not in
    your corpus, the second means you learned nothing at all.
    """
    path = Path(ref_or_path)
    if not path.exists():
        path = _resolve_ref_to_path(str(ref_or_path))
    blob = path.read_bytes()

    conn = corpus.connect()
    rows = corpus.all_rows(conn)

    verdict = match.by_dhash(blob, rows)
    # With nothing indexed there is no second opinion to seek, and running ORB
    # over zero rows only appends "no corpus figure could be read", which reads
    # as "your files are broken" rather than "you have not built it yet".
    if not rows:
        return {"verdict": "could-not-decide", "matches": [],
                "reason": getattr(verdict, "reason", "the corpus is empty")}
    if not isinstance(verdict, match.Match):
        orb = match.by_orb(blob, rows, corpus.IMAGE_DIR, corpus.DESCRIPTOR_DIR)
        # An ORB NoMatch is a real search of the corpus, so it outranks dhash's
        # could-not-decide -- which only ever meant "a crop is invisible to me".
        if isinstance(orb, (match.Match, match.NoMatch)):
            verdict = orb
        elif isinstance(verdict, match.CouldNotDecide):
            verdict = match.CouldNotDecide(f"{verdict.reason}; {orb.reason}")

    matches: list[dict] = []
    if isinstance(verdict, match.Match):
        row = next(
            (r for r in rows
             if getattr(r, "pmcid", None) == verdict.pmcid
             and getattr(r, "label", None) == verdict.label),
            None,
        )
        matches.append({
            "source": verdict.method,
            "score": round(verdict.score, 1),
            "doi": verdict.doi,
            "title": (getattr(row, "caption", "")[:120] if row else verdict.label),
            "container": verdict.pmcid,
            "year": "",
            "type": "figure",
        })

    # Open tabs are a garnish: a lead about what you were reading, never
    # evidence. A failure to read them must not lose a real pixel match.
    try:
        matches.extend(session_tabs.tab_candidates())
    except Exception:
        pass

    if isinstance(verdict, match.Match):
        name, reason = "match", ""
    elif isinstance(verdict, match.NoMatch):
        name, reason = "no-match", ""
    else:
        name, reason = "could-not-decide", verdict.reason
    return {"verdict": name, "matches": matches, "reason": reason}


def duplicates(ref_or_path, credited_doi: str = "") -> dict:
    """Has this figure appeared in a paper other than the one credited?

    Reports only. Nothing here rewrites a record: a hit is a question for the
    user, not a correction to apply on their behalf.

    `reason` carries WHY the answer is empty when it is, because "no other
    paper has this figure" and "this image has no gradient for the hash to
    work with" are different facts and only one of them is about the figure.
    """
    path = Path(ref_or_path)
    if not path.exists():
        path = _resolve_ref_to_path(str(ref_or_path))
    blob = path.read_bytes()

    if not corpus.can_compare(blob):
        return {
            "others": [],
            "reason": "this image has too little gradient to compare -- a flat "
            "fill hashes the same as any other flat fill, so a match would "
            "mean nothing",
        }

    rows = corpus.duplicates_of(blob, credited_doi)
    return {
        "others": [
            {"doi": r.doi, "pmcid": r.pmcid, "label": r.label, "licence": r.licence}
            for r in rows
        ],
        "reason": "" if rows else "no other indexed paper carries this figure",
    }
