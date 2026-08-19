"""Thumbnails for records that were never copied into the library.

`figcite register` and the matplotlib hook record provenance for an image
WITHOUT modifying or moving it -- that is the documented point of `register`.
Those records have no library copy, and the thumbnail resolver looked only
there, so it reported them as gone (410) while the file sat untouched at the
path the record itself stores.

Measured on a real manifest: 19 of 23 records 410'd, and 15/15 of the
`generated` ones had an `original_file` that still existed. The deck screen --
the one whose entire job is deciding by looking -- rendered "(image
unavailable)" on every row for the figures the user made themselves.
"""

import pytest
from PIL import Image

from figcite import service, store
from figcite.provenance import Record, now_stamps


def _png(path, size=(40, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (10, 120, 200)).save(path)
    return path


def _record(sha, detail):
    u, loc = now_stamps()
    store.put(
        Record(
            sha256=sha,
            dhash="0" * 16,
            source_kind="generated",
            confirmed=True,
            captured_utc=u,
            captured_local=loc,
            source_detail=detail,
        )
    )
    return f"filed:{sha}"


def test_a_registered_image_thumbnails_from_where_it_actually_lives(tmp_path):
    src = _png(tmp_path / "figures" / "fig9.png")
    ref = _record("aa" * 32, {"original_file": str(src)})

    blob, mime = service.thumbnail(ref)

    assert blob, "no bytes returned for an image that exists on disk"
    assert mime.startswith("image/")


def test_a_record_with_nothing_on_disk_still_reports_it_missing(tmp_path):
    """Positive control: the fallback must not turn every miss into a success."""
    ref = _record("bb" * 32, {"original_file": str(tmp_path / "gone.png")})
    with pytest.raises(service.LibraryFileMissing):
        service.thumbnail(ref)


def test_a_record_with_no_path_at_all_reports_it_missing():
    ref = _record("cc" * 32, {})
    with pytest.raises(service.LibraryFileMissing):
        service.thumbnail(ref)


def test_the_library_copy_wins_when_there_is_one(tmp_path, monkeypatch):
    """The library copy is the canonical filed bytes.

    A record can have both -- `register --copy`-style flows, or an original
    that was later edited. The filed copy is what the citation was attached to,
    so it must not be shadowed by whatever is at the original path now.
    """
    lib = _png(tmp_path / "lib" / "filed.png", size=(80, 60))
    orig = _png(tmp_path / "figures" / "original.png", size=(20, 20))
    ref = _record("dd" * 32, {"original_file": str(orig)})
    monkeypatch.setattr(service, "_library_path_for", lambda sha: lib)

    blob, _ = service.thumbnail(ref)

    # The two differ in aspect ratio, so the returned bytes identify which was
    # read without depending on byte-for-byte encoding.
    import io

    w, h = Image.open(io.BytesIO(blob)).size
    assert w > h, "thumbnail came from the original, not the filed library copy"


@pytest.mark.live
def test_the_real_manifest_resolves_every_record_whose_file_exists():
    """The measurement that started this: no record should 410 while its bytes
    are sitting on disk."""
    import pathlib

    broken = []
    for rec in store.all_records().values():
        detail = rec.source_detail or {}
        orig = detail.get("original_file")
        if not (orig and pathlib.Path(orig).exists()):
            continue
        try:
            service.thumbnail(f"filed:{rec.sha256}")
        except service.LibraryFileMissing:
            broken.append(rec.sha256[:12])
    assert not broken, (
        f"{len(broken)} record(s) 410 while their file exists: {broken[:5]}"
    )
