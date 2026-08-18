"""Success criterion 2: the CLI and the service layer must confirm identically.

The whole reason `figcite/service.py` exists is that two front ends
disagreeing about what a citation gets attached to would silently break the
one guarantee this tool sells: it never states a citation it cannot support.
These tests drive two front doors -- `cli.main(["confirm", ...])` and a
direct `service.confirm(...)` call, the same call the web UI's route makes --
over the *same* source image bytes and assert the resulting `Record`s agree
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

Round-2 finding: comparing `via_cli`/`via_service` with only `cite=` set is
mostly vacuous. `record_for`'s manual branch (figcite/_actions.py) populates
`citation`, `short_cite`, `source_kind`, `source_detail`, `confirmed`, and
the timestamps -- everything else (`doi`, `authors`, `year`, `title`,
`container`, `license_url`, `reuse`, `retracted`, `adapted_from`, `note`,
`url`) sits at its dataclass default on BOTH sides, so comparing them proved
nothing. A reviewer confirmed this by mutating `cli.py`'s
`adapted_from=a.adapted_from` to `adapted_from=None` -- a real dropped-flag
bug -- and watching the (then-unextended) test still pass. Three things
close that:

  1. `test_both_front_doors_produce_the_same_record` now also drives
     `--note`/`--adapted-from`, so those two fields carry a real,
     distinguishing value instead of "" / None on both sides.
  2. `test_pick_selector_front_doors_agree` exercises the `pick=` selector,
     which -- unlike `cite=` -- resolves through `record_from_doi` and
     populates `doi`, `authors`, `year`, `title`, `container`,
     `license_url`, `reuse`, `retracted`, `url`. `record_from_doi` is
     monkeypatched to a deterministic stub so this needs no network.
  3. `test_own_work_selector_web_route_and_service_agree` exercises
     `own_work=True`. This selector has no `figcite confirm` flag at all
     (see `cli.py`'s `_in_cli_words`, and the comment on the `confirm`
     subparser build-out) -- the web UI's "This is my own work" button is
     its only front door, and driving it over a real HTTP socket would
     require `@pytest.mark.live`. Instead this test calls the exact
     wire-to-service translation `do_POST` runs for `/api/confirm`
     (`web._fields(payload, web.CONFIRM_FIELDS)` then
     `service.confirm(ref, **fields)`) directly against a JSON-shaped
     payload -- real `web.py` code, no socket -- and compares it to a
     direct `service.confirm(ref, own_work=True)` call.

`doi=` (the fourth selector) is not covered here: it requires a live
CrossRef lookup and this file carries no `@pytest.mark.live` test by design.
"""

import io
import json

from PIL import Image

from figcite import _actions, cli, service, store, web
from figcite.provenance import Record, now_stamps

VOLATILE = {"captured_utc", "captured_local", "sha256"}

CITE = "Band et al. 2014"
NOTE = "reproduced with permission -- see lab notebook p. 42"
ADAPTED_FROM = "10.1234/upstream-figure"
PICK_DOI = "10.9999/testcand"


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
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _stage(tmp_path, monkeypatch, name, png_bytes, candidates=None):
    staging = tmp_path / name
    staging.mkdir()
    png = staging / "clip-1.png"
    png.write_bytes(png_bytes)
    (staging / "clip-1.pending.json").write_text(
        json.dumps(
            {
                "png": str(png),
                "capture": {"process": "firefox", "title": "A paper"},
                "inference": {
                    "kind": "browser",
                    "candidates": candidates or [],
                    "doi_evidence": "",
                },
            }
        )
    )
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))
    return png


def _confirm_via_cli(argv):
    """Run a CLI confirm and return the ONE new manifest record it produced.

    A diff of manifest keys taken immediately around the call, not
    `max(..., key=captured_utc)` -- the manifest is session-global, so
    picking "the newest" can collide with a record another test file left
    behind.
    """
    before = set(store.all_records().keys())
    assert cli.main(argv) == 0
    new_keys = set(store.all_records().keys()) - before
    assert len(new_keys) == 1, (
        f"expected exactly one new manifest record from {argv!r}, "
        f"got {new_keys!r}"
    )
    return store.all_records()[new_keys.pop()]


def _assert_parity(via_a, via_b):
    a = {k: v for k, v in vars(via_a).items() if k not in VOLATILE}
    b = {k: v for k, v in vars(via_b).items() if k not in VOLATILE}
    assert a == b
    # dhash is pixel-derived, not metadata-derived, so byte-identical source
    # images should still hash identically after each front door's `embed()`
    # writes its own (different) timestamp into the PNG's tEXt chunk.
    assert via_a.dhash == via_b.dhash
    assert via_a.dhash != ""


def test_both_front_doors_produce_the_same_record(tmp_path, monkeypatch):
    source_bytes = _same_source_png()

    # --- front door 1: the CLI ---------------------------------------------
    png_a = _stage(tmp_path, monkeypatch, "a", source_bytes)
    # Sanity, taken before `confirm` moves/deletes the staged file: the
    # fixture really did start from the canonical pixel bytes. If this ever
    # fails, dropping `sha256`/`dhash` from VOLATILE above would be testing
    # nothing.
    assert png_a.read_bytes() == source_bytes
    via_cli = _confirm_via_cli(
        [
            "confirm",
            "0",
            "--cite",
            CITE,
            "--note",
            NOTE,
            "--adapted-from",
            ADAPTED_FROM,
        ]
    )

    # --- front door 2: the service layer, the web UI's route ---------------
    png_b = _stage(tmp_path, monkeypatch, "b", source_bytes)
    assert png_b.read_bytes() == source_bytes
    via_service = service.confirm(
        "staged:clip-1.png", cite=CITE, note=NOTE, adapted_from=ADAPTED_FROM
    ).record

    # Positive control: a parity test that passes because both records are
    # empty proves nothing. Confirm the citation, note, and adapted_from
    # actually landed -- these are exactly the fields a dropped-kwarg wiring
    # bug (e.g. `adapted_from=a.adapted_from` -> `adapted_from=None`) would
    # silently zero out on one side only.
    assert via_cli.citation == CITE or via_cli.short_cite == CITE[:40]
    assert via_cli.confirmed is True
    assert via_cli.source_kind == "clipboard"
    assert via_cli.note == NOTE
    assert via_cli.adapted_from == ADAPTED_FROM

    _assert_parity(via_cli, via_service)


def test_pick_selector_front_doors_agree(tmp_path, monkeypatch):
    """`pick=` resolves through `record_from_doi`, unlike `cite=` -- so this
    exercises `doi`/`authors`/`year`/`title`/`container`/`license_url`/
    `reuse`/`retracted`/`url` actually carrying real, non-default content on
    both sides. `record_from_doi` is stubbed so this needs no network.
    """

    def _stub_record_from_doi(doi, *, confirmed, source_kind="manual", source_detail=None):
        u, loc = now_stamps()
        return Record(
            doi=doi,
            url=f"https://doi.org/{doi}",
            citation="Stub, S. (2020). Stub Title. Stub Journal.",
            short_cite="Stub 2020",
            authors=["Stub, S."],
            year=2020,
            title="Stub Title",
            container="Stub Journal",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            reuse="reuse-ok-attribution-required",
            retracted=False,
            source_kind=source_kind,
            source_detail=source_detail or {},
            captured_utc=u,
            captured_local=loc,
            confirmed=confirmed,
        )

    monkeypatch.setattr(_actions, "record_from_doi", _stub_record_from_doi)

    candidates = [
        {
            "doi": PICK_DOI,
            "score": 42,
            "title": "Stub Title",
            "container": "Stub Journal",
            "year": 2020,
            "type": "journal-article",
        }
    ]
    source_bytes = _same_source_png()

    # --- front door 1: the CLI ---------------------------------------------
    _stage(tmp_path, monkeypatch, "a", source_bytes, candidates=candidates)
    via_cli = _confirm_via_cli(["confirm", "0", "--pick", "0"])

    # --- front door 2: the service layer ------------------------------------
    _stage(tmp_path, monkeypatch, "b", source_bytes, candidates=candidates)
    via_service = service.confirm("staged:clip-1.png", pick=0).record

    # Positive control: the stubbed CrossRef fields actually landed, on the
    # fields the plain `cite=` case above cannot exercise at all.
    assert via_cli.doi == PICK_DOI
    assert via_cli.title == "Stub Title"
    assert via_cli.authors == ["Stub, S."]
    assert via_cli.confirmed is True

    _assert_parity(via_cli, via_service)


def test_own_work_selector_web_route_and_service_agree(tmp_path, monkeypatch):
    """`own_work=True` has no `figcite confirm` flag at all (cli.py's
    `_in_cli_words` strips it from confirm()'s own error text for exactly
    this reason) -- the web UI's "This is my own work" button is the only
    front door for it, and driving that over a real socket needs
    `@pytest.mark.live`. So the two doors compared here are `do_POST`'s own
    wire-to-service translation for `/api/confirm` -- `web._fields(payload,
    web.CONFIRM_FIELDS)` then `service.confirm(ref, **fields)`, called
    in-process against a JSON-shaped payload, no socket involved -- versus a
    direct `service.confirm(ref, own_work=True)` call.
    """
    source_bytes = _same_source_png()

    # --- front door 1: the web route's wire-to-service translation --------
    _stage(tmp_path, monkeypatch, "a", source_bytes)
    payload = {"ref": "staged:clip-1.png", "own_work": True}
    fields = web._fields(payload, web.CONFIRM_FIELDS)
    ref = fields.pop("ref")
    via_web = service.confirm(ref, **fields).record

    # --- front door 2: the service layer, called directly -------------------
    _stage(tmp_path, monkeypatch, "b", source_bytes)
    via_service = service.confirm("staged:clip-1.png", own_work=True).record

    # Positive control.
    assert via_web.citation == "This work"
    assert via_web.confirmed is True

    _assert_parity(via_web, via_service)
