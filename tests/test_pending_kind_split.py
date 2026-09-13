"""A pending list holds two kinds of thing, and nothing tested them together.

`PendingItem.kind` is `"staged"` or `"filed"` and nothing else. Three places
split on it: `cmd_pending` renders two sections, `cmd_confirm` chooses which
pool `m0` and `0` index into, and `pending_items` assigns the two label series.

Every test that reached those splits had ONE kind present. A predicate that
sorts a single-kind list cannot be observed to sort it wrongly, so all three
splits were free to be wrong in a way no assertion could see -- which the
reduced-ROR sweep found as five surviving mutants:

    cmd_pending    kind == "staged" -> <=      a filed item listed as staged
    cmd_pending    kind == "filed"  -> >=      a staged item listed as filed
    cmd_confirm    kind == "staged" -> <=      `confirm 0` reaching a filed one
    cmd_confirm    kind == "filed"  -> >=      `confirm m0` reaching a staged one
    pending_items  kind == "filed"  -> >=      both series labelling one item

They survive because `"filed" < "staged"`: `<=` admits filed wherever staged
was meant, and `>=` admits staged wherever filed was meant. The other direction
at each site is genuinely equivalent over a two-value domain, and is certified
as such in tests/test_demoted_equivalence_claims.py rather than tested here --
there is no input that distinguishes it, so a test claiming to would be lying.

The `confirm` one is the one with teeth. `figcite confirm m0` writes a citation
into whichever capture the filed pool's first entry names; if a staged capture
can enter that pool, the DOI a human just vouched for lands on a different
image than the one they were looking at.
"""

import json

import pytest
from PIL import Image

from figcite import cli, service, store
from figcite.provenance import Record, now_stamps

FILED_SHA = "a" * 64


@pytest.fixture
def both_kinds(tmp_path, monkeypatch):
    """One staged capture and one filed capture, present at the same time.

    This is the whole point of the fixture: every existing test builds one or
    the other.
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    png = staging / "clip-staged-one.png"
    Image.new("RGB", (12, 9), (20, 60, 120)).save(png)
    (staging / "clip-staged-one.pending.json").write_text(
        json.dumps(
            {
                "png": str(png),
                "capture": {"process": "firefox", "title": "A staged window"},
                "inference": {"doi": None, "doi_evidence": "nothing matched"},
            }
        )
    )
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))

    u, loc = now_stamps()
    store.put(
        Record(
            sha256=FILED_SHA,
            dhash="0" * 16,
            source_kind="clipboard",
            confirmed=False,
            captured_utc=u,
            captured_local=loc,
            source_detail={
                "clipboard_capture": {
                    "process": "powerpnt",
                    "title": "A filed window",
                    "width": 40,
                    "height": 30,
                },
                "doi_evidence": "nothing matched either",
            },
        )
    )
    return {
        "staged_ref": "staged:clip-staged-one.png",
        "filed_ref": f"filed:{FILED_SHA}",
    }


def test_the_two_kinds_are_both_present(both_kinds):
    """Positive control for every test below.

    If the fixture only ever produced one kind, each split assertion would pass
    on a predicate that cannot be wrong -- exactly the hole being closed.
    """
    kinds = sorted(i.kind for i in service.pending_items())
    assert kinds == ["filed", "staged"], kinds


def test_a_filed_capture_is_not_also_listed_as_staged(both_kinds, capsys):
    """`kind == "staged"` widened to `<=` admits "filed", so the same capture
    is printed in both sections."""
    assert cli.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert out.count(FILED_SHA[:12]) == 0, (
        "the filed capture's sha appears in the staged section, which renders "
        f"`ref.split(':')[1]` -- it was counted as staged:\n{out}"
    )
    assert "A staged window" in out, "the staged capture vanished from the listing"


def test_a_staged_capture_is_not_also_counted_as_filed(both_kinds, capsys):
    """`kind == "filed"` widened to `>=` admits "staged", so the headline count
    counts the staged capture too."""
    assert cli.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert "1 filed capture(s)" in out, f"exactly one capture is filed; the count disagrees:\n{out}"


def test_confirm_m0_reaches_the_filed_capture_and_0_reaches_the_staged_one(both_kinds, monkeypatch):
    """The teeth. `m0` indexes the filed pool and `0` the staged pool; if
    either predicate widens, a human's DOI lands on the wrong image."""
    seen: list[str] = []
    monkeypatch.setattr(service, "confirm", lambda ref, **kw: (seen.append(ref), None)[1])

    cli.main(["confirm", "m0", "--doi", "10.1/filed"])
    cli.main(["confirm", "0", "--doi", "10.1/staged"])

    assert seen == [both_kinds["filed_ref"], both_kinds["staged_ref"]], (
        f"confirm resolved the wrong captures: {seen}"
    )


def test_the_two_label_series_do_not_collide(both_kinds):
    """`pending_items` numbers filed captures m0, m1... and staged ones 0, 1...
    A widened predicate puts one capture in both series."""
    items = service.pending_items()
    refs = {i.ref: i.cli_ref for i in items}
    assert refs[both_kinds["filed_ref"]] == "m0", refs
    assert refs[both_kinds["staged_ref"]] == "0", refs
    labels = [i.cli_ref for i in items]
    assert len(labels) == len(set(labels)), f"two captures share a label: {labels}"


def test_confirm_m1_fails_when_only_one_capture_is_filed(both_kinds, monkeypatch, capsys):
    """The case the test above could not see.

    With one capture of each kind, `m0` names the filed one whether or not the
    filed pool has wrongly absorbed the staged capture -- the filed capture is
    first either way, so `pool[0]` is the same object and the assertion passes
    on a broken predicate. Measured: the `kind == "filed" -> >=` mutant in
    `cmd_confirm` SURVIVED the first version of this file.

    The index PAST the end is what distinguishes them. One capture is filed, so
    `m1` must be refused; a widened pool answers it with the staged capture and
    writes the human's DOI into the wrong image.
    """
    seen: list[str] = []
    monkeypatch.setattr(service, "confirm", lambda ref, **kw: (seen.append(ref), None)[1])

    rc = cli.main(["confirm", "m1", "--doi", "10.1/nope"])

    assert rc == 2, f"`confirm m1` was accepted with only one filed capture (rc={rc})"
    assert seen == [], f"it confirmed something anyway: {seen}"
    assert "have 1" in capsys.readouterr().err
