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
from .provenance import dhash_bytes

CORPUS_DIR = store.DATA_DIR / "corpus"
DB_PATH = CORPUS_DIR / "figures.sqlite"
IMAGE_DIR = CORPUS_DIR / "images"

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
        records = pmc.lookup_dois(dois)
    except Exception as e:
        # An upstream outage is exactly the case a resumable build exists for.
        # Europe PMC's /search endpoint 404'd every query mid-run once; letting
        # that propagate loses every outcome already gathered and reports a
        # traceback where the user needs a per-DOI status they can act on.
        return [BuildOutcome(d, "", "failed", f"DOI lookup failed: {e}") for d in dois]
    found = {r.doi: r for r in records}
    outcomes: list[BuildOutcome] = []
    indexed_papers = 0

    for doi in dois:
        rec = found.get(doi.lower())
        if rec is None:
            outcomes.append(BuildOutcome(doi, "", "not-in-pmc"))
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
