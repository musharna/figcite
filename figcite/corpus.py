"""The local figure index.

One row per figure of one paper, so that "which paper is this figure from?"
and "does this figure also appear elsewhere?" are two queries over one table.

Keyed by (pmcid, label) rather than a surrogate id because the build is
resumable and will re-visit articles it already holds: a natural key makes a
re-run overwrite instead of duplicating every figure.
"""

from __future__ import annotations

import io
import sqlite3
from dataclasses import dataclass, fields
from pathlib import Path

from PIL import Image

from . import pmc, store
from .match import DHASH_THRESHOLD
from .provenance import dhash_bytes

CORPUS_DIR = store.DATA_DIR / "corpus"
DB_PATH = CORPUS_DIR / "figures.sqlite"
IMAGE_DIR = CORPUS_DIR / "images"
DESCRIPTOR_DIR = CORPUS_DIR / "descriptors"

SCHEMA = """
CREATE TABLE IF NOT EXISTS figures (
    pmcid      TEXT NOT NULL,
    doi        TEXT NOT NULL,
    label      TEXT NOT NULL,
    caption    TEXT NOT NULL DEFAULT '',
    licence    TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    dhash      TEXT NOT NULL DEFAULT '',
    width      INTEGER NOT NULL DEFAULT 0,
    height     INTEGER NOT NULL DEFAULT 0,
    image_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (pmcid, label)
);
"""


@dataclass
class FigureRow:
    pmcid: str
    doi: str
    label: str
    caption: str
    licence: str
    source_url: str
    dhash: str
    width: int
    height: int
    image_path: str


def connect() -> sqlite3.Connection:
    """Open the index, creating it if this is the first run."""
    Path(CORPUS_DIR).mkdir(parents=True, exist_ok=True)
    Path(IMAGE_DIR).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


_COLS = [f.name for f in fields(FigureRow)]


def upsert(conn: sqlite3.Connection, row: FigureRow) -> None:
    placeholders = ",".join("?" for _ in _COLS)
    conn.execute(
        f"INSERT OR REPLACE INTO figures ({','.join(_COLS)}) VALUES ({placeholders})",
        [getattr(row, c) for c in _COLS],
    )
    conn.commit()


def all_rows(conn: sqlite3.Connection) -> list[FigureRow]:
    cur = conn.execute(f"SELECT {','.join(_COLS)} FROM figures")
    return [FigureRow(*r) for r in cur.fetchall()]


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM figures").fetchone()[0]


@dataclass
class BuildOutcome:
    doi: str
    pmcid: str
    status: str  # indexed | not-in-pmc | not-open-access | failed
    detail: str = ""


def _download(url: str) -> bytes:
    return pmc._get(url, headers={"User-Agent": pmc.USER_AGENT})


def build(dois: list[str], limit: int | None = None) -> list[BuildOutcome]:
    """Index the open-access subset of `dois`.

    EVERY doi produces an outcome, including the ones that cannot be indexed.
    A coverage count without the reasons is the number that hides the bug: you
    cannot tell "not in PMC" from "the fetch broke" from "I forgot to run it".

    One article's failure never stops the rest -- a build that aborts on the
    first bad article is not resumable in any useful sense.
    """
    conn = connect()
    try:
        result = pmc.lookup_dois(dois)
    except Exception as e:
        # A TOTAL failure -- DNS gone, or something unforeseen. Europe PMC's
        # /search endpoint 404'd every query mid-run once; letting that
        # propagate loses every outcome already gathered and reports a
        # traceback where the user needs a per-DOI status they can act on.
        # Per-REQUEST failures no longer arrive here: `lookup_dois` survives
        # them and names the DOIs it could not reach, because one 504 on
        # batch 3 of 67 used to mark all 535 DOIs failed.
        return [BuildOutcome(d, "", "failed", f"DOI lookup failed: {e}") for d in dois]
    found = {r.doi: r for r in result.records}
    outcomes: list[BuildOutcome] = []
    indexed_papers = 0

    for doi in dois:
        # Order matters: an unreachable DOI must never fall through to
        # "not-in-europe-pmc", which asserts we asked and it was not there.
        why = result.unreachable.get(doi.lower())
        if why:
            outcomes.append(BuildOutcome(doi, "", "failed", why))
            continue
        rec = found.get(doi.lower())
        if rec is None:
            outcomes.append(BuildOutcome(doi, "", "not-in-europe-pmc"))
            continue
        if not rec.pmcid:
            outcomes.append(BuildOutcome(doi, "", "no-pmc-copy"))
            continue
        if not rec.is_open_access:
            outcomes.append(BuildOutcome(doi, rec.pmcid, "not-open-access"))
            continue
        if limit is not None and indexed_papers >= limit:
            break
        try:
            figures = pmc.figures_of(rec.pmcid)
            urls = pmc.image_urls(rec.pmcid)
            licence = pmc.licence_of(rec.pmcid)
            for fig in figures:
                url = urls.get(fig.filename)
                if not url:
                    continue
                blob = _download(url)
                rel = f"{rec.pmcid}/{fig.filename}"
                dest = Path(IMAGE_DIR) / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(blob)
                # Compute once here rather than on every query: a query would
                # otherwise re-decode the whole corpus.
                write_descriptors(rel, blob)
                img = Image.open(io.BytesIO(blob))
                upsert(
                    conn,
                    FigureRow(
                        pmcid=rec.pmcid,
                        doi=rec.doi,
                        label=fig.label,
                        caption=fig.caption,
                        licence=licence,
                        source_url=url,
                        dhash=dhash_bytes(blob),
                        width=img.width,
                        height=img.height,
                        image_path=rel,
                    ),
                )
        except Exception as e:
            outcomes.append(BuildOutcome(doi, rec.pmcid, "failed", str(e)))
            continue
        indexed_papers += 1
        outcomes.append(BuildOutcome(doi, rec.pmcid, "indexed"))
    return outcomes


def status() -> dict:
    conn = connect()
    rows = all_rows(conn)
    return {
        "figures": len(rows),
        "papers": len({r.pmcid for r in rows}),
    }


def descriptor_path(rel: str) -> Path:
    return Path(DESCRIPTOR_DIR) / (rel + ".npz")


def write_descriptors(rel: str, blob: bytes) -> bool:
    """Cache what a query needs so it does not re-decode the whole corpus.

    Stores the descriptors AND the keypoint coordinates. Descriptors alone are
    not enough: RANSAC fits a homography from point pairs, and those points are
    not recoverable from the descriptors, so a descriptor-only cache would
    force every candidate back through a full decode anyway.

    Returns False when opencv is absent, or when the image is too smooth to
    yield features -- both are valid states, not errors.
    """
    from . import match

    if not match.opencv_available():
        return False
    import cv2
    import numpy as np

    img = match._decode(blob)
    if img is None:
        return False
    kp, desc = cv2.ORB_create(nfeatures=match.ORB_FEATURES).detectAndCompute(img, None)
    if desc is None or not kp:
        return False
    dest = descriptor_path(rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        str(dest),
        desc=desc,
        pts=np.float32([k.pt for k in kp]).reshape(-1, 2),
    )
    return True


def can_compare_dhash(dh: str) -> bool:
    """Whether a dhash carries enough signal to mean anything.

    A flat fill has no gradients at all, so its dhash is all zeros -- a red
    square and a blue square hash IDENTICALLY. Comparing such an image would
    report every other featureless figure as a duplicate of it, which is a
    confident and wrong accusation about someone's citation. An all-ones hash
    is the same degeneracy from the other end.

    Same rule as the ORB keypoint floor: refuse rather than guess. The
    predicate lives on the HASH rather than the bytes because that is what it
    was always really about, and because callers holding a stored hash (an
    audit row) must be able to apply the guard without re-reading the image.
    """
    if not dh:
        return False
    try:
        bits = bin(int(dh, 16)).count("1")
    except ValueError:
        return False
    return 0 < bits < 64


def can_compare(image_bytes: bytes) -> bool:
    """`can_compare_dhash` for a caller holding the image itself."""
    from .provenance import dhash_bytes

    return can_compare_dhash(dhash_bytes(image_bytes))


def duplicates_of_dhash(dh: str, credited_doi: str = "") -> list[FigureRow]:
    """Corpus figures matching this hash but carrying a different DOI.

    dhash only, deliberately. A figure republished elsewhere is normally the
    same image re-encoded or rescaled, which dhash sees exactly; ORB would also
    fire on a figure that merely CONTAINS a similar panel, and a false "you
    credited this wrongly" is far more damaging than a missed duplicate.

    Reporting is the whole job. Nothing here rewrites a record: a hit is a
    question for the user, not a correction.
    """
    from .provenance import hamming

    if not can_compare_dhash(dh):
        return []
    credited = (credited_doi or "").strip().lower()
    conn = connect()
    out = []
    for row in all_rows(conn):
        if credited and row.doi.strip().lower() == credited:
            continue
        if can_compare_dhash(row.dhash) and hamming(dh, row.dhash) <= DHASH_THRESHOLD:
            out.append(row)
    return out


def duplicates_of(image_bytes: bytes, credited_doi: str = "") -> list[FigureRow]:
    """`duplicates_of_dhash` for a caller holding the image itself."""
    from .provenance import dhash_bytes

    return duplicates_of_dhash(dhash_bytes(image_bytes), credited_doi)
