import json
from pathlib import Path

import pytest
from PIL import Image

from figcite import service
from figcite.provenance import Record, now_stamps


def _stage(tmp_path, monkeypatch, inference, name="clip-1"):
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    png = staging / f"{name}.png"
    # A real, decodable PNG: confirm() runs the actual finalize()/embed() path,
    # which opens this file with Pillow. The brief's literal 8-byte PNG
    # signature (b"\x89PNG\r\n\x1a\n") is not a decodable image and made
    # Image.open() raise UnidentifiedImageError inside the two tests that
    # reach finalize() -- a collateral failure unrelated to what those tests
    # check. Mirrors the fixture pattern already used in
    # tests/test_capture_trail.py.
    Image.new("RGB", (4, 4), (30, 90, 140)).save(png)
    (staging / f"{name}.pending.json").write_text(
        json.dumps(
            {
                "png": str(png),
                "capture": {"process": "firefox", "title": "A paper"},
                "inference": inference,
            }
        )
    )
    # Safe to call more than once per test (staging two items): re-pointing
    # staging_dirs at the same directory is idempotent.
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))
    return png


def test_a_guessed_doi_is_refused_without_an_explicit_doi(tmp_path, monkeypatch):
    """Merely displaying a guess must never be enough to confirm it."""
    _stage(
        tmp_path,
        monkeypatch,
        {"doi": "10.1/guess", "grounded": False, "candidates": []},
    )
    with pytest.raises(service.NotGrounded):
        service.confirm("staged:clip-1.png")


def test_the_same_guess_is_accepted_when_named_explicitly(tmp_path, monkeypatch):
    """Positive control: the legitimate path still works, so a broken harness
    cannot read as 'safely refused'."""
    _stage(
        tmp_path,
        monkeypatch,
        {"doi": "10.1/guess", "grounded": False, "candidates": []},
    )
    monkeypatch.setattr(service, "record_for", _fake_record_for)
    rec = service.confirm("staged:clip-1.png", doi="10.1/guess").record
    assert rec.confirmed is True
    assert rec.doi == "10.1/guess"


def _fake_record_for(doi, cite, url, *, confirmed, kind, detail, adapted_from=None, note=""):
    u, loc = now_stamps()
    return Record(
        doi=doi,
        confirmed=confirmed,
        source_kind=kind,
        source_detail=detail,
        captured_utc=u,
        captured_local=loc,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"doi": "10.1/a", "own_work": True},  # two selectors
        {"pick": 0, "cite": "Someone 2020"},  # two selectors
    ],
)
def test_confirm_refuses_more_than_one_selector(tmp_path, monkeypatch, kwargs):
    _stage(tmp_path, monkeypatch, {"candidates": [{"doi": "10.1/a"}]})
    with pytest.raises(ValueError):
        service.confirm("staged:clip-1.png", **kwargs)


def test_zero_selectors_means_use_this_item_s_own_grounded_doi(tmp_path, monkeypatch):
    """Controller Ruling 1. Zero selectors is the `figcite confirm 0` case that
    cmd_pending itself prints as the instruction for a grounded capture. It is
    valid, and the grounded check -- not an arity check -- is what guards it."""
    _stage(tmp_path, monkeypatch, {"doi": "10.1/real", "grounded": True, "candidates": []})
    monkeypatch.setattr(service, "record_for", _fake_record_for)
    rec = service.confirm("staged:clip-1.png").record
    assert rec.doi == "10.1/real"


def test_skip_defers_and_does_not_delete(tmp_path, monkeypatch):
    """I2 (review round 1). A single staged item is already last before
    out.sort() runs -- that fixture shape cannot fail if the sort is deleted.
    Two staged items are needed so the skipped one has somewhere to move
    FROM: assert its position relative to the other staged item, not just
    that it ended up last.

    Filtered to kind == "staged": the shared FIGCITE_HOME store can carry
    unconfirmed clipboard records left behind by other test modules in the
    same pytest session (each surfaces as a "filed:..." item), so asserting
    on the raw pending_items() list is coupled to test execution order across
    files. The two staged refs' relative order is the only thing this test is
    actually about.
    """
    png1 = _stage(tmp_path, monkeypatch, {"candidates": []}, name="clip-1")
    png2 = _stage(tmp_path, monkeypatch, {"candidates": []}, name="clip-2")

    def staged_refs():
        return [i.ref for i in service.pending_items() if i.kind == "staged"]

    before = staged_refs()
    assert before == ["staged:clip-1.png", "staged:clip-2.png"]

    service.skip("staged:clip-1.png")
    assert png1.exists() and png2.exists(), "skip must not touch disk"

    after = staged_refs()
    assert after == ["staged:clip-2.png", "staged:clip-1.png"], (
        "the skipped item must move behind the other staged item"
    )


def test_confirm_refuses_to_file_with_nothing_to_confirm_with(tmp_path, monkeypatch):
    """C1 (review round 1, Controller Ruling 4). cmd_confirm has TWO guards, not
    one -- Ruling 1 correctly removed the arity check, but the second guard
    (need --doi, --pick N, or --cite) went missing in the move. Without it,
    zero selectors on an item with NO inferable DOI sets doi=None, skips
    NotGrounded because doi is FALSY rather than grounded, and falls through
    to record_for(None, None, None) -> finalize() -> _clear_staged(): an
    uncited, unconfirmed record gets filed while the pending.json holding the
    candidates/inference-kind/error is deleted. Irreversible.

    The exception alone is not sufficient evidence -- a guard that raised
    AFTER the deletion would still make this assertion pass -- so the
    surviving-file checks are the ones that actually pin the behaviour.
    """
    png = _stage(
        tmp_path,
        monkeypatch,
        {"kind": "browser", "candidates": [], "error": "CrossRef unreachable"},
    )
    pending_json = Path(str(png)[:-4] + ".pending.json")
    assert pending_json.exists(), "fixture sanity check"

    with pytest.raises(ValueError):
        service.confirm("staged:clip-1.png")

    assert png.exists(), "confirm must not delete the capture on a bare refusal"
    assert pending_json.exists(), (
        "confirm must not destroy the candidates/error/kind the pending.json "
        "carries on a bare refusal"
    )
