"""Two decisions the tool exists to get right, neither of which was tested.

Both survived the string sweep, and both have the same shape: the suite
asserts what happens AFTER the distinction is made, and never that the
distinction is made.

1. `crossref.is_retracted` -- `str(msg.get("type","")).lower() == "retraction"`
   survived `-> "MUTANT"`. The only test mentioning retraction is
   `test_cli_confirm_characterization.py:120`, which builds a Record with
   `retracted=True` directly. It proves the WARNING renders; it cannot prove
   anything is ever set.

2. `pmc._get` -- `"NameResolution" in str(e)` survived. `test_pmc_resilience`
   does `raise pmc.DnsUnreachable(...)` by hand and checks the caller copes.
   It proves the recovery works; it cannot prove the failure is ever
   RECOGNISED.

The second is the project's cardinal rule, and the source says so itself:

    "This is a DNS failure, not an absence of figures."

If the recognition stops working, a DNS outage stops raising DnsUnreachable
and surfaces as an ordinary failure -- and a corpus build that reached
nothing reports the same emptiness as a corpus build that found nothing.
An outage folded into an absence is the one error this tool cannot make.
"""

from __future__ import annotations

import socket

import pytest
import requests

from figcite import crossref, pmc

# --- retraction detection ------------------------------------------------


def test_a_work_whose_own_type_is_retraction_is_retracted():
    """The surviving branch. CrossRef gives a retraction NOTICE its own
    `type: "retraction"`, distinct from the retracted article carrying an
    `update-to` pointer, and this is the only check that sees it."""
    assert crossref.is_retracted({"type": "retraction"}) is True


def test_the_type_check_is_case_insensitive():
    """`.lower()` is in the expression on purpose; CrossRef's casing is not
    something to bet a retraction warning on."""
    assert crossref.is_retracted({"type": "Retraction"}) is True


def test_a_work_pointing_at_what_it_retracts_is_retracted():
    """The other branch, via `update-to`. Asserted separately so neither
    branch can cover for the other -- the two copies / two observations rule
    that this audit keeps arriving at."""
    msg = {
        "type": "journal-article",
        "update-to": [{"type": "retraction", "DOI": "10.1/original"}],
    }
    assert crossref.is_retracted(msg) is True


def test_an_ordinary_article_is_not_retracted():
    """Positive control, and the one that matters most.

    "Everything is retracted" passes all three tests above while making the
    warning meaningless -- and a warning that fires on every paper is worse
    than none, because people learn to click past it.
    """
    assert crossref.is_retracted({"type": "journal-article"}) is False
    assert crossref.is_retracted({}) is False
    assert crossref.is_retracted({"type": "journal-article", "update-to": []}) is False


def test_an_unrelated_update_is_not_a_retraction():
    """A correction is an update-to as well, and it is not a retraction."""
    msg = {
        "type": "journal-article",
        "update-to": [{"type": "correction", "DOI": "10.1/original"}],
    }
    assert crossref.is_retracted(msg) is False


# --- a DNS failure is not an absence of figures --------------------------


def _connection_error(message: str, cause: BaseException | None = None):
    err = requests.exceptions.ConnectionError(message)
    if cause is not None:
        err.__cause__ = cause
    return err


def test_a_dns_failure_with_a_gaierror_cause_is_named_as_one(monkeypatch):
    monkeypatch.setattr(pmc, "_last_call", 0.0, raising=False)
    monkeypatch.setattr(
        pmc.requests,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            _connection_error("failed to resolve", cause=socket.gaierror(-2, "Name"))
        ),
    )

    with pytest.raises(pmc.DnsUnreachable) as got:
        pmc._get("https://www.ebi.ac.uk/europepmc/whatever")

    assert "not an absence" in str(got.value), (
        f"the outage was raised without saying it is not an absence: {got.value}"
    )


def test_a_dns_failure_recognised_only_by_its_message_is_named_as_one(monkeypatch):
    """THE surviving branch.

    urllib3 wraps some resolver failures such that `__cause__` is not a
    `socket.gaierror` and only the message says NameResolutionError. That is
    what the string test is for, and nothing drove it.
    """
    monkeypatch.setattr(pmc, "_last_call", 0.0, raising=False)
    monkeypatch.setattr(
        pmc.requests,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            _connection_error(
                "HTTPSConnectionPool(host='www.ebi.ac.uk', port=443): Max retries "
                "exceeded (Caused by NameResolutionError(...))"
            )
        ),
    )

    with pytest.raises(pmc.DnsUnreachable):
        pmc._get("https://www.ebi.ac.uk/europepmc/whatever")


def test_a_connection_failure_that_is_not_dns_is_not_relabelled(monkeypatch):
    """Positive control, and the asymmetric half.

    Without it, "call every ConnectionError a DNS failure" passes both tests
    above. The tool would then tell you your resolver is down when the real
    answer is a refused connection or a dropped TLS handshake -- a confident
    diagnosis of the wrong thing, which is worse than the bare error.
    """
    monkeypatch.setattr(pmc, "_last_call", 0.0, raising=False)
    monkeypatch.setattr(
        pmc.requests,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            _connection_error("Connection refused", cause=ConnectionRefusedError(111))
        ),
    )

    with pytest.raises(requests.exceptions.ConnectionError) as got:
        pmc._get("https://www.ebi.ac.uk/europepmc/whatever")

    assert not isinstance(got.value, pmc.DnsUnreachable), (
        "a refused connection was reported as a DNS failure"
    )
