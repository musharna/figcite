"""One bad request must not erase the answers to every other one.

Found in real execution, not by a test: sweeping a 535-DOI library, Europe PMC
returned a single 504 on the third of 67 batches and every DOI came back
`failed`. `build`'s error boundary enclosed the whole lookup loop, so one
transient blip discarded the 66 batches that had already succeeded -- against
a docstring that promises "one article's failure never stops the rest".

The same defect class as an error boundary that is too NARROW, seen from the
other side: what matters is that the boundary's scope matches the unit of
independent failure, which here is one HTTP request.

The second rule this pins is the project's cardinal one: a batch that could
not be looked up must never be reported as a DOI that is absent from PMC.
"Searched and found nothing" and "could not look" license different actions,
and silently converting an outage into an absence is the exact failure figcite
exists to prevent.
"""

import json

import pytest
import requests

from figcite import corpus, pmc


def _ok(results):
    return json.dumps({"resultList": {"result": results}}).encode()


def _rec(doi, pmcid="PMC1"):
    return {"doi": doi, "pmcid": pmcid, "title": "T", "pubYear": "2020", "isOpenAccess": "Y"}


def _http(code):
    resp = requests.Response()
    resp.status_code = code
    return requests.exceptions.HTTPError(f"{code} Server Error", response=resp)


# ------------------------------------------------- per-batch survival


def test_one_failing_batch_does_not_lose_the_others(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if len(calls) == 2:
            raise _http(504)
        i = len(calls)
        return _ok([_rec(f"10.1/d{i}")])

    monkeypatch.setattr(pmc, "_get", fake_get)
    monkeypatch.setattr(pmc, "_get_with_retry", fake_get)
    out = pmc.lookup_dois([f"10.1/d{i}" for i in range(6)], batch=2)
    assert [r.doi for r in out.records] == ["10.1/d1", "10.1/d3"], (
        "a 504 on the second batch discarded the first and third too"
    )


def test_the_dois_it_could_not_look_up_are_named_not_dropped(monkeypatch):
    def fake_get(url, **kw):
        raise _http(504)

    monkeypatch.setattr(pmc, "_get", fake_get)
    monkeypatch.setattr(pmc, "_get_with_retry", fake_get)
    out = pmc.lookup_dois(["10.1/a", "10.1/b"], batch=2)
    assert out.records == []
    assert set(out.unreachable) == {"10.1/a", "10.1/b"}
    assert "504" in out.unreachable["10.1/a"]


def test_a_successful_sweep_reports_nothing_unreachable(monkeypatch):
    """Positive control: `unreachable` must be able to come back empty."""
    monkeypatch.setattr(pmc, "_get", lambda url, **kw: _ok([_rec("10.1/a")]))
    out = pmc.lookup_dois(["10.1/a"], batch=2)
    assert out.unreachable == {}
    assert [r.doi for r in out.records] == ["10.1/a"]


def test_a_dns_failure_still_aborts_the_whole_sweep(monkeypatch):
    """DNS is total, not per-request. Retrying 67 batches against a name that
    does not resolve wastes minutes to reach the same answer."""

    def fake_get(url, **kw):
        raise pmc.DnsUnreachable("nothing resolves")

    monkeypatch.setattr(pmc, "_get", fake_get)
    monkeypatch.setattr(pmc, "_get_with_retry", fake_get)
    with pytest.raises(pmc.DnsUnreachable):
        pmc.lookup_dois(["10.1/a", "10.1/b"], batch=1)


# ------------------------------------------------- the build's verdict


def test_an_unreachable_doi_is_failed_not_absent(monkeypatch, tmp_path):
    """The cardinal rule. `not-in-europe-pmc` means we asked and it is not
    there; a batch that never returned must never be reported that way."""
    monkeypatch.setattr(corpus, "CORPUS_DIR", tmp_path / "c")
    monkeypatch.setattr(corpus, "DB_PATH", tmp_path / "c" / "f.sqlite")
    monkeypatch.setattr(corpus, "IMAGE_DIR", tmp_path / "c" / "i")
    monkeypatch.setattr(corpus, "DESCRIPTOR_DIR", tmp_path / "c" / "d")
    monkeypatch.setattr(
        pmc,
        "lookup_dois",
        lambda dois, batch=8: pmc.Lookup(records=[], unreachable={"10.1/down": "HTTP 504"}),
    )
    out = {o.doi: o for o in corpus.build(["10.1/down"])}
    assert out["10.1/down"].status == "failed", (
        "an outage was reported as the paper being absent from PMC"
    )
    assert "504" in out["10.1/down"].detail


def test_a_genuinely_absent_doi_is_still_reported_absent(monkeypatch, tmp_path):
    """Positive control: the two verdicts must stay distinguishable."""
    monkeypatch.setattr(corpus, "CORPUS_DIR", tmp_path / "c")
    monkeypatch.setattr(corpus, "DB_PATH", tmp_path / "c" / "f.sqlite")
    monkeypatch.setattr(corpus, "IMAGE_DIR", tmp_path / "c" / "i")
    monkeypatch.setattr(corpus, "DESCRIPTOR_DIR", tmp_path / "c" / "d")
    monkeypatch.setattr(
        pmc,
        "lookup_dois",
        lambda dois, batch=8: pmc.Lookup(records=[], unreachable={}),
    )
    out = {o.doi: o for o in corpus.build(["10.1/ghost"])}
    assert out["10.1/ghost"].status == "not-in-europe-pmc"


# ------------------------------------------------- transient retry


def test_a_transient_5xx_is_retried(monkeypatch):
    """A 504 means "try again", so failing on the first one throws away an
    answer the server was willing to give."""
    n = [0]

    def flaky(url, **kw):
        n[0] += 1
        if n[0] < 3:
            raise _http(503)
        return b"ok"

    monkeypatch.setattr(pmc, "_get", flaky)
    monkeypatch.setattr(pmc, "_RETRY_WAIT", 0)
    assert pmc._get_with_retry("http://x") == b"ok"
    assert n[0] == 3


def test_retries_are_bounded(monkeypatch):
    """Positive control on the retry: it must be able to give up."""
    n = [0]

    def always(url, **kw):
        n[0] += 1
        raise _http(504)

    monkeypatch.setattr(pmc, "_get", always)
    monkeypatch.setattr(pmc, "_RETRY_WAIT", 0)
    with pytest.raises(requests.exceptions.HTTPError):
        pmc._get_with_retry("http://x")
    assert n[0] == pmc.MAX_ATTEMPTS


def test_a_404_is_not_retried(monkeypatch):
    """Only transient codes. Retrying a definite answer just wastes time."""
    n = [0]

    def gone(url, **kw):
        n[0] += 1
        raise _http(404)

    monkeypatch.setattr(pmc, "_get", gone)
    monkeypatch.setattr(pmc, "_RETRY_WAIT", 0)
    with pytest.raises(requests.exceptions.HTTPError):
        pmc._get_with_retry("http://x")
    assert n[0] == 1, "a 404 was retried"
