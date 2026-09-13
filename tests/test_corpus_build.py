"""The build: resumable, and honest about what it could not index."""

import io

import pytest
from PIL import Image

from figcite import corpus, pmc


def _lookup(records):
    """`lookup_dois` returns records AND the DOIs it could not reach.

    These stubs all describe a lookup that completed, so nothing is
    unreachable -- tests/test_pmc_resilience.py covers the other channel.
    """
    return pmc.Lookup(records=records, unreachable={})


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
        lambda dois, batch=8: _lookup(
            [
                pmc.PmcRecord("10.1/oa", "PMC1", "Open paper", "2020", True),
                pmc.PmcRecord("10.1/closed", "PMC2", "Closed paper", "2021", False),
            ]
        ),
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


def test_a_doi_europe_pmc_has_never_seen_is_reported(wired, monkeypatch):
    """Renamed from not-in-pmc: that name covered two different facts."""
    monkeypatch.setattr(pmc, "lookup_dois", lambda dois, batch=8: _lookup([]))
    out = corpus.build(["10.1/ghost"])
    assert [o.status for o in out] == ["not-in-europe-pmc"]


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
        lambda dois, batch=8: _lookup(
            [
                pmc.PmcRecord("10.1/bad", "PMCBAD", "Bad", "2020", True),
                pmc.PmcRecord("10.1/good", "PMCGOOD", "Good", "2020", True),
            ]
        ),
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


def test_an_upstream_outage_is_reported_per_doi_not_a_traceback(wired, monkeypatch):
    """Europe PMC's /search endpoint returned 404 for everything mid-build.

    build() propagated the HTTPError and died, so a ten-minute run over 535
    DOIs would lose every outcome it had already gathered. An upstream outage
    is exactly the case a resumable build exists for.
    """

    def boom(dois, batch=8):
        raise RuntimeError("404 Client Error: Not Found for url: .../rest/search")

    monkeypatch.setattr(pmc, "lookup_dois", boom)
    out = corpus.build(["10.1/a", "10.1/b"])
    assert [o.status for o in out] == ["failed", "failed"]
    assert "404" in out[0].detail


def test_a_working_lookup_is_not_reported_as_failed(wired):
    """Positive control: the catch must not swallow the healthy path."""
    out = corpus.build(["10.1/oa"])
    assert out[0].status == "indexed"


def test_found_but_paywalled_is_distinct_from_never_heard_of_it(wired, monkeypatch):
    """Two very different facts were collapsing into one status.

    Measured on the real library: 10.1111/nph.20145 IS in Europe PMC but has
    no PMC copy, while 10.1029/2024MS004308 is absent entirely. The first says
    "the paper exists, its figures are not reachable"; the second may mean the
    DOI is wrong. Reporting both as "not-in-pmc" throws that away.
    """
    monkeypatch.setattr(
        pmc,
        "lookup_dois",
        lambda dois, batch=8: _lookup(
            [
                pmc.PmcRecord("10.1/paywalled", "", "Paywalled", "2024", False),
            ]
        ),
    )
    out = corpus.build(["10.1/paywalled", "10.1/unknown"])
    by = {o.doi: o.status for o in out}
    assert by["10.1/paywalled"] == "no-pmc-copy"
    assert by["10.1/unknown"] == "not-in-europe-pmc"


def test_a_pmc_copy_that_is_not_open_access_keeps_its_own_status(wired, monkeypatch):
    """Positive control: the third case must not collapse into the other two."""
    monkeypatch.setattr(
        pmc,
        "lookup_dois",
        lambda dois, batch=8: _lookup(
            [
                pmc.PmcRecord("10.1/closed", "PMC9", "Closed", "2024", False),
            ]
        ),
    )
    out = corpus.build(["10.1/closed"])
    assert out[0].status == "not-open-access"
