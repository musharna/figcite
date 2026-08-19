"""The local figure index.

One row per figure of one paper, so that "which paper is this figure from?"
and "does this figure also appear elsewhere?" are two queries over one table.

Keyed by (pmcid, label) rather than a surrogate id because the build is
resumable and will re-visit articles it already holds: a natural key makes a
re-run overwrite instead of duplicating every figure.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from pathlib import Path

from . import store

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
