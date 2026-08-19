import pytest
from figcite import service, store
from figcite.provenance import Record


def test_an_unknown_ref_is_refused_rather_than_read_from_disk():
    with pytest.raises(KeyError):
        service.thumbnail("staged:../../../../etc/passwd")


def test_a_real_staged_capture_renders(tmp_path, monkeypatch):
    """Positive control in the same file: the refusal above must not be
    passing because thumbnail() is simply broken for everything."""
    from PIL import Image

    staging = tmp_path / "staging"
    staging.mkdir()
    Image.new("RGB", (1200, 900), "white").save(staging / "clip-1.png")
    (staging / "clip-1.pending.json").write_text(
        '{"png": "%s", "capture": {}, "inference": {}}' % (staging / "clip-1.png")
    )
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))

    blob, mime = service.thumbnail("staged:clip-1.png")
    assert mime == "image/png"
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    assert max(Image.open(__import__("io").BytesIO(blob)).size) <= 480


def test_a_sha_ref_for_a_record_with_no_library_file_is_refused_cleanly():
    """The manifest can name a record whose bytes never made it into (or
    were removed from) the library. thumbnail() must not let that surface
    as a bare FileNotFoundError out of Image.open() -- it should be a
    clearly-typed, catchable failure instead."""
    sha = "d" * 64
    store.put(
        Record(
            sha256=sha,
            dhash="0" * 16,
            source_kind="clipboard",
            confirmed=True,
        )
    )
    with pytest.raises(service.LibraryFileMissing):
        service.thumbnail(f"sha:{sha}")
