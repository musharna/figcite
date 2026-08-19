"""The corpus index: one row per figure, addressed by (pmcid, label)."""

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
        pmcid="PMC1",
        doi="10.1/a",
        label="Figure 1",
        caption="A caption",
        licence="CC BY",
        source_url="https://example.org/f1.jpg",
        dhash="0011223344556677",
        width=100,
        height=80,
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
