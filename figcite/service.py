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
    return out


def _context_of(cap: dict[str, Any]) -> str:
    bits = [b for b in (cap.get("process"), cap.get("title"), cap.get("when")) if b]
    return " - ".join(str(b) for b in bits)
