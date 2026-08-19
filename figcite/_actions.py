"""Record-building helpers shared by the CLI and the service layer.

Moved out of cli.py so the CLI and the service can both call the same code
for "build a Record" and "finalize it into the library" -- printing stays
with the CLI; this module never prints.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from . import store
from .crossref import normalize_doi, record_from_doi
from .provenance import Record, embed, now_stamps


def slug(s: str, n: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s or "").strip("-")[:n] or "image"


def library_dest(rec: Record, src: Path) -> Path:
    store._ensure()
    base = slug(rec.doi or rec.short_cite or src.stem)
    stamp = (rec.captured_local or "")[:19].replace(":", "").replace("-", "")
    return store.LIBRARY / f"{base}--{stamp or 'na'}{src.suffix.lower() or '.png'}"


def finalize(src: Path, rec: Record, out: Optional[str]) -> Path:
    dest = Path(out) if out else library_dest(rec, src)
    rec = embed(src, dest, rec)
    store.put(rec)
    return dest


def record_for(
    doi: Optional[str],
    cite: Optional[str],
    url: Optional[str],
    confirmed: bool,
    kind: str,
    detail: dict,
    adapted_from: Optional[str] = None,
    note: str = "",
) -> Record:
    if doi:
        rec = record_from_doi(
            doi, confirmed=confirmed, source_kind=kind, source_detail=detail
        )
    else:
        u, loc = now_stamps()
        rec = Record(
            citation=cite or "",
            short_cite=(cite or "")[:40],
            url=url,
            source_kind=kind,
            source_detail=detail,
            captured_utc=u,
            captured_local=loc,
            confirmed=bool(cite or url),
        )
    if adapted_from:
        rec.adapted_from = normalize_doi(adapted_from)
    if note:
        rec.note = note
    return rec
