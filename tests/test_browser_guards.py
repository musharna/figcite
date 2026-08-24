"""browser.py guards: the identifier paths, CrossRef verification, and history.

Thirteen survivors sat here, and most of them for one structural reason: the
NCBI and PII identifier paths are exercised only by `@pytest.mark.live` tests.
Those talk to NCBI and CrossRef over the network, so they are deselected on
every ordinary run -- and a mutation sweep runs `-m "not live"`. The code was
covered by tests that never ran, which is indistinguishable from not being
covered at all.

Everything here drives the same code paths with the HTTP responses supplied,
so the branch logic is checked on every run and the live tests keep doing what
only they can do: prove the real endpoints still return what we parse.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from figcite import browser as B


# ------------------------------------------------- snapshotting the history


def test_a_profile_with_a_places_db_is_snapshotted(tmp_path):
    """`if not src.exists()` survived dropping its `not`.

    Inverted, every profile that HAS a history database is skipped and every
    profile that does not is copied -- so history grounding stops working
    everywhere at once, and reports "no profile" on the machines where it
    would have worked.
    """
    profile = tmp_path / "abc.default"
    profile.mkdir()
    (profile / "places.sqlite").write_bytes(b"SQLite format 3\x00" + b"\0" * 64)

    got = B.snapshot_history(profile)

    assert got is not None, "a profile with a places.sqlite was not snapshotted"
    assert got.exists() and got.name == "places.sqlite"
    assert got != profile / "places.sqlite", "the live database was handed back"


def test_a_profile_without_a_places_db_is_not_snapshotted(tmp_path):
    """Positive control: "always copy" passes the test above and then dies on
    a missing file deeper in."""
    profile = tmp_path / "empty.default"
    profile.mkdir()

    assert B.snapshot_history(profile) is None


# ------------------------------------------------- NCBI: PMC *or* PMID


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _ncbi(monkeypatch, payload):
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        seen["params"] = kw.get("params")
        return _Resp(payload)

    monkeypatch.setattr(B.requests, "get", fake_get)
    return seen


def test_a_pmc_url_alone_resolves(monkeypatch):
    """`if not (pmc or pmid)` survived `Or -> And`.

    A URL carries one identifier or the other, never both. Under `and` the
    guard reads "return None unless BOTH are present", so it returns None for
    every real URL and the whole NCBI path goes dark -- silently, because
    returning None here is indistinguishable from "this URL has no
    identifier".
    """
    seen = _ncbi(monkeypatch, {"records": [{"doi": "10.1104/pp.19.01474"}]})

    got = B.doi_from_ncbi_id("https://pmc.ncbi.nlm.nih.gov/articles/PMC7140940/")

    assert got == "10.1104/pp.19.01474", got
    assert "idconv" in seen["url"], (
        f"a PMCID was not sent to the id converter: {seen['url']}"
    )


def test_a_pmid_url_alone_resolves(monkeypatch):
    """The other operand on its own -- and a different endpoint.

    A PMID need not be in PMC at all, so idconv is the wrong service for it;
    the code routes PMIDs to esummary. A fixture carrying only one of the two
    identifier kinds cannot observe that the split exists.
    """
    seen = _ncbi(
        monkeypatch,
        {
            "result": {
                "16107481": {
                    "articleids": [{"idtype": "doi", "value": "10.1242/dev.01955"}]
                }
            }
        },
    )

    got = B.doi_from_ncbi_id("https://pubmed.ncbi.nlm.nih.gov/16107481/")

    assert got == "10.1242/dev.01955", got
    assert "esummary" in seen["url"], (
        f"a PMID was sent to the wrong endpoint: {seen['url']}"
    )


def test_a_url_with_neither_identifier_asks_ncbi_nothing(monkeypatch):
    """Positive control: "always call NCBI" passes both tests above and puts a
    network round trip on every URL figcite ever sees."""
    called = []
    monkeypatch.setattr(B.requests, "get", lambda *a, **kw: called.append(1))

    assert B.doi_from_ncbi_id("https://example.org/no/identifier/here") is None
    assert not called, "NCBI was queried for a URL with no identifier in it"


def test_the_doi_is_taken_from_the_articleid_of_type_doi(monkeypatch):
    """`if a.get("idtype") == "doi" and a.get("value")` survived four mutants.

    esummary returns every identifier a record has, in one list, distinguished
    only by `idtype`. The decoys make each operand wrong on its own: a pubmed
    id that HAS a value, and a doi entry whose value is empty. Under `or` the
    pubmed id is returned as though it were a DOI -- "16107481" filed as the
    citation for a figure. Under either mutated key name nothing matches and a
    resolvable paper reports no DOI.
    """
    _ncbi(
        monkeypatch,
        {
            "result": {
                "16107481": {
                    "articleids": [
                        {"idtype": "pubmed", "value": "16107481"},
                        {"idtype": "doi", "value": ""},
                        {"idtype": "doi", "value": "10.1242/dev.01955"},
                    ]
                }
            }
        },
    )

    got = B.doi_from_ncbi_id("https://pubmed.ncbi.nlm.nih.gov/16107481/")

    assert got == "10.1242/dev.01955", (
        f"the wrong identifier was returned as the DOI: {got!r}"
    )


def test_a_record_with_no_doi_articleid_yields_nothing(monkeypatch):
    """Positive control: "return the first id you see" passes the test above."""
    _ncbi(
        monkeypatch,
        {
            "result": {
                "16107481": {"articleids": [{"idtype": "pubmed", "value": "16107481"}]}
            }
        },
    )

    assert B.doi_from_ncbi_id("https://pubmed.ncbi.nlm.nih.gov/16107481/") is None


# --------------------------------- every candidate is verified in CrossRef


@pytest.fixture
def _no_network(monkeypatch):
    """Nothing in these tests may reach out; each supplies its own answers."""
    monkeypatch.setattr(
        B.requests, "get", lambda *a, **kw: pytest.fail("unexpected network call")
    )


def test_a_doi_read_out_of_the_url_is_returned_once_crossref_confirms_it(
    monkeypatch, _no_network
):
    """`if fetch_work(cand) is not None` survived `IsNot -> Is` at four sites.

    Inverted, the rule becomes "use the DOI only if CrossRef does NOT have
    it": every real DOI is discarded and every DOI that resolves to nothing is
    accepted. This is the check that stops a plausible-looking string in an
    address bar being filed as a citation.
    """
    monkeypatch.setattr(B, "fetch_work", lambda doi: {"DOI": doi})

    doi, evidence = B.url_to_doi(
        "https://nph.onlinelibrary.wiley.com/doi/10.1111/nph.71477", allow_fetch=False
    )

    assert doi == "10.1111/nph.71477", (doi, evidence)
    assert "verified in CrossRef" in evidence, evidence


def test_a_doi_crossref_does_not_have_is_not_returned(monkeypatch, _no_network):
    """Positive control, and the whole point of verifying: a well-formed DOI
    that resolves to nothing is not a citation."""
    monkeypatch.setattr(B, "fetch_work", lambda doi: None)

    doi, evidence = B.url_to_doi(
        "https://nph.onlinelibrary.wiley.com/doi/10.1111/nph.00000", allow_fetch=False
    )

    assert doi is None, (doi, evidence)


def test_an_identifier_resolved_doi_is_also_verified(monkeypatch, _no_network):
    """The second verification site: a DOI recovered from a PMID/PMC/PII
    identifier goes through exactly the same check."""
    monkeypatch.setattr(B, "doi_from_ncbi_id", lambda url: "10.1242/dev.01955")
    monkeypatch.setattr(B, "doi_from_alternative_id", lambda url: None)
    monkeypatch.setattr(B, "fetch_work", lambda doi: {"DOI": doi})

    doi, evidence = B.url_to_doi(
        "https://pubmed.ncbi.nlm.nih.gov/16107481/", allow_fetch=False
    )

    assert doi == "10.1242/dev.01955", (doi, evidence)
    assert "verified in CrossRef" in evidence, evidence


def test_an_identifier_resolved_doi_crossref_rejects_is_dropped(
    monkeypatch, _no_network
):
    """Positive control for that second site."""
    monkeypatch.setattr(B, "doi_from_ncbi_id", lambda url: "10.1242/dev.01955")
    monkeypatch.setattr(B, "doi_from_alternative_id", lambda url: None)
    monkeypatch.setattr(B, "fetch_work", lambda doi: None)

    doi, _ = B.url_to_doi(
        "https://pubmed.ncbi.nlm.nih.gov/16107481/", allow_fetch=False
    )

    assert doi is None, doi


def test_a_citation_doi_meta_tag_is_also_verified(monkeypatch, _no_network):
    """The third site, reached only when `allow_fetch` permits loading the
    page itself."""
    monkeypatch.setattr(B, "doi_from_page_meta", lambda url, **kw: "10.1111/nph.71477")
    monkeypatch.setattr(B, "doi_from_ncbi_id", lambda url: None)
    monkeypatch.setattr(B, "doi_from_alternative_id", lambda url: None)
    monkeypatch.setattr(B, "fetch_work", lambda doi: {"DOI": doi})

    doi, evidence = B.url_to_doi("https://example.org/article", allow_fetch=True)

    assert doi == "10.1111/nph.71477", (doi, evidence)
    assert "citation_doi" in evidence, evidence


def test_a_meta_tag_doi_crossref_rejects_is_dropped(monkeypatch, _no_network):
    """Positive control for the third site. A page can claim any DOI it
    likes in a meta tag; CrossRef is what makes it evidence."""
    monkeypatch.setattr(B, "doi_from_page_meta", lambda url, **kw: "10.1111/nph.00000")
    monkeypatch.setattr(B, "doi_from_ncbi_id", lambda url: None)
    monkeypatch.setattr(B, "doi_from_alternative_id", lambda url: None)
    monkeypatch.setattr(B, "fetch_work", lambda doi: None)

    doi, _ = B.url_to_doi("https://example.org/article", allow_fetch=True)

    assert doi is None, doi


# ----------------------------------- title first, time only as a fallback


def _history(monkeypatch, tmp_path, *, title_hit, time_hit):
    db = tmp_path / "places.sqlite"
    db.write_bytes(b"SQLite format 3\x00")
    monkeypatch.setattr(B, "firefox_profiles", lambda: [tmp_path])
    monkeypatch.setattr(B, "snapshot_history", lambda p: db)
    monkeypatch.setattr(B, "lookup_by_title", lambda d, t: title_hit)
    calls = []

    def _by_time(d, ts, **kw):
        calls.append(ts)
        return time_hit

    monkeypatch.setattr(B, "lookup_by_time", _by_time)
    monkeypatch.setattr(B, "url_to_doi", lambda u, allow_fetch=True: ("10.1/x", "why"))
    return calls


def _hit(url, match="exact-title"):
    return {"url": url, "title": "T", "match": match, "ambiguous": False, "when": ""}


def test_a_title_match_is_not_second_guessed_by_the_clock(monkeypatch, tmp_path):
    """`if hit is None and capture.get("captured_local")` survived `Is -> IsNot`
    and `And -> Or`.

    Title matching is EVIDENCE of which page was on screen; nearest-visit is a
    GUESS, and the module is explicit that a guess must not ground a citation.
    Both mutants run the clock lookup after a successful title match and let
    its answer replace the evidence -- so the strong result is overwritten by
    the weak one, and `grounded` goes on saying True.
    """
    calls = _history(
        monkeypatch,
        tmp_path,
        title_hit=_hit("https://example.org/right"),
        time_hit=_hit("https://example.org/wrong", match="nearest-visit"),
    )

    out = B.resolve_from_capture(
        {"title": "A paper", "captured_local": datetime.now().isoformat()},
        allow_fetch=False,
    )

    assert out["url"] == "https://example.org/right", out
    assert not calls, (
        "the nearest-visit fallback ran even though the title matched exactly"
    )
    assert out["grounded"] is True, out


def test_the_clock_fallback_runs_when_the_title_does_not_match(monkeypatch, tmp_path):
    """`capture.get("captured_local")` survived mutating the key name.

    Under the mutant the key is never found, the fallback never runs, and
    every capture whose window title does not match history reports "no
    history entry" -- losing the one recovery path that exists for a page
    whose title Firefox recorded differently.
    """
    calls = _history(
        monkeypatch,
        tmp_path,
        title_hit=None,
        time_hit=_hit("https://example.org/near", match="nearest-visit"),
    )

    out = B.resolve_from_capture(
        {"title": "A paper", "captured_local": "2026-08-23T10:00:00"},
        allow_fetch=False,
    )

    assert calls, "the nearest-visit fallback did not run after a title miss"
    assert out["url"] == "https://example.org/near", out
    assert out["grounded"] is False, (
        "a nearest-visit guess was reported as evidence of which tab was showing"
    )


def test_a_capture_with_no_timestamp_gets_no_clock_fallback(monkeypatch, tmp_path):
    """The second operand alone. With no capture time there is nothing to be
    near, and the answer is an honest miss rather than a guess."""
    calls = _history(
        monkeypatch,
        tmp_path,
        title_hit=None,
        time_hit=_hit("https://example.org/near", match="nearest-visit"),
    )

    out = B.resolve_from_capture({"title": "A paper"}, allow_fetch=False)

    assert not calls, "a nearest-visit lookup ran with no capture time to anchor it"
    assert out["doi"] is None
    assert "no history entry" in out["evidence"], out["evidence"]
    # The second half of that sentence is the actionable part -- it tells the
    # reader the miss may be their own private window rather than a broken
    # tool. Asserted separately because a substring check on the first clause
    # alone leaves the explanation free to be deleted.
    assert "private-browsing" in out["evidence"], out["evidence"]
