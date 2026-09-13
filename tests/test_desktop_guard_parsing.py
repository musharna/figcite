"""The PowerPoint guard's probe, pinned -- and NOT `live`, on purpose.

The guard that decides whether it is safe to drive PowerPoint lives in a
module every safe run deselects. Its parsing is pure string handling with no
COM in it, so it can and must be checked by the ordinary suite: a safety
predicate whose only tests are behind the flag it exists to make safe is not
protected at all.

Both defects below shipped, hours apart, in the guard written to prevent a
data-loss incident:

1. FAIL-OPEN. The probe returned "" on timeout, missing executable, or
   permission error, and the caller read that silence as "PowerPoint is not
   running" -- so an outage AUTHORISED the COM automation. An outage folded
   into an absence, inside the check meant to stop it.

2. SUBSTRING. The fix then asked `"NOT-RUNNING" not in out`, and the busy
   sentinel CONTAINS the idle one, so the output "RUNNING\nNOT-RUNNING" also
   authorised COM. My own four-state check missed it because its garbage case
   was unrelated text rather than contaminated text -- a control whose inputs
   did not include the adversarial one.

Hence the shape of this file: every case asserts which way the guard FAILS,
and the sentinel-contamination case is the one that matters most.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "ran,stdout,expect_busy,why",
    [
        (True, "NOT-RUNNING\n", False, "the ONLY input that may authorise COM"),
        (True, "RUNNING\n", True, "a human has PowerPoint open"),
        (False, "", True, "probe timed out or could not run -- an outage"),
        (False, "NOT-RUNNING\n", True, "nonzero exit, whatever it printed"),
        (True, "", True, "ran but said nothing"),
        (True, "wat\n", True, "unrecognised output"),
        (True, "RUNNING\nNOT-RUNNING\n", True, "SENTINEL CONTAMINATION"),
        (True, "NOT-RUNNING-YET\n", True, "idle sentinel as a prefix"),
        (True, "  NOT-RUNNING  \n", False, "whitespace is not contamination"),
    ],
    ids=[
        "idle",
        "busy",
        "probe-failed",
        "nonzero-exit",
        "empty",
        "garbage",
        "contaminated",
        "prefix",
        "whitespace",
    ],
)
def test_only_a_positive_idle_report_authorises_com(
    ran, stdout, expect_busy, why, monkeypatch, ppt_module
):
    """Anything that is not an exact, successful "NOT-RUNNING" must refuse.

    `expect_busy` is the safety direction: True means "refuse to touch
    PowerPoint". Only one row in this table is allowed to be False.
    """
    monkeypatch.setattr(ppt_module, "_pwsh", lambda cmd, timeout=90: (ran, stdout))

    assert ppt_module._powerpoint_already_running() is expect_busy, why


def test_exactly_one_input_authorises_com(ppt_module, monkeypatch):
    """A guard that refused everything would pass every row above.

    This is the positive control for the table: the idle case really does get
    through, so the assertions are discriminating rather than uniformly
    paranoid.
    """
    monkeypatch.setattr(ppt_module, "_pwsh", lambda cmd, timeout=90: (True, "NOT-RUNNING\n"))
    assert ppt_module._powerpoint_already_running() is False


def test_installed_is_also_matched_exactly(ppt_module, monkeypatch):
    """Same defect class, same fix, other probe.

    `"INSTALLED" in out` would accept "NOT-INSTALLED", which is the identical
    substring trap one word over.
    """
    monkeypatch.setattr(ppt_module, "_pwsh", lambda cmd, timeout=90: (True, "NOT-INSTALLED\n"))
    assert ppt_module._powerpoint_available() is False

    monkeypatch.setattr(ppt_module, "_pwsh", lambda cmd, timeout=90: (True, "INSTALLED\n"))
    assert ppt_module._powerpoint_available() is True
