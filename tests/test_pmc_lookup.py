"""DOI -> PMCID via Europe PMC.

Batched because a real library has hundreds of DOIs and the API takes an OR
query. conftest blocks the network for non-live tests, so these drive a stubbed
transport; tests/test_pmc_live.py covers the real boundary.
"""

import json

from figcite import pmc


def _fake_response(results):
    return json.dumps({"resultList": {"result": results}}).encode()


def test_a_doi_that_is_open_access_is_reported_as_such(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _fake_response(
            [
                {
                    "doi": "10.1/aa",
                    "pmcid": "PMC1",
                    "title": "A",
                    "pubYear": "2020",
                    "isOpenAccess": "Y",
                },
            ]
        )

    monkeypatch.setattr(pmc, "_get", fake_get)
    out = pmc.lookup_dois(["10.1/aa"])
    assert len(out) == 1
    assert out[0].pmcid == "PMC1"
    assert out[0].is_open_access is True
    assert len(calls) == 1


def test_a_closed_access_hit_is_kept_but_flagged(monkeypatch):
    """We still want to know the paper exists; it just cannot be indexed."""
    monkeypatch.setattr(
        pmc,
        "_get",
        lambda url, **kw: _fake_response(
            [
                {
                    "doi": "10.1/bb",
                    "pmcid": "PMC2",
                    "title": "B",
                    "pubYear": "2021",
                    "isOpenAccess": "N",
                },
            ]
        ),
    )
    out = pmc.lookup_dois(["10.1/bb"])
    assert out[0].is_open_access is False


def test_dois_are_batched_not_queried_one_at_a_time(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pmc, "_get", lambda url, **kw: calls.append(url) or _fake_response([])
    )
    pmc.lookup_dois([f"10.1/{i}" for i in range(20)], batch=8)
    assert len(calls) == 3, f"expected 3 batched calls, got {len(calls)}"


def test_a_doi_with_no_pmc_record_simply_returns_nothing(monkeypatch):
    """Positive control: absence here is data, not an error."""
    monkeypatch.setattr(pmc, "_get", lambda url, **kw: _fake_response([]))
    assert pmc.lookup_dois(["10.1/none"]) == []
