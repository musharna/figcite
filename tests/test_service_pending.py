import json

from figcite import service, store
from figcite.provenance import Record, now_stamps


def test_a_filed_unconfirmed_capture_becomes_a_pending_item():
    u, loc = now_stamps()
    store.put(
        Record(
            sha256="a" * 64,
            dhash="0" * 16,
            source_kind="clipboard",
            confirmed=False,
            captured_utc=u,
            captured_local=loc,
            source_detail={
                "clipboard_capture": {"process": "firefox", "title": "A paper"}
            },
        )
    )
    items = service.pending_items()
    refs = [i.ref for i in items]
    assert f"filed:{'a' * 64}" in refs
    it = [i for i in items if i.ref == f"filed:{'a' * 64}"][0]
    assert it.kind == "filed"
    assert it.doi is None
    assert it.candidates == []
    assert it.error is None  # "looked, found nothing"


def test_a_lookup_failure_is_not_an_empty_candidate_list(tmp_path, monkeypatch):
    """The distinction the whole fail-loud rule rests on."""
    staging = tmp_path / "staging"
    staging.mkdir()
    png = staging / "clip-1.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    (staging / "clip-1.pending.json").write_text(
        json.dumps(
            {
                "png": str(png),
                "capture": {
                    "process": "firefox",
                    "title": "A paper",
                    "width": 800,
                    "height": 600,
                },
                "inference": {
                    "kind": "browser",
                    "candidates": [],
                    "error": "CrossRef unreachable",
                },
            }
        )
    )
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))

    it = [i for i in service.pending_items() if i.kind == "staged"][0]
    assert it.candidates == []
    assert it.error == "CrossRef unreachable"
    assert it.ref == "staged:clip-1.png"
