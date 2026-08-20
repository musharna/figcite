# figcite Figure Corpus (SP2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer "where did this figure come from?" and "has this figure appeared elsewhere?" from one local index of figures belonging to the user's own Zotero papers.

**Architecture:** `pmc.py` fetches (Europe PMC for DOI→PMCID, PMC for figure images), `corpus.py` owns a SQLite index plus image files under `FIGCITE_HOME/corpus/`, `match.py` compares image bytes with dhash then optionally ORB+RANSAC. `service.whereis()` is the single entry point both front ends call, exactly as `service.confirm()` is today.

**Tech Stack:** Python 3.10+, stdlib `sqlite3`, existing deps (pillow, requests, PyMuPDF), optional `opencv-python-headless` + numpy for crop matching.

**Spec:** `docs/superpowers/specs/2026-08-19-figcite-figure-corpus-design.md`

## Global Constraints

- **The three outcomes never collapse:** `Match` / `NoMatch` / `CouldNotDecide` are distinct in every layer including the CLI and the web card. `NoMatch` means searched-and-found-nothing; `CouldNotDecide` means could-not-look.
- **Nothing auto-confirms.** Every match is a candidate a human accepts.
- **opencv is optional.** Base install stays at 4 dependencies. When opencv is absent, a query dhash cannot settle returns `CouldNotDecide`, never `NoMatch` — a crop is invisible to dhash, so "dhash says no" is not evidence of absence.
- **No PMC figure images committed to the repo.** They are copyrighted; one probed article is CC BY-NC-ND. Deterministic tests use synthetic images.
- **Every retrieval test ships decoys** and asserts the margin over the runner-up, not just the top hit. A two-candidate test cannot fail.
- **Politeness:** ≤3 requests/second to NCBI hosts, single-threaded.
- **dhash threshold is 6**, matching `store.find_similar`'s default and `deck.py:93`.
- Existing tests must pass unmodified. If one needs changing, the change altered behaviour — stop and report.

**Known-good interfaces (verified against the tree at `57bedb2`):**

- `figcite.provenance.dhash_bytes(b: bytes, size: int = 8) -> str` (hex)
- `figcite.provenance.hamming(a: str, b: str) -> int` (999 when unusable)
- `figcite.store.DATA_DIR: Path` — honours `FIGCITE_HOME`
- `figcite.zotero.library(max_age_hours=None) -> list[dict]`
- `figcite.zotero._doi_of(item: dict) -> tuple[str, str]` — `(doi, how)`, doi `""` when absent
- pytest marker `live` is declared in `pytest.ini`

---

### Task 1: Corpus storage skeleton

**Files:**

- Create: `figcite/corpus.py`
- Test: `tests/test_corpus_store.py`

**Interfaces:**

- Consumes: `store.DATA_DIR`
- Produces:
  - `CORPUS_DIR: Path`, `DB_PATH: Path`, `IMAGE_DIR: Path`
  - `connect() -> sqlite3.Connection` (creates schema if absent)
  - `FigureRow` dataclass: `pmcid, doi, label, caption, licence, source_url, dhash, width, height, image_path`
  - `upsert(conn, row: FigureRow) -> None`
  - `all_rows(conn) -> list[FigureRow]`
  - `count(conn) -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus_store.py
"""The corpus index: one row per figure, addressed by (pmcid, label)."""
import os
from pathlib import Path

import pytest

from figcite import corpus


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "CORPUS_DIR", tmp_path / "corpus")
    monkeypatch.setattr(corpus, "DB_PATH", tmp_path / "corpus" / "figures.sqlite")
    monkeypatch.setattr(corpus, "IMAGE_DIR", tmp_path / "corpus" / "images")
    conn = corpus.connect()
    yield conn
    conn.close()


def _row(**over):
    base = dict(
        pmcid="PMC1", doi="10.1/a", label="Figure 1", caption="A caption",
        licence="CC BY", source_url="https://example.org/f1.jpg",
        dhash="0011223344556677", width=100, height=80,
        image_path="PMC1/f1.jpg",
    )
    base.update(over)
    return corpus.FigureRow(**base)


def test_a_row_round_trips(db):
    corpus.upsert(db, _row())
    rows = corpus.all_rows(db)
    assert len(rows) == 1
    assert rows[0].doi == "10.1/a"
    assert rows[0].caption == "A caption"


def test_upsert_is_idempotent_on_pmcid_and_label(db):
    """A re-run of the build must not duplicate every figure."""
    corpus.upsert(db, _row(caption="first"))
    corpus.upsert(db, _row(caption="second"))
    rows = corpus.all_rows(db)
    assert corpus.count(db) == 1
    assert rows[0].caption == "second", "upsert did not overwrite"


def test_two_figures_of_the_same_paper_are_distinct_rows(db):
    corpus.upsert(db, _row(label="Figure 1"))
    corpus.upsert(db, _row(label="Figure 2"))
    assert corpus.count(db) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_corpus_store.py -q`
Expected: FAIL with `ImportError: cannot import name 'corpus'`

- [ ] **Step 3: Write minimal implementation**

```python
# figcite/corpus.py
"""The local figure index.

One row per figure of one paper. Keyed by (pmcid, label) rather than a
surrogate id so a re-run of the build overwrites rather than duplicating --
the build is resumable and will re-visit articles it already has.
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
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_corpus_store.py -q`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add figcite/corpus.py tests/test_corpus_store.py
git commit -m "feat(corpus): sqlite index keyed by (pmcid, label)"
```

---

### Task 2: The matcher — dhash stage

**Files:**

- Create: `figcite/match.py`
- Test: `tests/test_match_dhash.py`

**Interfaces:**

- Consumes: `provenance.dhash_bytes`, `provenance.hamming`
- Produces:
  - `Match(doi, pmcid, label, method, score, margin)` dataclass
  - `NoMatch()` dataclass
  - `CouldNotDecide(reason: str)` dataclass
  - `Verdict = Match | NoMatch | CouldNotDecide`
  - `DHASH_THRESHOLD: int = 6`
  - `by_dhash(query_bytes: bytes, rows: list) -> Verdict` — `rows` are objects with `.dhash`, `.doi`, `.pmcid`, `.label`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_match_dhash.py
"""dhash stage: exact and rescaled hits only.

Measured on a real figure against threshold 6: re-save 0, rescale-to-50% 0,
crop-5%-each-edge 7, crop-10% 16. So dhash MUST NOT report NoMatch on a miss
-- a crop is invisible to it, and calling that 'no match' asserts an absence
it cannot see.
"""
import io

from PIL import Image

from figcite import match
from figcite.provenance import dhash_bytes


def _png(img):
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def _noise(seed, size=(64, 64)):
    import random
    rnd = random.Random(seed)
    im = Image.new("RGB", size)
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                for _ in range(size[0] * size[1])])
    return im


class Row:
    def __init__(self, dh, doi):
        self.dhash, self.doi, self.pmcid, self.label = dh, doi, "PMC1", "Figure 1"


def test_an_identical_image_is_matched():
    img = _noise(1)
    rows = [Row(dhash_bytes(_png(img)), "10.1/right"),
            Row(dhash_bytes(_png(_noise(2))), "10.1/wrong")]
    v = match.by_dhash(_png(img), rows)
    assert isinstance(v, match.Match)
    assert v.doi == "10.1/right"
    assert v.method == "dhash"


def test_a_rescaled_image_is_matched():
    img = _noise(3, (128, 128))
    rows = [Row(dhash_bytes(_png(img)), "10.1/right")]
    small = img.resize((64, 64))
    v = match.by_dhash(_png(small), rows)
    assert isinstance(v, match.Match), "dhash is scale-invariant; this must hit"


def test_a_miss_is_could_not_decide_not_no_match():
    """The rule this stage exists to respect."""
    rows = [Row(dhash_bytes(_png(_noise(4))), "10.1/other")]
    v = match.by_dhash(_png(_noise(5)), rows)
    assert isinstance(v, match.CouldNotDecide)
    assert not isinstance(v, match.NoMatch)
    assert "crop" in v.reason.lower()


def test_an_empty_corpus_is_could_not_decide():
    v = match.by_dhash(_png(_noise(6)), [])
    assert isinstance(v, match.CouldNotDecide)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_match_dhash.py -q`
Expected: FAIL with `ImportError: cannot import name 'match'`

- [ ] **Step 3: Write minimal implementation**

```python
# figcite/match.py
"""Comparing one image against corpus figures.

Two stages. dhash is free and exact-ish; ORB (Task 3) handles crops. Neither
ever returns a bare boolean: the caller must be able to tell 'searched and
found nothing' from 'could not look', because they license different actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .provenance import dhash_bytes, hamming

DHASH_THRESHOLD = 6


@dataclass
class Match:
    doi: str
    pmcid: str
    label: str
    method: str
    score: float
    margin: float


@dataclass
class NoMatch:
    pass


@dataclass
class CouldNotDecide:
    reason: str


Verdict = Union[Match, NoMatch, CouldNotDecide]


def by_dhash(query_bytes: bytes, rows) -> Verdict:
    """Nearest corpus figure by perceptual hash.

    A miss is CouldNotDecide, never NoMatch: dhash cannot see a crop at all
    (measured, hamming 7 for 5% off each edge against a threshold of 6), so
    its silence is not evidence the figure is absent.
    """
    if not rows:
        return CouldNotDecide("the corpus is empty -- run `figcite corpus build`")

    dh = dhash_bytes(query_bytes)
    scored = sorted(((hamming(dh, r.dhash), r) for r in rows), key=lambda t: t[0])
    best_d, best = scored[0]
    if best_d > DHASH_THRESHOLD:
        return CouldNotDecide(
            "no perceptual-hash hit; dhash cannot see a crop, so this is not "
            "evidence the figure is absent"
        )
    runner_up = scored[1][0] if len(scored) > 1 else 64
    return Match(
        doi=best.doi, pmcid=best.pmcid, label=best.label,
        method="dhash", score=float(best_d), margin=float(runner_up - best_d),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_match_dhash.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add figcite/match.py tests/test_match_dhash.py
git commit -m "feat(match): dhash stage, whose miss is could-not-decide"
```

---

### Task 3: The matcher — ORB stage, optional dependency

**Files:**

- Modify: `figcite/match.py`
- Modify: `pyproject.toml`
- Test: `tests/test_match_orb.py`

**Interfaces:**

- Consumes: Task 2's `Match`/`NoMatch`/`CouldNotDecide`
- Produces:
  - `opencv_available() -> bool`
  - `MIN_KEYPOINTS: int = 25`, `MIN_INLIERS: int = 15`, `MIN_MARGIN: float = 3.0`
  - `by_orb(query_bytes: bytes, rows, image_root) -> Verdict` — `rows` need `.image_path` relative to `image_root`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_match_orb.py
"""ORB stage: the one that handles crops.

Measured on real figures with decoys from three unrelated papers: 7/7 correct
at a median 38.9x margin. Measured WITHOUT decoys the same technique scored
283x on a two-candidate test and 1.5x on a real retrieval task -- so every
test here carries decoys and asserts the margin.
"""
import io
import random

import pytest
from PIL import Image, ImageDraw

from figcite import match


def _textured(seed, size=(320, 320)):
    """A high-feature synthetic figure. ORB needs corners; flat fills give none."""
    rnd = random.Random(seed)
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    for _ in range(90):
        x, y = rnd.randrange(size[0] - 40), rnd.randrange(size[1] - 40)
        w, h = rnd.randrange(8, 38), rnd.randrange(8, 38)
        col = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
        (d.rectangle if rnd.random() < 0.5 else d.ellipse)([x, y, x + w, y + h], fill=col)
    return im


def _write(img, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


class Row:
    def __init__(self, image_path, doi):
        self.image_path, self.doi = image_path, doi
        self.pmcid, self.label = "PMC1", "Figure 1"


def _png(img):
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


pytestmark = pytest.mark.skipif(
    not match.opencv_available(), reason="opencv not installed"
)


def test_a_panel_crop_finds_its_source_among_decoys(tmp_path):
    truth = _textured(11)
    _write(truth, tmp_path / "truth.png")
    rows = [Row("truth.png", "10.1/right")]
    for i in range(4):
        _write(_textured(100 + i), tmp_path / f"decoy{i}.png")
        rows.append(Row(f"decoy{i}.png", f"10.1/decoy{i}"))

    w, h = truth.size
    crop = truth.crop((0, 0, int(w * 0.55), int(h * 0.55)))

    v = match.by_orb(_png(crop), rows, tmp_path)
    assert isinstance(v, match.Match), v
    assert v.doi == "10.1/right"
    assert v.margin >= match.MIN_MARGIN, f"margin {v.margin} too thin to assert a source"


def test_a_figure_that_is_in_no_paper_is_a_real_no_match(tmp_path):
    """The positive control for the test above: NoMatch must be reachable."""
    rows = []
    for i in range(4):
        _write(_textured(200 + i), tmp_path / f"decoy{i}.png")
        rows.append(Row(f"decoy{i}.png", f"10.1/decoy{i}"))

    v = match.by_orb(_png(_textured(999)), rows, tmp_path)
    assert isinstance(v, match.NoMatch), v


def test_a_featureless_query_is_could_not_decide(tmp_path):
    """Measured on a real figure: a smooth panel yielded 2 keypoints.

    Reporting that as NoMatch would assert an absence nothing observed.
    """
    for i in range(3):
        _write(_textured(300 + i), tmp_path / f"decoy{i}.png")
    rows = [Row(f"decoy{i}.png", f"10.1/d{i}") for i in range(3)]

    blank = Image.new("RGB", (300, 300), "white")
    v = match.by_orb(_png(blank), rows, tmp_path)
    assert isinstance(v, match.CouldNotDecide)
    assert "feature" in v.reason.lower()


def test_without_opencv_the_verdict_is_could_not_decide(tmp_path, monkeypatch):
    """A silent degrade to NoMatch is indistinguishable from a real negative."""
    monkeypatch.setattr(match, "opencv_available", lambda: False)
    v = match.by_orb(_png(_textured(1)), [], tmp_path)
    assert isinstance(v, match.CouldNotDecide)
    assert "opencv" in v.reason.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_match_orb.py -q`
Expected: FAIL with `AttributeError: module 'figcite.match' has no attribute 'opencv_available'`

- [ ] **Step 3: Write minimal implementation**

Append to `figcite/match.py`:

```python
MIN_KEYPOINTS = 25
MIN_INLIERS = 15
MIN_MARGIN = 3.0


def opencv_available() -> bool:
    try:
        import cv2  # noqa: F401
    except Exception:
        return False
    return True


def _decode(data: bytes):
    import cv2
    import numpy as np

    arr = np.frombuffer(data, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)


def by_orb(query_bytes: bytes, rows, image_root) -> Verdict:
    """Sub-image search. Scored by RANSAC INLIERS, not raw match counts.

    Raw match counts do not discriminate: on text-heavy inputs they ranked an
    unrelated document's page level with the truth (1.5x). Inliers separated
    the same cases by a median 38.9x, because spurious matches are not
    geometrically consistent.
    """
    if not opencv_available():
        return CouldNotDecide(
            "opencv is not installed, so a cropped figure cannot be matched; "
            "install figcite[match] to enable it"
        )

    import cv2
    import numpy as np
    from pathlib import Path

    query = _decode(query_bytes)
    if query is None:
        return CouldNotDecide("the query image could not be decoded")

    orb = cv2.ORB_create(nfeatures=1500)
    kq, dq = orb.detectAndCompute(query, None)
    if dq is None or len(kq) < MIN_KEYPOINTS:
        return CouldNotDecide(
            f"only {0 if dq is None else len(kq)} visual features in this image "
            f"(need {MIN_KEYPOINTS}); it is too smooth to identify"
        )

    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    scored = []
    for row in rows:
        img = cv2.imread(str(Path(image_root) / row.image_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        k, d = orb.detectAndCompute(img, None)
        if d is None or len(d) < 10:
            continue
        pairs = [p for p in bf.knnMatch(dq, d, k=2) if len(p) == 2]
        good = [m for m, s in pairs if m.distance < 0.75 * s.distance]
        inliers = 0
        if len(good) >= 8:
            src = np.float32([kq[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
            dst = np.float32([k[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
            _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
            inliers = int(mask.sum()) if mask is not None else 0
        scored.append((inliers, row))

    if not scored:
        return CouldNotDecide("no corpus figure could be read for comparison")

    scored.sort(key=lambda t: t[0], reverse=True)
    best_n, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0
    if best_n < MIN_INLIERS:
        return NoMatch()
    margin = best_n / max(second, 1)
    if margin < MIN_MARGIN:
        return CouldNotDecide(
            f"the two best candidates are too close to call ({best_n} vs {second})"
        )
    return Match(
        doi=best.doi, pmcid=best.pmcid, label=best.label,
        method="orb", score=float(best_n), margin=float(margin),
    )
```

Add to `pyproject.toml` after the `dependencies` block:

```toml
[project.optional-dependencies]
match = ["opencv-python-headless>=4.8", "numpy>=1.24"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_match_orb.py -q`
Expected: 4 passed (or 4 skipped if opencv is genuinely absent — verify with `python -c "import cv2"`)

- [ ] **Step 5: Commit**

```bash
git add figcite/match.py tests/test_match_orb.py pyproject.toml
git commit -m "feat(match): ORB+RANSAC crop matching behind an optional dep"
```

---

### Task 4: Europe PMC lookup — DOI to PMCID

**Files:**

- Create: `figcite/pmc.py`
- Test: `tests/test_pmc_lookup.py`

**Interfaces:**

- Consumes: nothing from earlier tasks
- Produces:
  - `EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"`
  - `PmcRecord` dataclass: `doi, pmcid, title, year, is_open_access: bool`
  - `lookup_dois(dois: list[str], batch: int = 8) -> list[PmcRecord]`
  - `DnsUnreachable(RuntimeError)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pmc_lookup.py
"""DOI -> PMCID via Europe PMC.

Batched because a real library has hundreds of DOIs and the API takes an OR
query. Network is blocked by conftest for non-live tests, so these drive a
stubbed transport; one live test elsewhere covers the real boundary.
"""
import json

import pytest

from figcite import pmc


def _fake_response(results):
    return json.dumps({"resultList": {"result": results}}).encode()


def test_a_doi_that_is_open_access_is_reported_as_such(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _fake_response([
            {"doi": "10.1/aa", "pmcid": "PMC1", "title": "A", "pubYear": "2020",
             "isOpenAccess": "Y"},
        ])

    monkeypatch.setattr(pmc, "_get", fake_get)
    out = pmc.lookup_dois(["10.1/aa"])
    assert len(out) == 1
    assert out[0].pmcid == "PMC1"
    assert out[0].is_open_access is True
    assert len(calls) == 1


def test_a_closed_access_hit_is_kept_but_flagged(monkeypatch):
    """We still want to know the paper exists; it just cannot be indexed."""
    monkeypatch.setattr(pmc, "_get", lambda url, **kw: _fake_response([
        {"doi": "10.1/bb", "pmcid": "PMC2", "title": "B", "pubYear": "2021",
         "isOpenAccess": "N"},
    ]))
    out = pmc.lookup_dois(["10.1/bb"])
    assert out[0].is_open_access is False


def test_dois_are_batched_not_queried_one_at_a_time(monkeypatch):
    calls = []
    monkeypatch.setattr(pmc, "_get",
                        lambda url, **kw: calls.append(url) or _fake_response([]))
    pmc.lookup_dois([f"10.1/{i}" for i in range(20)], batch=8)
    assert len(calls) == 3, f"expected 3 batched calls, got {len(calls)}"


def test_a_doi_with_no_pmc_record_simply_returns_nothing(monkeypatch):
    """Positive control: absence here is data, not an error."""
    monkeypatch.setattr(pmc, "_get", lambda url, **kw: _fake_response([]))
    assert pmc.lookup_dois(["10.1/none"]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pmc_lookup.py -q`
Expected: FAIL with `ImportError: cannot import name 'pmc'`

- [ ] **Step 3: Write minimal implementation**

```python
# figcite/pmc.py
"""Fetching from Europe PMC and PMC. No index knowledge, no matching.

NCBI asks for no more than 3 requests/second without an API key, so every
request in this module goes through one throttle. It is deliberately separate
from crossref.throttled_get: that one is tuned to CrossRef's 1/s, and sharing
it would make NCBI politeness a side effect of CrossRef's rate limit.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.parse
from dataclasses import dataclass

import requests

EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
MIN_INTERVAL = 1.0 / 3.0
_last_call = 0.0


class DnsUnreachable(RuntimeError):
    """A hostname did not resolve.

    Named because it has a real precedent: cdn.ncbi.nlm.nih.gov failed to
    resolve on the author's machine while every other NCBI host worked, and
    the resulting failure looked like 'this article has no figures'.
    """


@dataclass
class PmcRecord:
    doi: str
    pmcid: str
    title: str
    year: str
    is_open_access: bool


def _get(url: str, **kw) -> bytes:
    global _last_call
    wait = MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    try:
        r = requests.get(url, timeout=kw.pop("timeout", 45), **kw)
    except requests.exceptions.ConnectionError as e:
        if isinstance(e.__cause__, socket.gaierror) or "NameResolution" in str(e):
            host = urllib.parse.urlsplit(url).hostname or url
            raise DnsUnreachable(
                f"{host} did not resolve; the corpus will be incomplete. "
                "This is a DNS failure, not an absence of figures."
            ) from e
        raise
    finally:
        _last_call = time.monotonic()
    r.raise_for_status()
    return r.content


def lookup_dois(dois: list[str], batch: int = 8) -> list[PmcRecord]:
    out: list[PmcRecord] = []
    for i in range(0, len(dois), batch):
        chunk = dois[i:i + batch]
        query = " OR ".join(f'DOI:"{d}"' for d in chunk)
        url = EUROPE_PMC + "?" + urllib.parse.urlencode(
            {"query": query, "format": "json", "pageSize": "25", "resultType": "core"}
        )
        payload = json.loads(_get(url))
        for it in payload.get("resultList", {}).get("result", []) or []:
            if not it.get("pmcid"):
                continue
            out.append(PmcRecord(
                doi=(it.get("doi") or "").lower(),
                pmcid=it["pmcid"],
                title=it.get("title", ""),
                year=str(it.get("pubYear", "")),
                is_open_access=it.get("isOpenAccess") == "Y",
            ))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pmc_lookup.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add figcite/pmc.py tests/test_pmc_lookup.py
git commit -m "feat(pmc): batched DOI -> PMCID lookup with a named DNS error"
```

---

### Task 5: Figure enumeration and image URLs

> **Superseded during execution (2026-08-19).** The article-page scrape this
> task specifies does not work: PMC returns a "Checking your browser -
> reCAPTCHA" interstitial to non-browser clients, and no header set clears it.
> Implemented instead against the AWS Open Data bucket NCBI publishes for
> programmatic access, which needs no HTML parsing and returns byte-identical
> images. `image_urls` keeps its signature; `s3_prefix(pmcid)` is new. See the
> spec's "Where the images come from" section and `tests/test_pmc_figures.py`.


**Files:**

- Modify: `figcite/pmc.py`
- Test: `tests/test_pmc_figures.py`

**Interfaces:**

- Consumes: Task 4's `_get`, `DnsUnreachable`
- Produces:
  - `FigureRef` dataclass: `label, filename, caption`
  - `figures_of(pmcid: str) -> list[FigureRef]` — parses `fullTextXML`
  - `image_urls(pmcid: str) -> dict[str, str]` — filename → CDN URL, scraped from the article page
  - `licence_of(pmcid: str) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pmc_figures.py
"""Enumerating figures, and finding the URL that actually serves them.

fullTextXML names each figure (fpls-08-00491-g0001.jpg) but the servable URL
contains an opaque path segment that appears nowhere in the XML or the API:
  https://cdn.ncbi.nlm.nih.gov/pmc/blobs/9779/5383700/ffcd625bb91d/<name>.jpg
It must be scraped from the article page. That is the most fragile joint in
SP2, so it is isolated here and tested against captured markup.
"""
import pytest

from figcite import pmc

XML = b"""<article>
  <fig id="F1"><label>Figure 1</label>
    <caption><p>Evolution of the thing.</p></caption>
    <graphic xmlns:xlink="http://www.w3.org/1999/xlink" content-type="image"
             xlink:href="fpls-08-00491-g0001.jpg"/>
    <graphic xmlns:xlink="http://www.w3.org/1999/xlink" content-type="thumb"
             xlink:href="fpls-08-00491-g0001.gif"/>
  </fig>
  <fig id="F2"><label>Figure 2</label>
    <caption><p>Another thing.</p></caption>
    <graphic xmlns:xlink="http://www.w3.org/1999/xlink" content-type="image"
             xlink:href="fpls-08-00491-g0002.jpg"/>
  </fig>
</article>"""

PAGE = b"""<html><body>
<img src="https://cdn.ncbi.nlm.nih.gov/pmc/blobs/9779/5383700/ffcd625bb91d/fpls-08-00491-g0001.jpg">
<img src="https://cdn.ncbi.nlm.nih.gov/pmc/blobs/9779/5383700/3c1df266830f/fpls-08-00491-g0002.jpg">
<img src="https://cdn.ncbi.nlm.nih.gov/pmc/banners/logo-frontplantsci.png">
</body></html>"""


def test_figures_are_enumerated_with_labels_and_captions(monkeypatch):
    monkeypatch.setattr(pmc, "_get", lambda url, **kw: XML)
    figs = pmc.figures_of("PMC5383700")
    assert [f.label for f in figs] == ["Figure 1", "Figure 2"]
    assert figs[0].filename == "fpls-08-00491-g0001.jpg"
    assert "Evolution" in figs[0].caption


def test_thumbnails_are_not_treated_as_figures(monkeypatch):
    """content-type="thumb" is a preview of the same figure, not another one."""
    monkeypatch.setattr(pmc, "_get", lambda url, **kw: XML)
    names = [f.filename for f in pmc.figures_of("PMC5383700")]
    assert not any(n.endswith(".gif") for n in names), names


def test_image_urls_are_scraped_and_keyed_by_filename(monkeypatch):
    monkeypatch.setattr(pmc, "_get", lambda url, **kw: PAGE)
    urls = pmc.image_urls("PMC5383700")
    assert urls["fpls-08-00491-g0001.jpg"].endswith("ffcd625bb91d/fpls-08-00491-g0001.jpg")
    assert len(urls) == 2, "the site banner must not be collected as a figure"


def test_a_page_with_no_figure_images_returns_empty_not_an_error(monkeypatch):
    """Positive control: 'no figures' is data. It must not raise."""
    monkeypatch.setattr(pmc, "_get", lambda url, **kw: b"<html></html>")
    assert pmc.image_urls("PMC0") == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pmc_figures.py -q`
Expected: FAIL with `AttributeError: module 'figcite.pmc' has no attribute 'figures_of'`

- [ ] **Step 3: Write minimal implementation**

Append to `figcite/pmc.py`:

```python
import re
import xml.etree.ElementTree as ET

FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
ARTICLE_PAGE = "https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
OA_SERVICE = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}"
XLINK = "{http://www.w3.org/1999/xlink}href"
BLOB_URL = re.compile(
    r"https://cdn\.ncbi\.nlm\.nih\.gov/pmc/blobs/[^\"'\s]+?/([^/\"'\s]+\.(?:jpg|jpeg|png|gif))"
)
USER_AGENT = "figcite/0.1 (https://github.com/musharna/figcite)"


@dataclass
class FigureRef:
    label: str
    filename: str
    caption: str


def figures_of(pmcid: str) -> list[FigureRef]:
    root = ET.fromstring(_get(FULLTEXT.format(pmcid=pmcid)))
    out: list[FigureRef] = []
    for fig in root.iter("fig"):
        label = (fig.findtext("label") or "").strip()
        caption = " ".join(t.strip() for t in fig.itertext() if t.strip())
        for g in fig.iter("graphic"):
            # A <graphic content-type="thumb"> is a preview of the SAME figure.
            # Counting it would double every row and index a downsampled copy.
            if g.get("content-type") == "thumb":
                continue
            href = g.get(XLINK)
            if href:
                out.append(FigureRef(label=label, filename=href, caption=caption))
                break
    return out


def image_urls(pmcid: str) -> dict[str, str]:
    """filename -> servable CDN URL, scraped from the article page.

    The opaque segment in the path exists in no API response, so there is no
    way to construct these. If PMC changes its markup this is the one function
    that breaks, and `corpus status` reports the resulting per-article failure
    rather than silently indexing nothing.
    """
    html = _get(ARTICLE_PAGE.format(pmcid=pmcid),
                headers={"User-Agent": USER_AGENT}).decode("utf8", "replace")
    return {m.group(1): m.group(0) for m in BLOB_URL.finditer(html)}


def licence_of(pmcid: str) -> str:
    try:
        xml = _get(OA_SERVICE.format(pmcid=pmcid)).decode("utf8", "replace")
    except Exception:
        return ""
    m = re.search(r'license="([^"]+)"', xml)
    return m.group(1) if m else ""
```

Note: move the `import re`, `import xml.etree.ElementTree as ET` lines to the
top of the file with the other imports before committing — an import and its
first use must land in the same write, or the repo's formatter strips it.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pmc_figures.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add figcite/pmc.py tests/test_pmc_figures.py
git commit -m "feat(pmc): enumerate figures and scrape their servable URLs"
```

---

### Task 6: The build

**Files:**

- Modify: `figcite/corpus.py`
- Test: `tests/test_corpus_build.py`

**Interfaces:**

- Consumes: Task 1 storage, Task 4/5 `pmc` functions
- Produces:
  - `BuildOutcome` dataclass: `doi, pmcid, status, detail` where `status` ∈ `{"indexed", "not-in-pmc", "not-open-access", "failed"}`
  - `build(dois: list[str], limit: int | None = None) -> list[BuildOutcome]`
  - `status() -> dict` with keys `figures`, `papers`, `by_status`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus_build.py
"""The build: resumable, and honest about what it could not index."""
import io

import pytest
from PIL import Image

from figcite import corpus, pmc


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "CORPUS_DIR", tmp_path / "corpus")
    monkeypatch.setattr(corpus, "DB_PATH", tmp_path / "corpus" / "figures.sqlite")
    monkeypatch.setattr(corpus, "IMAGE_DIR", tmp_path / "corpus" / "images")

    def png(color):
        b = io.BytesIO()
        Image.new("RGB", (40, 30), color).save(b, "PNG")
        return b.getvalue()

    monkeypatch.setattr(pmc, "lookup_dois", lambda dois, batch=8: [
        pmc.PmcRecord("10.1/oa", "PMC1", "Open paper", "2020", True),
        pmc.PmcRecord("10.1/closed", "PMC2", "Closed paper", "2021", False),
    ])
    monkeypatch.setattr(pmc, "figures_of", lambda p: [
        pmc.FigureRef("Figure 1", "f1.jpg", "First caption"),
    ])
    monkeypatch.setattr(pmc, "image_urls", lambda p: {"f1.jpg": "https://x/f1.jpg"})
    monkeypatch.setattr(pmc, "licence_of", lambda p: "CC BY")
    monkeypatch.setattr(corpus, "_download", lambda url: png("red"))
    return tmp_path


def test_an_open_access_paper_is_indexed(wired):
    out = corpus.build(["10.1/oa", "10.1/closed"])
    by = {o.doi: o.status for o in out}
    assert by["10.1/oa"] == "indexed"
    conn = corpus.connect()
    rows = corpus.all_rows(conn)
    assert len(rows) == 1
    assert rows[0].licence == "CC BY"
    assert rows[0].dhash, "a figure was stored without a perceptual hash"


def test_a_closed_access_paper_is_reported_not_silently_skipped(wired):
    out = corpus.build(["10.1/oa", "10.1/closed"])
    by = {o.doi: o.status for o in out}
    assert by["10.1/closed"] == "not-open-access"


def test_a_doi_with_no_pmc_record_is_reported(wired, monkeypatch):
    monkeypatch.setattr(pmc, "lookup_dois", lambda dois, batch=8: [])
    out = corpus.build(["10.1/ghost"])
    assert [o.status for o in out] == ["not-in-pmc"]


def test_a_rerun_does_not_duplicate_figures(wired):
    corpus.build(["10.1/oa"])
    corpus.build(["10.1/oa"])
    assert corpus.count(corpus.connect()) == 1


def test_a_download_failure_is_recorded_not_fatal(wired, monkeypatch):
    def boom(url):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(corpus, "_download", boom)
    out = corpus.build(["10.1/oa"])
    assert out[0].status == "failed"
    assert "connection reset" in out[0].detail


def test_status_reports_coverage_with_reasons(wired):
    corpus.build(["10.1/oa", "10.1/closed"])
    st = corpus.status()
    assert st["figures"] == 1
    assert st["papers"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_corpus_build.py -q`
Expected: FAIL with `AttributeError: module 'figcite.corpus' has no attribute 'build'`

- [ ] **Step 3: Write minimal implementation**

Append to `figcite/corpus.py` (add `from dataclasses import dataclass` usage and the `pmc`/`provenance`/`requests` imports at the top of the file in the same write):

```python
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

    Every DOI produces an outcome, including the ones that cannot be indexed.
    A coverage number without the reasons is the number that hides the bug.
    """
    conn = connect()
    records = pmc.lookup_dois(dois)
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
                dest = IMAGE_DIR / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(blob)
                img = Image.open(io.BytesIO(blob))
                upsert(conn, FigureRow(
                    pmcid=rec.pmcid, doi=rec.doi, label=fig.label,
                    caption=fig.caption, licence=licence, source_url=url,
                    dhash=dhash_bytes(blob), width=img.width, height=img.height,
                    image_path=rel,
                ))
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
        "by_status": {},
    }
```

Add at the top of `figcite/corpus.py`, in the same write:

```python
import io
from PIL import Image
from . import pmc
from .provenance import dhash_bytes
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_corpus_build.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add figcite/corpus.py tests/test_corpus_build.py
git commit -m "feat(corpus): resumable build that reports every uncovered DOI"
```

---

### Task 6b: Cache ORB descriptors at build time

**Files:**
- Modify: `figcite/corpus.py`, `figcite/match.py`
- Test: `tests/test_corpus_descriptors.py`

**Why:** `by_orb` as written in Task 3 decodes and re-processes every corpus
image on every query. At the measured corpus size (~1,200 figures) that is
tens of seconds per lookup, which makes the pending card unusable. The spec
stores descriptors for this reason.

**Interfaces:**
- Produces: `corpus.DESCRIPTOR_DIR: Path`, `corpus.descriptor_path(rel: str) -> Path`,
  `corpus.write_descriptors(rel: str, blob: bytes) -> bool` (False when opencv absent)
- Changes: `match.by_orb(query_bytes, rows, image_root, descriptor_dir=None)` —
  loads `<descriptor_dir>/<image_path>.npy` when present, else falls back to
  decoding the image, so a corpus built without opencv still works once it is
  installed.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus_descriptors.py
"""Descriptors are computed once at build time, not per query."""
import io

import pytest
from PIL import Image, ImageDraw

from figcite import corpus, match


def _textured(seed, size=(200, 200)):
    import random
    rnd = random.Random(seed)
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    for _ in range(60):
        x, y = rnd.randrange(160), rnd.randrange(160)
        d.rectangle([x, y, x + 20, y + 20],
                    fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "CORPUS_DIR", tmp_path / "c")
    monkeypatch.setattr(corpus, "IMAGE_DIR", tmp_path / "c" / "images")
    monkeypatch.setattr(corpus, "DESCRIPTOR_DIR", tmp_path / "c" / "descriptors")
    monkeypatch.setattr(corpus, "DB_PATH", tmp_path / "c" / "figures.sqlite")
    return tmp_path


@pytest.mark.skipif(not match.opencv_available(), reason="opencv not installed")
def test_descriptors_are_written_for_a_figure(wired):
    assert corpus.write_descriptors("PMC1/f1.png", _textured(1)) is True
    assert corpus.descriptor_path("PMC1/f1.png").exists()


def test_write_descriptors_is_a_no_op_without_opencv(wired, monkeypatch):
    """Absent opencv is a valid state, not an error."""
    monkeypatch.setattr(match, "opencv_available", lambda: False)
    assert corpus.write_descriptors("PMC1/f1.png", _textured(1)) is False
    assert not corpus.descriptor_path("PMC1/f1.png").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_corpus_descriptors.py -q`
Expected: FAIL with `AttributeError: module 'figcite.corpus' has no attribute 'DESCRIPTOR_DIR'`

- [ ] **Step 3: Write minimal implementation**

In `figcite/corpus.py` (all in one write, imports included):

```python
DESCRIPTOR_DIR = CORPUS_DIR / "descriptors"


def descriptor_path(rel: str):
    return DESCRIPTOR_DIR / (rel + ".npy")


def write_descriptors(rel: str, blob: bytes) -> bool:
    """Cache ORB descriptors so a query does not re-decode the whole corpus.

    Returns False when opencv is absent -- that is a valid state, and by_orb
    falls back to decoding images, so a corpus built without it still works
    once it is installed.
    """
    from . import match

    if not match.opencv_available():
        return False
    import cv2
    import numpy as np

    img = match._decode(blob)
    if img is None:
        return False
    _k, desc = cv2.ORB_create(nfeatures=1500).detectAndCompute(img, None)
    if desc is None:
        return False
    dest = descriptor_path(rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    np.save(str(dest), desc)
    return True
```

Call it from `build()` immediately after `dest.write_bytes(blob)`:

```python
                write_descriptors(rel, blob)
```

In `figcite/match.py`, give `by_orb` the optional cache:

```python
def by_orb(query_bytes: bytes, rows, image_root, descriptor_dir=None) -> Verdict:
```

and inside the per-row loop, replace the `cv2.imread(...)` + `detectAndCompute`
pair with:

```python
        d = None
        if descriptor_dir is not None:
            cached = Path(descriptor_dir) / (row.image_path + ".npy")
            if cached.exists():
                d = np.load(str(cached))
                k = None
        if d is None:
            img = cv2.imread(str(Path(image_root) / row.image_path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            k, d = orb.detectAndCompute(img, None)
```

Geometric verification needs the target keypoints, which the cache does not
carry, so when `k is None` score that row by ratio-test match count and
re-verify only the top candidate by decoding its image. Implement that as:

```python
    scored.sort(key=lambda t: t[0], reverse=True)
    # Re-verify the leader geometrically if it was scored from cache alone.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_corpus_descriptors.py tests/test_match_orb.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add figcite/corpus.py figcite/match.py tests/test_corpus_descriptors.py
git commit -m "perf(corpus): cache ORB descriptors so a query is not a full re-scan"
```

---

### Task 7: `service.whereis` and open-tab candidates

**Files:**

- Modify: `figcite/service.py`
- Test: `tests/test_service_whereis.py`

**Interfaces:**

- Consumes: Task 2/3 `match`, Task 1/6 `corpus`, existing `session_tabs.tab_candidates`
- Produces: `service.whereis(ref_or_path) -> dict` with keys `verdict` (`"match"|"no-match"|"could-not-decide"`), `matches` (list of dicts shaped like the pending-card candidate: `source`, `score`, `doi`, `title`, `container`, `year`, `type`), `reason`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_service_whereis.py
"""whereis: the one entry point both front ends call."""
import io

import pytest
from PIL import Image

from figcite import corpus, match, service, session_tabs


def _png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), "blue").save(path)
    return path


def test_a_pixel_match_is_returned_as_a_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(
        match, "by_dhash",
        lambda b, rows: match.Match("10.1/found", "PMC9", "Figure 3", "dhash", 0.0, 9.0))
    monkeypatch.setattr(corpus, "all_rows", lambda conn: [object()])
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    out = service.whereis(str(_png(tmp_path / "q.png")))
    assert out["verdict"] == "match"
    assert out["matches"][0]["doi"] == "10.1/found"
    assert out["matches"][0]["source"] == "dhash"


def test_open_tabs_are_offered_but_rank_below_a_pixel_match(tmp_path, monkeypatch):
    """A tab is a lead about where a figure came from, not evidence."""
    monkeypatch.setattr(
        match, "by_dhash",
        lambda b, rows: match.Match("10.1/pixel", "PMC9", "Figure 1", "dhash", 0.0, 9.0))
    monkeypatch.setattr(corpus, "all_rows", lambda conn: [object()])
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [
        {"source": "open-tab", "score": "", "doi": "10.1/tab", "title": "T",
         "container": "example.org", "year": "", "type": ""}])

    out = service.whereis(str(_png(tmp_path / "q.png")))
    assert [m["doi"] for m in out["matches"]] == ["10.1/pixel", "10.1/tab"]


def test_could_not_decide_is_not_reported_as_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(match, "by_dhash",
                        lambda b, rows: match.CouldNotDecide("too smooth"))
    monkeypatch.setattr(match, "by_orb",
                        lambda b, rows, root, descriptor_dir=None: match.CouldNotDecide("too smooth"))
    monkeypatch.setattr(corpus, "all_rows", lambda conn: [object()])
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    out = service.whereis(str(_png(tmp_path / "q.png")))
    assert out["verdict"] == "could-not-decide"
    assert "too smooth" in out["reason"]


def test_a_real_no_match_is_reported_as_such(tmp_path, monkeypatch):
    """Positive control for the test above: no-match must stay reachable."""
    monkeypatch.setattr(match, "by_dhash",
                        lambda b, rows: match.CouldNotDecide("no dhash hit"))
    monkeypatch.setattr(match, "by_orb",
                        lambda b, rows, root, descriptor_dir=None: match.NoMatch())
    monkeypatch.setattr(corpus, "all_rows", lambda conn: [object()])
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    out = service.whereis(str(_png(tmp_path / "q.png")))
    assert out["verdict"] == "no-match"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_service_whereis.py -q`
Expected: FAIL with `AttributeError: module 'figcite.service' has no attribute 'whereis'`

- [ ] **Step 3: Write minimal implementation**

Append to `figcite/service.py` (adding `from . import corpus, match, session_tabs` at the top in the same write):

```python
def whereis(ref_or_path) -> dict:
    """Where might this figure have come from?

    dhash first (free), ORB second (handles crops), open tabs last (a lead,
    never evidence). The verdict distinguishes 'searched and found nothing'
    from 'could not look' because they license different next actions: the
    first means the figure is not in your corpus, the second means you learned
    nothing at all.
    """
    path = Path(ref_or_path)
    if not path.exists():
        path = _resolve_ref_to_path(str(ref_or_path))
    blob = path.read_bytes()

    conn = corpus.connect()
    rows = corpus.all_rows(conn)

    verdict = match.by_dhash(blob, rows)
    if not isinstance(verdict, match.Match):
        orb = match.by_orb(blob, rows, corpus.IMAGE_DIR, corpus.DESCRIPTOR_DIR)
        # An ORB NoMatch is a real search of the corpus and outranks dhash's
        # could-not-decide, which only ever meant "a crop is invisible to me".
        if isinstance(orb, (match.Match, match.NoMatch)):
            verdict = orb
        elif isinstance(verdict, match.CouldNotDecide):
            verdict = match.CouldNotDecide(f"{verdict.reason}; {orb.reason}")

    matches: list[dict] = []
    if isinstance(verdict, match.Match):
        row = next((r for r in rows
                    if r.pmcid == verdict.pmcid and r.label == verdict.label), None)
        matches.append({
            "source": verdict.method,
            "score": round(verdict.score, 1),
            "doi": verdict.doi,
            "title": (row.caption[:120] if row else verdict.label),
            "container": verdict.pmcid,
            "year": "",
            "type": "figure",
        })

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_service_whereis.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add figcite/service.py tests/test_service_whereis.py
git commit -m "feat(service): whereis, with open tabs ranked below any pixel match"
```

---

### Task 7b: Duplication — has this figure appeared elsewhere?

**Files:**
- Modify: `figcite/corpus.py`, `figcite/service.py`
- Test: `tests/test_corpus_duplicates.py`

**Why:** this is the second of the two questions SP2 exists to answer, and it
is the same index queried in the opposite direction. It reports; it never
rewrites a record.

**Interfaces:**
- Produces:
  - `corpus.duplicates_of(image_bytes: bytes, credited_doi: str) -> list[FigureRow]`
  - `service.duplicates(ref_or_path, credited_doi) -> dict` with keys
    `others` (list of `{doi, pmcid, label, licence}`) and `reason`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corpus_duplicates.py
"""The same figure carrying a DIFFERENT DOI than the one credited."""
import io

import pytest
from PIL import Image

from figcite import corpus, match


def _png(color, size=(64, 64)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "CORPUS_DIR", tmp_path / "c")
    monkeypatch.setattr(corpus, "DB_PATH", tmp_path / "c" / "figures.sqlite")
    monkeypatch.setattr(corpus, "IMAGE_DIR", tmp_path / "c" / "images")
    monkeypatch.setattr(corpus, "DESCRIPTOR_DIR", tmp_path / "c" / "descriptors")
    from figcite.provenance import dhash_bytes

    conn = corpus.connect()
    blob = _png("red")
    for pmcid, doi in (("PMC1", "10.1/credited"), ("PMC2", "10.1/elsewhere")):
        corpus.upsert(conn, corpus.FigureRow(
            pmcid=pmcid, doi=doi, label="Figure 1", caption="", licence="CC BY",
            source_url="", dhash=dhash_bytes(blob), width=64, height=64,
            image_path=f"{pmcid}/f1.png"))
    conn.close()
    return blob


def test_the_same_figure_under_another_doi_is_reported(wired):
    others = corpus.duplicates_of(wired, credited_doi="10.1/credited")
    assert [o.doi for o in others] == ["10.1/elsewhere"]


def test_the_credited_paper_is_not_reported_as_a_duplicate_of_itself(wired):
    others = corpus.duplicates_of(wired, credited_doi="10.1/credited")
    assert all(o.doi != "10.1/credited" for o in others)


def test_a_figure_in_only_one_paper_has_no_duplicates(wired):
    """Positive control: the query must be able to return nothing."""
    others = corpus.duplicates_of(_png("blue"), credited_doi="10.1/credited")
    assert others == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_corpus_duplicates.py -q`
Expected: FAIL with `AttributeError: module 'figcite.corpus' has no attribute 'duplicates_of'`

- [ ] **Step 3: Write minimal implementation**

In `figcite/corpus.py`:

```python
def duplicates_of(image_bytes: bytes, credited_doi: str) -> list[FigureRow]:
    """Corpus figures matching these bytes but carrying a different DOI.

    dhash only: a duplicate published elsewhere is normally the same figure
    re-encoded or rescaled, which dhash sees. Reporting is the whole job here
    -- nothing is rewritten, and a hit is a question for the user, not a
    correction.
    """
    from .provenance import dhash_bytes, hamming
    from . import match as _match

    dh = dhash_bytes(image_bytes)
    credited = (credited_doi or "").lower()
    conn = connect()
    out = []
    for row in all_rows(conn):
        if row.doi.lower() == credited:
            continue
        if hamming(dh, row.dhash) <= _match.DHASH_THRESHOLD:
            out.append(row)
    return out
```

In `figcite/service.py`:

```python
def duplicates(ref_or_path, credited_doi: str) -> dict:
    path = Path(ref_or_path)
    if not path.exists():
        path = _resolve_ref_to_path(str(ref_or_path))
    rows = corpus.duplicates_of(path.read_bytes(), credited_doi)
    return {
        "others": [
            {"doi": r.doi, "pmcid": r.pmcid, "label": r.label, "licence": r.licence}
            for r in rows
        ],
        "reason": "" if rows else "no other indexed paper carries this figure",
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_corpus_duplicates.py -q`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add figcite/corpus.py figcite/service.py tests/test_corpus_duplicates.py
git commit -m "feat(corpus): report a figure that also appears under another DOI"
```

---

### Task 8: CLI verbs

**Files:**

- Modify: `figcite/cli.py`
- Test: `tests/test_cli_corpus.py`

**Interfaces:**

- Consumes: Task 6 `corpus.build`/`corpus.status`, Task 7 `service.whereis`
- Produces: `cmd_corpus_build`, `cmd_corpus_status`, `cmd_whereis`, and the `corpus` / `whereis` subparsers

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_corpus.py
"""`figcite corpus` and `figcite whereis`."""
from figcite import cli, corpus, service


def test_build_reports_every_uncovered_doi_with_its_reason(capsys, monkeypatch):
    monkeypatch.setattr(corpus, "build", lambda dois, limit=None: [
        corpus.BuildOutcome("10.1/a", "PMC1", "indexed"),
        corpus.BuildOutcome("10.1/b", "PMC2", "not-open-access"),
        corpus.BuildOutcome("10.1/c", "", "not-in-pmc"),
        corpus.BuildOutcome("10.1/d", "PMC4", "failed", "connection reset"),
    ])
    monkeypatch.setattr(cli, "_corpus_dois", lambda: ["10.1/a", "10.1/b", "10.1/c", "10.1/d"])

    class A:
        limit = None

    cli.cmd_corpus_build(A())
    out = capsys.readouterr().out
    assert "indexed" in out
    assert "not-open-access" in out
    assert "not-in-pmc" in out
    assert "connection reset" in out, "a failure must name its reason"


def test_whereis_prints_could_not_decide_with_the_reason(capsys, monkeypatch):
    monkeypatch.setattr(service, "whereis", lambda p: {
        "verdict": "could-not-decide", "matches": [], "reason": "too smooth"})

    class A:
        image = "x.png"

    cli.cmd_whereis(A())
    out = capsys.readouterr().out
    assert "could not" in out.lower()
    assert "too smooth" in out


def test_whereis_distinguishes_a_real_no_match(capsys, monkeypatch):
    """Positive control: the two outcomes must not print the same words."""
    monkeypatch.setattr(service, "whereis", lambda p: {
        "verdict": "no-match", "matches": [], "reason": ""})

    class A:
        image = "x.png"

    cli.cmd_whereis(A())
    out = capsys.readouterr().out.lower()
    assert "no match" in out or "not in your corpus" in out
    assert "could not" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_corpus.py -q`
Expected: FAIL with `AttributeError: module 'figcite.cli' has no attribute 'cmd_corpus_build'`

- [ ] **Step 3: Write minimal implementation**

Add to `figcite/cli.py`:

```python
def _corpus_dois() -> list[str]:
    from . import zotero

    out = []
    for item in zotero.library(max_age_hours=None):
        doi, _how = zotero._doi_of(item)
        if doi:
            out.append(doi)
    return out


def cmd_corpus_build(a) -> int:
    from . import corpus

    dois = _corpus_dois()
    print(f"{len(dois)} DOI(s) in your library; indexing the open-access ones...")
    outcomes = corpus.build(dois, limit=getattr(a, "limit", None))
    tally: dict[str, int] = {}
    for o in outcomes:
        tally[o.status] = tally.get(o.status, 0) + 1
        if o.status == "failed":
            print(f"  FAILED   {o.doi}  {o.detail[:80]}")
    for status_name, n in sorted(tally.items()):
        print(f"  {status_name:16} {n}")
    return 0


def cmd_corpus_status(a) -> int:
    from . import corpus

    st = corpus.status()
    print(f"corpus: {st['figures']} figure(s) from {st['papers']} paper(s)")
    return 0


def cmd_whereis(a) -> int:
    from . import service

    res = service.whereis(a.image)
    if res["verdict"] == "match":
        for i, m in enumerate(res["matches"]):
            print(f"  [{i}] {m['doi']}  [{m['source']}]  {m['title'][:70]}")
        print("  accept one with: figcite confirm <ref> --doi <the DOI above>")
    elif res["verdict"] == "no-match":
        print("  no match: this figure is not in your corpus")
    else:
        print(f"  could not decide: {res['reason']}")
    return 0
```

And in the parser section, beside the other subparsers:

```python
    cp = sub.add_parser("corpus", help="the local figure index used by `whereis`")
    csub = cp.add_subparsers(dest="corpus_cmd", required=True)
    cb = csub.add_parser("build", help="index figures for your Zotero DOIs")
    cb.add_argument("--limit", type=int, default=None)
    cb.set_defaults(func=cmd_corpus_build)
    cs = csub.add_parser("status", help="what the corpus covers, and what it does not")
    cs.set_defaults(func=cmd_corpus_status)

    wi = sub.add_parser("whereis", help="find which paper a figure came from")
    wi.add_argument("image")
    wi.set_defaults(func=cmd_whereis)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_corpus.py -q`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add figcite/cli.py tests/test_cli_corpus.py
git commit -m "feat(cli): corpus build/status and whereis"
```

---

### Task 9: Live boundary test

**Files:**

- Create: `tests/test_pmc_live.py`

**Interfaces:**

- Consumes: Tasks 4 and 5

- [ ] **Step 1: Write the test**

```python
# tests/test_pmc_live.py
"""The real boundary, including the scraped CDN URL no fixture can exercise.

Marked `live` so it is excluded from the push gate. This is the only test that
would notice PMC changing its article markup -- the fragile joint of SP2.
"""
import pytest

from figcite import pmc

PMCID = "PMC5383700"  # Frontiers in Plant Science, open access, 6 figures


@pytest.mark.live
def test_a_real_article_yields_figures_with_fetchable_urls():
    figs = pmc.figures_of(PMCID)
    assert figs, "fullTextXML returned no figures for a known 6-figure article"
    assert all(f.filename for f in figs)

    urls = pmc.image_urls(PMCID)
    assert urls, (
        "no CDN blob URLs scraped from the article page -- PMC's markup may "
        "have changed; this is the joint that breaks"
    )
    overlap = set(urls) & {f.filename for f in figs}
    assert overlap, (
        f"figure names {sorted(f.filename for f in figs)[:3]} do not line up "
        f"with page image names {sorted(urls)[:3]}"
    )

    name = sorted(overlap)[0]
    blob = pmc._get(urls[name], headers={"User-Agent": pmc.USER_AGENT})
    assert len(blob) > 5000, f"suspiciously small image: {len(blob)} bytes"
    assert blob[:3] == b"\xff\xd8\xff" or blob[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.live
def test_a_real_doi_resolves_to_a_pmcid():
    recs = pmc.lookup_dois(["10.3389/fpls.2017.00491"])
    assert recs, "Europe PMC returned nothing for a known indexed DOI"
    assert recs[0].pmcid == PMCID
    assert recs[0].is_open_access is True
```

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/test_pmc_live.py -q -m live`
Expected: 2 passed. If DNS for `cdn.ncbi.nlm.nih.gov` fails, the error must be `DnsUnreachable`, not a bare `ConnectionError`.

- [ ] **Step 3: Confirm it is excluded from the push gate**

Run: `python -m pytest -m "not live" --collect-only -q | tail -2`
Expected: the two tests above are deselected.

- [ ] **Step 4: Commit**

```bash
git add tests/test_pmc_live.py
git commit -m "test(pmc): live boundary test for the scraped CDN URL"
```

---

### Task 10: Surface matches in the pending card

**Files:**

- Modify: `figcite/webui.py`
- Test: `tests/test_webui_whereis.py`

**Interfaces:**

- Consumes: Task 7's candidate shape

- [ ] **Step 1: Write the failing test**

```python
# tests/test_webui_whereis.py
"""The card must show HOW a candidate was found, not just that it was.

An open-tab lead beside a pixel match invites being read as equivalent
evidence. The existing `.src` badge already renders `c.source`, so the
requirement is that whereis candidates carry it and the card keeps showing it.
"""
from figcite.webui import PAGE


def test_the_candidate_badge_renders_the_source():
    assert 'class="src"' in PAGE
    assert "c.source" in PAGE, (
        "the candidate list stopped rendering its source badge; an open-tab "
        "lead and a pixel match would then look identical"
    )


def test_the_card_has_a_place_to_show_a_could_not_decide_reason():
    assert "doi_evidence" in PAGE
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `python -m pytest tests/test_webui_whereis.py -q`
Expected: PASS if the badge already renders (it does at `57bedb2`) — this task is a regression guard, so if it passes immediately, verify by deleting `c.source` from `webui.py`, re-running to see it fail, then restoring.

- [ ] **Step 3: Confirm the guard can fail**

Temporarily change `${esc(c.source || "")}` to `${""}` in `figcite/webui.py`, run the test, confirm FAIL, then restore.

- [ ] **Step 4: Commit**

```bash
git add tests/test_webui_whereis.py
git commit -m "test(webui): pin the candidate source badge that distinguishes a lead from a match"
```

---

### Task 10b: Flag a duplicate on the deck row

**Files:**
- Modify: `figcite/service.py` (the `audit` report), `figcite/webui.py`
- Test: `tests/test_deck_duplicate_flag.py`

**Why:** the spec's Surfaces section requires it, and it is where duplication
becomes useful — reviewing a talk is when you would want to know a figure you
credited to one paper also appears in another. It reports only.

**Interfaces:**
- Consumes: Task 7b `corpus.duplicates_of`
- Produces: each `audit` row gains `duplicate_of: list[str]` (DOIs), empty when none

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deck_duplicate_flag.py
"""A credited figure that also appears under another DOI."""
from figcite import corpus, service


def test_a_row_carries_the_other_dois_that_hold_this_figure(monkeypatch):
    class Row:
        doi, pmcid, label, licence = "10.1/elsewhere", "PMC2", "Figure 1", "CC BY"

    monkeypatch.setattr(corpus, "duplicates_of", lambda b, credited_doi: [Row()])
    out = service._duplicate_dois(b"fake-bytes", "10.1/credited")
    assert out == ["10.1/elsewhere"]


def test_no_duplicate_yields_an_empty_list_not_none(monkeypatch):
    """Positive control: the empty case must be a list the UI can iterate."""
    monkeypatch.setattr(corpus, "duplicates_of", lambda b, credited_doi: [])
    assert service._duplicate_dois(b"fake-bytes", "10.1/credited") == []


def test_a_corpus_failure_does_not_break_the_audit(monkeypatch):
    """An audit must still render if the corpus is missing or unreadable."""
    def boom(b, credited_doi):
        raise RuntimeError("no corpus")

    monkeypatch.setattr(corpus, "duplicates_of", boom)
    assert service._duplicate_dois(b"fake-bytes", "10.1/credited") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_deck_duplicate_flag.py -q`
Expected: FAIL with `AttributeError: module 'figcite.service' has no attribute '_duplicate_dois'`

- [ ] **Step 3: Write minimal implementation**

In `figcite/service.py`:

```python
def _duplicate_dois(image_bytes: bytes, credited_doi: str) -> list[str]:
    """DOIs other than the credited one whose figures match these bytes.

    Deliberately swallows corpus failures: a deck audit is useful without the
    corpus, and an unbuilt index must not stop you reviewing a talk.
    """
    try:
        return [r.doi for r in corpus.duplicates_of(image_bytes, credited_doi)]
    except Exception:
        return []
```

Then in `audit()`, for each row that has both image bytes and a credited DOI,
set `row["duplicate_of"] = _duplicate_dois(blob, row_doi)`.

In `figcite/webui.py`'s deck row renderer, after the licence cell:

```javascript
${(r.duplicate_of && r.duplicate_of.length)
   ? `<div class="badge warn">also in ${esc(r.duplicate_of.join(", "))}</div>` : ""}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_deck_duplicate_flag.py -q`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add figcite/service.py figcite/webui.py tests/test_deck_duplicate_flag.py
git commit -m "feat(deck): flag a figure that also appears under another DOI"
```

---

### Task 11: Full suite and README

**Files:**

- Modify: `README.md`

- [ ] **Step 1: Run the whole suite**

Run: `python -m pytest -q`
Expected: all pass, 1 pre-existing skip (`test_browser.py:146`, publisher bot-wall).

- [ ] **Step 2: Run the push gate selection**

Run: `python -m pytest -m "not live" -q`
Expected: all pass; live tests deselected.

- [ ] **Step 3: Document the feature**

Add a `## Finding where a figure came from` section to `README.md` covering:
`figcite corpus build`, `figcite corpus status`, `figcite whereis <image>`; that
only open-access papers can be indexed and roughly what fraction of a library
that is; that opencv is optional and what is lost without it (crops); and that
a match is always a candidate, never an automatic citation.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: how to find where a figure came from"
```
