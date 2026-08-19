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

    monkeypatch.setattr(
        pmc,
        "lookup_dois",
        lambda dois, batch=8: [
            pmc.PmcRecord("10.1/oa", "PMC1", "Open paper", "2020", True),
            pmc.PmcRecord("10.1/closed", "PMC2", "Closed paper", "2021", False),
        ],
    )
    monkeypatch.setattr(
        pmc,
        "figures_of",
        lambda p: [pmc.FigureRef("Figure 1", "f1.jpg", "First caption")],
    )
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


def test_one_bad_article_does_not_stop_the_others(wired, monkeypatch):
    """A resumable build is worthless if the first failure aborts the run."""
    monkeypatch.setattr(
        pmc,
        "lookup_dois",
        lambda dois, batch=8: [
            pmc.PmcRecord("10.1/bad", "PMCBAD", "Bad", "2020", True),
            pmc.PmcRecord("10.1/good", "PMCGOOD", "Good", "2020", True),
        ],
    )

    def figures(pmcid):
        if pmcid == "PMCBAD":
            raise RuntimeError("malformed xml")
        return [pmc.FigureRef("Figure 1", "f1.jpg", "cap")]

    monkeypatch.setattr(pmc, "figures_of", figures)
    out = corpus.build(["10.1/bad", "10.1/good"])
    by = {o.doi: o.status for o in out}
    assert by["10.1/bad"] == "failed"
    assert by["10.1/good"] == "indexed", "a later article was skipped after a failure"


def test_status_reports_what_is_indexed(wired):
    corpus.build(["10.1/oa", "10.1/closed"])
    st = corpus.status()
    assert st["figures"] == 1
    assert st["papers"] == 1
