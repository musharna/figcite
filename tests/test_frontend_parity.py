"""Success criterion 2: the CLI and the service layer must confirm identically.

The whole reason `figcite/service.py` exists is that two front ends
disagreeing about what a citation gets attached to would silently break the
one guarantee this tool sells: it never states a citation it cannot support.
This test drives BOTH front doors -- `cli.main(["confirm", ...])` and a
direct `service.confirm(...)` call, the same call the web UI's route makes --
over the *same* source image bytes and asserts the resulting `Record`s agree
field-for-field, except for the handful of fields that cannot possibly match.

Why only `captured_utc`/`captured_local`/`sha256` are volatile, not `dhash`:
the two confirms happen at different wall-clock moments, so their timestamps
differ by construction. `embed()` (figcite/provenance.py) stamps
`rec.to_json()` -- which includes those timestamps -- into the output PNG's
tEXt chunk before hashing the OUTPUT file, so `sha256` inherits the
timestamps' volatility even when the two source images are byte-identical.
`dhash`, by contrast, is computed from decoded pixel data only
(`dhash_bytes` re-opens the image and hashes a resized grayscale copy), which
metadata chunks do not touch -- so with byte-identical source pixels, dhash
is expected to match, and is asserted to match here rather than excluded.
"""

import json

from PIL import Image

from figcite import cli, service, store

VOLATILE = {"captured_utc", "captured_local", "sha256"}

CITE = "Band et al. 2014"


def _same_source_png() -> bytes:
    """One canonical PNG, so both staged fixtures start from identical bytes.

    A non-flat pattern (not solid white) so `dhash` is actually exercising the
    perceptual hash rather than trivially matching two blank images.
    """
    im = Image.new("RGB", (40, 40))
    px = im.load()
    for y in range(40):
        for x in range(40):
            px[x, y] = (x * 6 % 256, y * 6 % 256, (x + y) * 3 % 256)
    import io

    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _stage(tmp_path, monkeypatch, name, png_bytes):
    staging = tmp_path / name
    staging.mkdir()
    png = staging / "clip-1.png"
    png.write_bytes(png_bytes)
    (staging / "clip-1.pending.json").write_text(
        json.dumps(
            {
                "png": str(png),
                "capture": {"process": "firefox", "title": "A paper"},
                "inference": {"kind": "browser", "candidates": [], "doi_evidence": ""},
            }
        )
    )
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))
    return png


def test_both_front_doors_produce_the_same_record(tmp_path, monkeypatch):
    source_bytes = _same_source_png()

    # --- front door 1: the CLI ---------------------------------------------
    png_a = _stage(tmp_path, monkeypatch, "a", source_bytes)
    # Sanity, taken before `confirm` moves/deletes the staged file: the
    # fixture really did start from the canonical pixel bytes. If this ever
    # fails, dropping `sha256`/`dhash` from VOLATILE below would be testing
    # nothing.
    assert png_a.read_bytes() == source_bytes
    before = set(store.all_records().keys())
    assert cli.main(["confirm", "0", "--cite", CITE]) == 0
    after = set(store.all_records().keys())
    new_keys = after - before
    assert len(new_keys) == 1, (
        "expected exactly one new manifest record from the CLI confirm, "
        f"got {new_keys!r}"
    )
    via_cli = store.all_records()[new_keys.pop()]

    # --- front door 2: the service layer, the web UI's route ---------------
    png_b = _stage(tmp_path, monkeypatch, "b", source_bytes)
    assert png_b.read_bytes() == source_bytes
    result = service.confirm("staged:clip-1.png", cite=CITE)
    via_service = result.record

    # Positive control: a parity test that passes because both records are
    # empty proves nothing. Confirm the citation actually landed.
    assert via_cli.citation == CITE or via_cli.short_cite == CITE[:40]
    assert via_cli.confirmed is True
    assert via_cli.source_kind == "clipboard"

    a = {k: v for k, v in vars(via_cli).items() if k not in VOLATILE}
    b = {k: v for k, v in vars(via_service).items() if k not in VOLATILE}
    assert a == b

    # dhash is pixel-derived, not metadata-derived, so byte-identical source
    # images should still hash identically after each front door's `embed()`
    # writes its own (different) timestamp into the PNG's tEXt chunk.
    assert via_cli.dhash == via_service.dhash
    assert via_cli.dhash != ""
