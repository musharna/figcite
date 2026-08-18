"""Actions, without a front end attached.

Both the CLI and the web UI call these. Nothing here prints, parses argv, or
knows about HTTP. The reason this module exists at all: two implementations of
"attach a citation to an image" is the one drift this codebase cannot survive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import clipboard, store
from ._actions import finalize, record_for


class NotGrounded(Exception):
    """An inferred DOI was offered for confirmation without being named."""


_skipped: list[str] = []  # deferred for this process only; never persisted


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


def pending_items() -> list[PendingItem]:
    out: list[PendingItem] = []

    for rec in store.all_records().values():
        if rec.confirmed or rec.source_kind != "clipboard":
            continue
        out.append(
            PendingItem(
                ref=f"filed:{rec.sha256}",
                kind="filed",
                context=rec.context_line(),
                error=None,
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
    return out


def _context_of(cap: dict[str, Any]) -> str:
    bits = [b for b in (cap.get("process"), cap.get("title"), cap.get("when")) if b]
    return " - ".join(str(b) for b in bits)


def skip(ref: str) -> None:
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
        return _confirm_filed(item, doi=doi, adapted_from=adapted_from, note=note)

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
    dest = finalize(png, rec, out)
    _clear_staged(png, dest)
    return rec


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


def _confirm_filed(item, *, doi, adapted_from, note):
    """A capture already in the manifest, still unconfirmed.

    Only a DOI can resolve one: the image bytes are already filed, so there is
    nothing to finalize -- the manifest simply gains a citation for that sha.
    """
    if not doi:
        raise ValueError("a filed capture can only be resolved with doi=")
    target = store.get(item.ref.split(":", 1)[1])
    if target is None:
        raise KeyError(f"no filed record {item.ref!r}")
    rec = record_for(
        doi,
        None,
        None,
        confirmed=True,
        kind="clipboard",
        detail=target.source_detail,
        adapted_from=adapted_from,
        note=note or "",
    )
    rec.sha256, rec.dhash = target.sha256, target.dhash
    rec.captured_utc, rec.captured_local = target.captured_utc, target.captured_local
    store.put(rec)
    return rec
