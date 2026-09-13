"""One paper has many figures, and only one had ever been in the corpus.

`whereis` gets a verdict naming a pmcid AND a label, then finds the corpus row
behind it to read a caption off:

    row = next(
        (r for r in rows
         if getattr(r, "pmcid", None) == verdict.pmcid
         and getattr(r, "label", None) == verdict.label),
        None,
    )
    ...
    "title": row.caption[:120] if row else verdict.label

The label half narrowed to `>=` survived, because every corpus any test builds
holds ONE figure per paper -- so the pmcid alone already identifies the row and
the label comparison has nothing to discriminate.

Papers have Figure 1 and Figure 2. With two rows sharing a pmcid, `next()`
returns the FIRST row satisfying the predicate, and `>=` is satisfied by any
label sorting at or above the one that actually matched. The verdict still
names the right paper and the right label -- but the caption shown to the user
comes off a different figure.

That is a quiet wrong answer of the kind this tool is built to avoid: not "I
could not tell", but a confident description of the wrong picture.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from figcite import corpus, service
from figcite.corpus import FigureRow
from figcite.provenance import dhash_bytes

PMCID = "PMC999999"
DOI = "10.1/two-figures"


def _png(path, colour, offset):
    """A figure with STRUCTURE.

    dhash compares adjacent-pixel gradients, so a flat rectangle hashes the
    same whatever colour it is -- the first version of this fixture built two
    solid images and they collided, which would have let the match name either
    row for a reason unrelated to the guard under test.
    """
    im = Image.new("RGB", (64, 48), (250, 250, 250))
    d = ImageDraw.Draw(im)
    d.rectangle([offset, 6, offset + 18, 40], fill=colour)
    d.ellipse([offset + 24, 10, offset + 40, 36], fill=(20, 20, 20))
    im.save(path)
    return path


def _row(label, caption, dhash, image_path):
    return FigureRow(
        pmcid=PMCID,
        doi=DOI,
        label=label,
        caption=caption,
        licence="cc-by",
        source_url="https://example.invalid/fig",
        dhash=dhash,
        width=64,
        height=48,
        image_path=str(image_path),
    )


@pytest.fixture
def two_figures_one_paper(tmp_path, monkeypatch):
    """Two figures from ONE paper, the higher label inserted FIRST.

    Insertion order matters: `all_rows` issues a bare SELECT with no ORDER BY,
    so rows come back in rowid order. Putting "Figure 2" first is what lets a
    widened comparison reach it before the row that actually matched.
    """
    query_png = _png(tmp_path / "query.png", (10, 120, 200), offset=4)
    other_png = _png(tmp_path / "other.png", (200, 40, 10), offset=20)
    query_hash = dhash_bytes(query_png.read_bytes())
    other_hash = dhash_bytes(other_png.read_bytes())

    # `whereis` garnishes its answer with open browser tabs, which shells out
    # to PowerShell. The suite blocks Windows interop in non-live tests, and
    # rightly: this test is about which corpus row a match names.
    monkeypatch.setattr(service.session_tabs, "tab_candidates", lambda: [])

    conn = corpus.connect()
    corpus.upsert(conn, _row("Figure 2", "The SECOND figure", other_hash, other_png))
    corpus.upsert(conn, _row("Figure 1", "The FIRST figure", query_hash, query_png))
    conn.close()
    return {"query": query_png, "query_hash": query_hash, "other_hash": other_hash}


def test_the_fixture_really_holds_two_rows_for_one_paper(two_figures_one_paper):
    """The premise. One row per paper makes the label comparison unobservable,
    which is exactly why this mutant survived the existing suite."""
    conn = corpus.connect()
    rows = [r for r in corpus.all_rows(conn) if r.pmcid == PMCID]
    conn.close()

    assert len(rows) == 2, rows
    assert [r.label for r in rows] == ["Figure 2", "Figure 1"], (
        f"insertion order changed; the higher label must come first for the "
        f"widened comparison to be reachable: {[r.label for r in rows]}"
    )
    assert "Figure 2" > "Figure 1"
    assert two_figures_one_paper["query_hash"] != two_figures_one_paper["other_hash"], (
        "both figures hash the same, so the match could name either"
    )


def test_whereis_describes_the_figure_it_matched(two_figures_one_paper):
    """The caption must come off the row that matched, not off whichever row
    with the same pmcid happens to sort at or above it."""
    out = service.whereis(str(two_figures_one_paper["query"]))

    assert out["verdict"] == "match", out
    evidence = [m for m in out["matches"] if m.get("evidence")]
    assert evidence, out

    titles = [m["title"] for m in evidence]
    assert any("FIRST" in t for t in titles), (
        f"whereis matched Figure 1 and described a different figure: {titles}"
    )
    assert not any("SECOND" in t for t in titles), f"the caption came off the wrong row: {titles}"
