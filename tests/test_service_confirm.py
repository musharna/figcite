import json
import pytest

from PIL import Image

from figcite import service
from figcite.provenance import Record, now_stamps


def _stage(tmp_path, monkeypatch, inference):
    staging = tmp_path / "staging"
    staging.mkdir()
    png = staging / "clip-1.png"
    # A real, decodable PNG: confirm() runs the actual finalize()/embed() path,
    # which opens this file with Pillow. The brief's literal 8-byte PNG
    # signature (b"\x89PNG\r\n\x1a\n") is not a decodable image and made
    # Image.open() raise UnidentifiedImageError inside the two tests that
    # reach finalize() -- a collateral failure unrelated to what those tests
    # check. Mirrors the fixture pattern already used in
    # tests/test_capture_trail.py.
    Image.new("RGB", (4, 4), (30, 90, 140)).save(png)
    (staging / "clip-1.pending.json").write_text(
        json.dumps(
            {
                "png": str(png),
                "capture": {"process": "firefox", "title": "A paper"},
                "inference": inference,
            }
        )
    )
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
    rec = service.confirm("staged:clip-1.png", doi="10.1/guess")
    assert rec.confirmed is True
    assert rec.doi == "10.1/guess"


def _fake_record_for(
    doi, cite, url, *, confirmed, kind, detail, adapted_from=None, note=""
):
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
    _stage(
        tmp_path, monkeypatch, {"doi": "10.1/real", "grounded": True, "candidates": []}
    )
    monkeypatch.setattr(service, "record_for", _fake_record_for)
    rec = service.confirm("staged:clip-1.png")
    assert rec.doi == "10.1/real"


def test_skip_defers_and_does_not_delete(tmp_path, monkeypatch):
    png = _stage(tmp_path, monkeypatch, {"candidates": []})
    service.skip("staged:clip-1.png")
    assert png.exists(), "skip must not touch disk"
    assert service.pending_items()[-1].ref == "staged:clip-1.png", (
        "deferred to the back"
    )
