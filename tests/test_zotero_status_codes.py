"""`zotero._get` sorts HTTP status into three messages, and nothing drove it.

    403                    -> "refused the API key"
    429 or >= 500          -> "try again later"
    anything else != 200   -> "Zotero returned N for <path>"
    200                    -> the response

Two reduced-ROR mutants survived here because no test in the suite ever put a
non-200 status through this function at all. Both change which MESSAGE a caller
gets, and the messages are the whole point of the split: "try again later" is
advice, and giving it for a 404 sends someone back to retry a request that will
never succeed.

    `r.status_code == 429` -> `>=`   pulls 430..499 into the retry-advice arm.
                                     The `or >= 500` already covers 5xx, so the
                                     difference is exactly the 4xx band above
                                     429 -- a 451 or a 404... no: 404 is below.
                                     431, 451, 499.

    `r.status_code != 200` -> `>`    stops refusing anything BELOW 200. A 1xx
                                     would be returned as a success and its
                                     body parsed as a Zotero payload.

The second is the more interesting one. `requests` normally consumes 1xx
responses itself, so this may be unreachable through the real client -- but the
guard is written as "only 200 is acceptable", and that is the right thing for it
to mean whatever the transport does. A test can put a 1xx in front of it, so the
guard is held to what it says.
"""

from __future__ import annotations

import pytest

from figcite import zotero


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


def _answers(monkeypatch, response):
    def fake_get(url, **kw):
        return response

    monkeypatch.setattr(zotero.requests, "get", fake_get)


def test_a_200_is_returned(monkeypatch):
    """Positive control for every refusal below: a client that rejected
    everything would satisfy all of them."""
    _answers(monkeypatch, FakeResponse(200, [{"key": "AAA"}]))

    r = zotero._get("/items", key="k")

    assert r.status_code == 200
    assert r.json() == [{"key": "AAA"}]


@pytest.mark.parametrize("code", [429, 500, 503])
def test_a_throttle_or_server_fault_says_try_again(monkeypatch, code):
    _answers(monkeypatch, FakeResponse(code))

    with pytest.raises(zotero.LookupUnavailable) as e:
        zotero._get("/items", key="k")
    assert "try again later" in str(e.value), str(e.value)


@pytest.mark.parametrize("code", [404, 400, 431, 451])
def test_an_ordinary_error_is_not_dressed_up_as_a_retry(monkeypatch, code):
    """`== 429` widened to `>=` sweeps 430..499 into the retry-advice arm.

    404 and 400 sort BELOW 429 and are unaffected -- they are here as the
    other half of the bracket. 431 and 451 sort above it, and are what the
    widening actually catches: a real client would be told to wait and try a
    URL again that is never going to work.
    """
    _answers(monkeypatch, FakeResponse(code))

    with pytest.raises(zotero.LookupUnavailable) as e:
        zotero._get("/items", key="k")
    msg = str(e.value)
    assert "try again later" not in msg, f"a {code} was reported as a transient fault: {msg}"
    assert str(code) in msg and "/items" in msg, msg


def test_a_403_keeps_its_own_message(monkeypatch):
    """403 is caught before either of the mutated comparisons, and says
    something a user can act on: the key is wrong, not the server."""
    _answers(monkeypatch, FakeResponse(403))

    with pytest.raises(zotero.LookupUnavailable) as e:
        zotero._get("/items", key="k")
    assert "API key" in str(e.value), str(e.value)


@pytest.mark.parametrize("code", [100, 199])
def test_a_status_below_200_is_still_not_a_success(monkeypatch, code):
    """`!= 200` widened to `> 200` accepts everything below 200.

    The guard says only 200 is acceptable. A 1xx handed to the caller would be
    a response with no Zotero payload, parsed as though it had one.
    """
    _answers(monkeypatch, FakeResponse(code))

    with pytest.raises(zotero.LookupUnavailable):
        zotero._get("/items", key="k")


def test_the_probed_codes_bracket_both_comparisons():
    """The premise for this file. Each mutated comparison needs probes on BOTH
    sides of its constant, and the reason these mutants survived is that the
    suite had none at all."""
    probed = [100, 199, 400, 403, 404, 429, 431, 451, 500, 503]
    assert any(c < 200 for c in probed), probed
    assert any(200 < c < 429 for c in probed), probed
    assert any(429 < c < 500 for c in probed), probed
    assert any(c >= 500 for c in probed), probed
