"""The Zotero HTTP client, which decides outage-vs-absence and had no tests.

The mutation sweep flagged `zotero._get`'s status handling, and triage found
something larger than the branch it pointed at: **nothing in the suite touched
`_get`, `fetch_library`, `sync`, or `library` at all**. Every existing test
stubs `zotero.library` wholesale, so the layer that actually talks to a server
-- the only layer that can tell "your library does not contain this" from "I
could not ask" -- was invisible to the gate while the tests above it stayed
green. That is the failure mode this project cares about most, tested nowhere.

What the two survivors actually do, because they are not equally severe:

  `r.status_code == 429`  ->  `!=`
      Severe, and not in the direction the branch name suggests. Under `!=`, a
      **200** satisfies `200 != 429` and raises "try again later", so every
      successful response becomes an outage and the library is permanently
      unreachable. It survived because no test ever drove a success through
      `_get`. The happy-path test below is what kills it.

  `... or r.status_code >= 500`  ->  `and`
      Near-equivalent, and worth saying so rather than implying a catch. Under
      `and` the branch is dead (429 is not >= 500, 503 is not == 429), but both
      codes then fall to the `!= 200` catch-all and still raise
      LookupUnavailable -- which `resolve` still turns into `available=False`.
      The OUTCOME contract cannot distinguish it; only the message can. So the
      message assertions here are doing real work, and they are the only thing
      separating the retry-able branch from the terminal one.

The catch-all is why the shipped CrossRef defect (a 429 swallowed as an
absence) cannot happen here: every non-200 raises. The tests still pin it,
because that backstop is one edit away from being removed.
"""

from __future__ import annotations

import json
import time

import pytest

from figcite import zotero
from figcite.crossref import LookupUnavailable

KEY, LIB, TYP = "not-a-real-key", "9999999", "user"


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = [] if payload is None else payload

    def json(self):
        return self._payload


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Credentials and a cache dir, with no real network anywhere.

    conftest blocks sockets, so an un-stubbed `requests.get` would raise and be
    caught by `_get`'s `except Exception` -- turning a missing stub into a
    passing "outage" test. Every test here therefore installs its own
    transport and asserts on what it was called with.
    """
    monkeypatch.setattr(zotero, "credentials", lambda: (KEY, LIB, TYP))
    monkeypatch.setattr(zotero, "CACHE_DIR", tmp_path)
    return tmp_path


def _transport(monkeypatch, *responses, record=None):
    """Install a fake `requests.get` returning `responses` in order."""
    calls = record if record is not None else []
    seq = list(responses)

    def fake_get(url, **kw):
        calls.append({"url": url, **kw})
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(zotero.requests, "get", fake_get)
    return calls


# --- _get: which statuses mean what --------------------------------------


def test_a_successful_response_is_handed_back(wired, monkeypatch):
    """THE positive control, and the test that kills `== 429` -> `!= 429`.

    Under that mutant a 200 raises "try again later" and the library is dead
    forever. Nothing in the suite drove a success through this function, which
    is exactly why a catastrophic mutant looked harmless.
    """
    _transport(monkeypatch, FakeResponse(200, [{"data": {"title": "x"}}]))

    r = zotero._get("/users/9999999/items", KEY)

    assert r.status_code == 200
    assert r.json() == [{"data": {"title": "x"}}]


def test_the_api_key_is_sent_as_a_header_not_a_query_parameter(wired, monkeypatch):
    """A key in the URL lands in server logs and in `~/.zsh_history` via any
    proxy or curl reproduction. It belongs in the header."""
    calls = _transport(monkeypatch, FakeResponse(200))

    zotero._get("/users/9999999/items", KEY, format="json")

    sent = calls[0]
    assert sent["headers"]["Zotero-API-Key"] == KEY
    assert sent["headers"]["Zotero-API-Version"] == zotero.API_VERSION
    assert KEY not in sent["url"], "the API key was interpolated into the URL"
    assert KEY not in json.dumps(sent["params"]), "the API key went into params"
    assert sent["timeout"] == zotero.TIMEOUT, "an un-timed request can hang forever"


def test_a_rate_limit_is_an_outage_and_says_to_retry(wired, monkeypatch):
    """429 must not read as a hard failure: the answer may well exist, we were
    just asked to come back. The wording is what distinguishes this branch from
    the terminal catch-all, and it is the ONLY thing that does."""
    _transport(monkeypatch, FakeResponse(429))

    with pytest.raises(LookupUnavailable) as e:
        zotero._get("/users/9999999/items", KEY)

    assert "429" in str(e.value)
    assert "try again later" in str(e.value), (
        "a rate limit was reported with the terminal wording, so a retry-able "
        "outage is indistinguishable from a permanent error"
    )


@pytest.mark.parametrize("code", [500, 502, 503, 599])
def test_a_server_error_is_an_outage_and_says_to_retry(wired, monkeypatch, code):
    _transport(monkeypatch, FakeResponse(code))

    with pytest.raises(LookupUnavailable) as e:
        zotero._get("/users/9999999/items", KEY)

    assert str(code) in str(e.value)
    assert "try again later" in str(e.value)


def test_a_refused_key_names_the_key_rather_than_the_library(wired, monkeypatch):
    """403 is the one status a user can act on, and the action is "check your
    key", not "add the paper to Zotero". Backlog item 3."""
    _transport(monkeypatch, FakeResponse(403))

    with pytest.raises(LookupUnavailable) as e:
        zotero._get("/users/9999999/items", KEY)

    msg = str(e.value)
    assert "403" in msg
    assert "key" in msg.lower(), msg
    assert "try again later" not in msg, (
        "a refused key was reported as a transient outage, so the user is told "
        "to wait for something that will never fix itself"
    )


def test_an_unexpected_status_is_terminal_not_retryable(wired, monkeypatch):
    """404 is the discriminator between the retry branch and the catch-all: it
    must raise, and must NOT claim retrying helps."""
    _transport(monkeypatch, FakeResponse(404))

    with pytest.raises(LookupUnavailable) as e:
        zotero._get("/users/9999999/items", KEY)

    assert "404" in str(e.value)
    assert "try again later" not in str(e.value)


def test_a_transport_failure_is_an_outage_not_a_crash(wired, monkeypatch):
    """DNS down, TLS refused, timeout. `figcite` must degrade to "could not
    ask", never propagate a raw requests exception into a caption path."""

    def boom(url, **kw):
        raise OSError("network is unreachable")

    monkeypatch.setattr(zotero.requests, "get", boom)

    with pytest.raises(LookupUnavailable) as e:
        zotero._get("/users/9999999/items", KEY)

    assert "network is unreachable" in str(e.value), (
        "the underlying cause was dropped, leaving an outage with no diagnosis"
    )


# --- the contract that actually matters ----------------------------------


def test_an_outage_mid_sync_is_not_reported_as_a_missing_paper(wired, monkeypatch):
    """THE finding, stated end to end rather than as a branch.

    A 429 while refreshing the snapshot has to surface as `available=False`.
    If it ever surfaced as a normal empty result, figcite would tell you a
    paper is not in your library at the exact moment it could not look -- the
    project's cardinal sin, and the defect that already shipped once here in
    the CrossRef path.
    """
    _transport(monkeypatch, FakeResponse(429))

    out = zotero.resolve("A Paper With A Sufficiently Long Title")

    assert out["available"] is False, f"an outage was reported as an available library: {out}"
    assert out["grounded"] is False
    assert out["doi"] is None
    assert "NOT a miss" in out["evidence"], out["evidence"]
    assert "429" in out["evidence"], f"the outage did not say what happened: {out['evidence']}"


def test_a_genuine_miss_still_reads_as_available(wired, monkeypatch):
    """The positive control for the test above, and the asymmetry that makes
    it mean something: if `resolve` returned `available=False` for everything,
    that test would pass while the feature was useless.

    Same call, same library, reachable server, title genuinely absent."""
    _transport(
        monkeypatch,
        FakeResponse(200, [{"data": {"key": "AAA", "title": "Something Else Entirely"}}]),
    )

    out = zotero.resolve("A Paper With A Sufficiently Long Title")

    assert out["available"] is True, f"a reachable library read as unavailable: {out}"
    assert out["doi"] is None
    assert "no Zotero item titled like" in out["evidence"], out["evidence"]


# --- fetch_library: paging and the hollow-twin filter ---------------------


def test_paging_continues_until_a_short_page(wired, monkeypatch):
    """`if len(batch) < PAGE: break` and `start += PAGE` decide whether a
    library larger than one page is ever fully seen. A single-page fixture
    cannot fail on either."""
    monkeypatch.setattr(zotero, "PAGE", 2)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    page1 = FakeResponse(
        200,
        [
            {"data": {"key": "A", "title": "First Paper", "DOI": "10.1/a"}},
            {"data": {"key": "B", "title": "Second Paper", "DOI": "10.1/b"}},
        ],
    )
    page2 = FakeResponse(200, [{"data": {"key": "C", "title": "Third Paper", "DOI": "10.1/c"}}])
    calls = _transport(monkeypatch, page1, page2)

    items = zotero.fetch_library()

    assert [i["key"] for i in items] == ["A", "B", "C"], items
    assert len(calls) == 2, f"expected two pages, got {len(calls)}"
    assert calls[0]["params"]["start"] == 0
    assert calls[1]["params"]["start"] == 2, (
        f"the second page re-requested the first: start={calls[1]['params']['start']}"
    )


def test_a_page_boundary_that_lands_exactly_is_still_followed(wired, monkeypatch):
    """The off-by-one that a short-page fixture hides: when the last page is
    exactly PAGE long, the loop must ask once more and stop on the empty
    reply rather than assuming it is done."""
    monkeypatch.setattr(zotero, "PAGE", 2)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    full = FakeResponse(
        200,
        [
            {"data": {"key": "A", "title": "First Paper"}},
            {"data": {"key": "B", "title": "Second Paper"}},
        ],
    )
    empty = FakeResponse(200, [])
    calls = _transport(monkeypatch, full, empty)

    items = zotero.fetch_library()

    assert [i["key"] for i in items] == ["A", "B"]
    assert len(calls) == 2


def test_an_item_with_no_title_is_dropped(wired, monkeypatch):
    """Attachments and notes carry their parent's identity but no title, and
    would otherwise double the library with hollow twins that match nothing."""
    monkeypatch.setattr(zotero, "PAGE", 5)
    _transport(
        monkeypatch,
        FakeResponse(
            200,
            [
                {"data": {"key": "A", "title": "A Real Paper", "DOI": "10.1/a"}},
                {"data": {"key": "B", "title": ""}},
                {"data": {"key": "C"}},
            ],
        ),
    )

    items = zotero.fetch_library()

    assert [i["key"] for i in items] == ["A"], items


def test_attachments_and_notes_are_excluded_server_side(wired, monkeypatch):
    """The filter is a query parameter, not a client-side pass. If it stops
    being sent, the client silently pays for and parses every attachment."""
    monkeypatch.setattr(zotero, "PAGE", 5)
    calls = _transport(monkeypatch, FakeResponse(200, []))

    zotero.fetch_library()

    assert calls[0]["params"]["itemType"] == "-attachment || note", calls[0]["params"]


# --- library(): when the snapshot is trusted -----------------------------


def test_a_fresh_snapshot_is_used_without_touching_the_network(wired, monkeypatch):
    """The whole point of the cache. A test that only checks the returned
    items would pass while every lookup hit the API."""
    snapshot = {"items": [{"key": "A", "title": "Cached Paper", "doi": "10.1/a"}]}
    (wired / f"{TYP}-{LIB}.json").write_text(json.dumps(snapshot), encoding="utf-8")

    def forbidden(url, **kw):
        raise AssertionError("a fresh snapshot still went to the network")

    monkeypatch.setattr(zotero.requests, "get", forbidden)

    assert zotero.library()[0]["key"] == "A"


def test_a_stale_snapshot_is_refreshed(wired, monkeypatch):
    """Positive control for the test above: 'never sync' also passes it."""
    cf = wired / f"{TYP}-{LIB}.json"
    stale = {"items": [{"key": "OLD", "title": "Stale Paper", "doi": "10.1/old"}]}
    cf.write_text(json.dumps(stale), encoding="utf-8")
    old = time.time() - (zotero.DEFAULT_TTL_HOURS + 1) * 3600
    import os

    os.utime(cf, (old, old))

    monkeypatch.setattr(zotero, "PAGE", 5)
    _transport(
        monkeypatch,
        FakeResponse(200, [{"data": {"key": "NEW", "title": "Fresh Paper"}}]),
    )

    assert [i["key"] for i in zotero.library()] == ["NEW"]


def test_a_corrupt_snapshot_is_refetched_rather_than_raising(wired, monkeypatch):
    """A truncated write must not make the library permanently unreadable."""
    (wired / f"{TYP}-{LIB}.json").write_text('{"items": [', encoding="utf-8")

    monkeypatch.setattr(zotero, "PAGE", 5)
    _transport(
        monkeypatch,
        FakeResponse(200, [{"data": {"key": "NEW", "title": "Fresh Paper"}}]),
    )

    assert [i["key"] for i in zotero.library()] == ["NEW"]


def test_an_outage_does_not_overwrite_a_good_snapshot(wired, monkeypatch):
    """If a refresh fails, yesterday's library must still be on disk. Writing
    the cache before the fetch completes would destroy it."""
    cf = wired / f"{TYP}-{LIB}.json"
    good = {"items": [{"key": "KEEP", "title": "Kept Paper", "doi": "10.1/keep"}]}
    cf.write_text(json.dumps(good), encoding="utf-8")
    old = time.time() - (zotero.DEFAULT_TTL_HOURS + 1) * 3600
    import os

    os.utime(cf, (old, old))

    _transport(monkeypatch, FakeResponse(503))

    with pytest.raises(LookupUnavailable):
        zotero.library()

    assert json.loads(cf.read_text(encoding="utf-8")) == good, (
        "a failed refresh destroyed the previous snapshot"
    )


def test_sync_reports_what_it_wrote(wired, monkeypatch):
    monkeypatch.setattr(zotero, "PAGE", 5)
    _transport(
        monkeypatch,
        FakeResponse(
            200,
            [
                {"data": {"key": "A", "title": "With DOI", "DOI": "10.1/a"}},
                {"data": {"key": "B", "title": "No DOI"}},
            ],
        ),
    )

    summary = zotero.sync()

    assert summary["items"] == 2
    assert summary["with_doi"] == 1, f"the DOI tally does not discriminate: {summary}"
    assert (
        json.loads((wired / f"{TYP}-{LIB}.json").read_text(encoding="utf-8"))["items"][0]["key"]
        == "A"
    )
