"""Browser grounding, tested against the real history DB and live CrossRef.

Fixtures are DERIVED from the live database rather than hardcoded, so the tests
stay valid as browsing changes, and skip with a stated reason when the machine
genuinely has nothing to test against.
"""

import sqlite3

import pytest

from figcite.browser import (
    doi_from_publisher_pattern,
    doi_from_url_text,
    doi_from_page_meta,
    firefox_profiles,
    lookup_by_title,
    resolve_from_capture,
    snapshot_history,
    strip_browser_suffix,
)

# --- offline: URL shapes taken verbatim from the real history -------------

REAL_URL_SHAPES = [
    ("https://nph.onlinelibrary.wiley.com/doi/10.1111/nph.71477", "10.1111/nph.71477"),
    (
        "https://www.frontiersin.org/journals/plant-science/articles/10.3389/fpls.2017.00491/full",
        "10.3389/fpls.2017.00491",
    ),
    (
        "https://link.springer.com/content/pdf/10.1038/msb.2013.40.pdf",
        "10.1038/msb.2013.40",
    ),
    (
        "https://journals.plos.org/plosbiology/article?id=10.1371/journal.pbio.1001841",
        "10.1371/journal.pbio.1001841",
    ),
    ("https://doi.org/10.1016/j.ympev.2025.108410", "10.1016/j.ympev.2025.108410"),
]


@pytest.mark.parametrize("url,expected", REAL_URL_SHAPES)
def test_doi_extracted_from_real_url_shapes(url, expected):
    assert doi_from_url_text(url) == expected


def test_publisher_pattern_and_its_negative_control():
    # positive: an accession URL with no literal DOI still maps deterministically
    assert (
        doi_from_publisher_pattern("https://www.nature.com/articles/s41598-019-42976-3")
        == "10.1038/s41598-019-42976-3"
    )
    # negative: a URL that encodes no DOI must NOT produce one
    oup = "https://academic.oup.com/plphys/article/194/1/258/7273633"
    assert doi_from_url_text(oup) is None
    assert doi_from_publisher_pattern(oup) is None


def test_strip_browser_suffix():
    assert (
        strip_browser_suffix("Plant room in 3D - plant twin - Mozilla Firefox")
        == "Plant room in 3D - plant twin"
    )
    assert strip_browser_suffix("Bare title") == "Bare title"


# --- live: the real history DB -------------------------------------------


@pytest.mark.live
def test_real_firefox_history_is_readable_and_discriminates():
    profs = firefox_profiles()
    if not profs:
        pytest.skip("no Firefox profile on this machine")
    db = snapshot_history(profs[0])
    assert db is not None, "could not snapshot the live history DB"

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = con.execute(
        "select title from moz_places where title is not null "
        "and url like 'http%' order by last_visit_date desc limit 1"
    ).fetchone()
    con.close()
    if not row:
        pytest.skip("history has no titled http visits")

    # positive control: a title that IS in history resolves to a URL
    hit = lookup_by_title(db, row[0])
    assert hit is not None and hit["url"].startswith("http"), (
        "a title read straight out of the DB did not resolve"
    )
    assert hit["match"] == "exact-title"

    # negative control: a title that is not there must return None, not a guess
    assert lookup_by_title(db, "figcite nonexistent title zzqq 8471") is None


@pytest.mark.live
def test_end_to_end_grounding_from_a_real_visit():
    """A real visited paper URL -> grounded DOI, verified in CrossRef."""
    profs = firefox_profiles()
    if not profs:
        pytest.skip("no Firefox profile on this machine")
    db = snapshot_history(profs[0])
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select title, url from moz_places where title is not null "
        "and url glob '*10.[0-9][0-9][0-9][0-9]/*' order by last_visit_date desc limit 20"
    ).fetchall()
    con.close()
    if not rows:
        pytest.skip("no visited URL containing a DOI in this history")

    title, url = rows[0]
    capture = {"title": f"{title} - Mozilla Firefox", "process": "firefox"}
    res = resolve_from_capture(capture, allow_fetch=False)

    assert res["url"] == url, f"resolved the wrong page: {res['url']} != {url}"
    assert res["doi"], f"no DOI from {url}: {res['evidence']}"
    assert res["grounded"] is True, f"exact title match should ground: {res['evidence']}"
    assert "CrossRef" in res["evidence"]


@pytest.mark.live
def test_unknown_title_is_not_grounded_and_says_why():
    capture = {
        "title": "figcite definitely not a real page zzqq - Mozilla Firefox",
        "process": "firefox",
    }
    res = resolve_from_capture(capture, allow_fetch=False)
    assert res["doi"] is None
    assert res["grounded"] is False
    assert res["evidence"], "refused to ground but gave no reason"


@pytest.mark.live
def test_citation_doi_meta_tag_path():
    """The third resolution path: the page declaring its own DOI."""
    doi = doi_from_page_meta("https://academic.oup.com/plphys/article/194/1/258/7273633")
    if doi is None:
        pytest.skip("publisher blocked the fetch (paywall/bot wall) -- path untestable here")
    assert doi.startswith("10."), doi


# --- identifier paths ----------------------------------------------------


def test_pii_normalization_handles_both_publisher_spellings():
    from figcite.browser import _normalize_pii

    # Cell Press punctuates it; Elsevier and CrossRef do not. Same identifier.
    assert _normalize_pii("S1674-2052(18)30156-4") == "S1674205218301564"
    assert _normalize_pii("S1055790325001277") == "S1055790325001277"


def test_lookup_failure_is_reported_not_swallowed(monkeypatch):
    """A throttled or broken lookup must not read as 'this image has no source'.

    This is the exact defect that shipped: a tight loop hit CrossRef's 1-req/sec
    limit and every 429 was caught and returned as None, which the caller could
    only interpret as an absence of provenance.
    """
    import figcite.browser as B
    from figcite.crossref import LookupUnavailable

    def boom(*a, **kw):
        raise RuntimeError("simulated 429")

    monkeypatch.setattr(B, "throttled_get", boom)
    url = "https://www.sciencedirect.com/science/article/abs/pii/S1055790325001277"

    with pytest.raises(LookupUnavailable):
        B.doi_from_alternative_id(url)

    doi, evidence = B.url_to_doi(url, allow_fetch=False)
    assert doi is None
    assert "LOOKUP FAILED" in evidence, f"failure was disguised as absence: {evidence!r}"
    assert "retry" in evidence.lower()

    # positive control: with the lookup WORKING, the same url resolves --
    # otherwise the assertions above would pass on a url that simply has no DOI.
    #
    # This used to be `monkeypatch.undo()` followed by a real call. Two things
    # were wrong with that. `monkeypatch` is function-scoped and shared with
    # conftest's autouse `_block_network` fixture, so `undo()` revoked the
    # network guard and this line reached api.crossref.org on every non-live
    # run -- which made the test fail intermittently under load, when CrossRef
    # rate-limited it. And a positive control that depends on a third party
    # being up can fail for reasons that have nothing to do with this code,
    # which is the opposite of what a control is for.
    class _Ok:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"message": {"items": [{"DOI": "10.1016/j.ympev.2025.108410"}]}}

    monkeypatch.setattr(B, "throttled_get", lambda *a, **kw: _Ok())
    assert B.doi_from_alternative_id(url) == "10.1016/j.ympev.2025.108410"


@pytest.mark.live
def test_real_identifier_paths_resolve_and_discriminate():
    from figcite.browser import doi_from_alternative_id, doi_from_ncbi_id

    # Elsevier PII, Cell Press punctuated PII, PMID, PMCID -- all real, all from
    # this machine's actual history.
    assert (
        doi_from_alternative_id(
            "https://www.sciencedirect.com/science/article/abs/pii/S0092867425003460"
        )
        == "10.1016/j.cell.2025.03.028"
    )
    assert (
        doi_from_alternative_id(
            "https://www.cell.com/molecular-plant/fulltext/S1674-2052(18)30156-4"
        )
        == "10.1016/j.molp.2018.04.006"
    )
    assert doi_from_ncbi_id("https://pubmed.ncbi.nlm.nih.gov/16107481/") == "10.1242/dev.01955"
    assert (
        doi_from_ncbi_id("https://pmc.ncbi.nlm.nih.gov/articles/PMC7140940/")
        == "10.1104/pp.19.01474"
    )

    # negatives: a URL with no such identifier yields nothing, quietly and correctly
    assert doi_from_alternative_id("https://example.org/no/identifier/here") is None
    assert doi_from_ncbi_id("https://example.org/no/identifier/here") is None


@pytest.mark.live
def test_nearest_visit_match_is_never_grounded(monkeypatch):
    """Time-proximity identifies a tab only by guessing, so it cannot auto-confirm.

    Grounding gates the watcher's auto-tagging, so treating a nearest-visit hit
    as evidence would file a citation nobody checked. Written because a mutant
    that forced grounded=True survived the suite.

    Marked live (task-2 review round-1, finding I4): url_to_doi() always
    verifies a URL-derived DOI candidate against the real CrossRef API before
    trusting it, even with allow_fetch=False -- allow_fetch only gates the
    page-scrape fallback, not this verification call. This test's assertion
    that the DOI resolves was silently depending on CrossRef being reachable;
    it just wasn't declared. The new autouse socket-block fixture in
    conftest.py (added for this same finding) caught it.
    """
    import figcite.browser as B

    real_url = "https://nph.onlinelibrary.wiley.com/doi/10.1111/nph.71477"
    monkeypatch.setattr(B, "firefox_profiles", lambda: [__import__("pathlib").Path("/tmp")])
    monkeypatch.setattr(B, "snapshot_history", lambda p: __import__("pathlib").Path("/tmp/x"))
    monkeypatch.setattr(B, "lookup_by_title", lambda db, t: None)
    monkeypatch.setattr(
        B,
        "lookup_by_time",
        lambda db, ts, window_s=180: {
            "url": real_url,
            "title": "whatever",
            "visited": "",
            "match": "nearest-visit",
            "ambiguous": True,
        },
    )

    cap = {
        "title": "Some page - Mozilla Firefox",
        "process": "firefox",
        "captured_local": "2026-08-13T23:00:00-04:00",
    }
    res = B.resolve_from_capture(cap, allow_fetch=False)

    assert res["doi"] == "10.1111/nph.71477", "the DOI itself should still resolve"
    assert res["grounded"] is False, (
        "a nearest-visit guess was marked grounded -- the watcher would auto-cite it"
    )
    assert "guess" in res["evidence"].lower()

    # positive control: the SAME url via an exact title match IS grounded,
    # so this test cannot pass by grounding being permanently off.
    monkeypatch.setattr(
        B,
        "lookup_by_title",
        lambda db, t: {
            "url": real_url,
            "title": t,
            "visited": "",
            "match": "exact-title",
            "ambiguous": False,
        },
    )
    res2 = B.resolve_from_capture(cap, allow_fetch=False)
    assert res2["grounded"] is True, "exact title match should ground"
