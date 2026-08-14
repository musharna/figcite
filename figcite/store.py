"""Central sha256-keyed manifest. Append-only JSONL, last write wins."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator, Optional

from .provenance import Record, embed, hamming

DATA_DIR = Path(os.environ.get("FIGCITE_HOME", Path.home() / ".local" / "share" / "figcite"))
MANIFEST = DATA_DIR / "manifest.jsonl"
STAGING = DATA_DIR / "staging"
LIBRARY = Path(os.environ.get("FIGCITE_LIBRARY", DATA_DIR / "library"))


def _ensure() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STAGING.mkdir(parents=True, exist_ok=True)
    LIBRARY.mkdir(parents=True, exist_ok=True)


def put(rec: Record) -> None:
    if not rec.sha256:
        raise ValueError("refusing to store a record with no sha256")
    _ensure()
    with open(MANIFEST, "a", encoding="utf-8") as fh:
        fh.write(rec.to_json() + "\n")


def iter_records() -> Iterator[Record]:
    if not MANIFEST.exists():
        return
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield Record.from_dict(json.loads(line))
        except Exception:
            continue


def all_records() -> dict[str, Record]:
    """sha256 -> newest record."""
    out: dict[str, Record] = {}
    for r in iter_records():
        out[r.sha256] = r
    return out


def get(sha: str) -> Optional[Record]:
    return all_records().get(sha)


def find_similar(dh: str, max_distance: int = 6) -> Optional[tuple[Record, int]]:
    """Perceptual fallback for images PowerPoint has re-encoded or rescaled."""
    best, best_d = None, 999
    for r in all_records().values():
        d = hamming(dh, r.dhash)
        if d < best_d:
            best, best_d = r, d
    if best is not None and best_d <= max_distance:
        return best, best_d
    return None


def finalize_into_library(src, rec: Record, out=None) -> Path:
    """Embed the record into the image and register it. Returns the final path.

    Shared by `figcite tag/grab/confirm` and by the watcher's auto-confirm path
    so there is exactly one definition of "tagged and filed".
    """
    import re as _re
    from pathlib import Path as _P
    src = _P(src)
    _ensure()
    if out:
        dest = _P(out)
    else:
        base = _re.sub(r"[^A-Za-z0-9._-]+", "-", rec.doi or rec.short_cite or src.stem).strip("-")[:60]
        stamp = (rec.captured_local or "")[:19].replace(":", "").replace("-", "")
        dest = LIBRARY / f"{base or 'image'}--{stamp or 'na'}{src.suffix.lower() or '.png'}"
    rec2 = embed(src, dest, rec)
    put(rec2)
    return dest


def register_existing(path, rec: Record) -> Record:
    """Record provenance for a file WITHOUT rewriting it.

    Retro-fitting an existing deck must not modify figures that live in someone
    else's project tree. The file is hashed as-is and the record is stored under
    those hashes, so a deck containing that exact image still matches by sha256,
    and a re-encoded copy still matches perceptually.
    """
    from pathlib import Path as _P
    from .provenance import dhash_bytes, sha256_bytes
    blob = _P(path).read_bytes()
    rec.sha256 = sha256_bytes(blob)
    rec.dhash = dhash_bytes(blob)
    put(rec)
    return rec
