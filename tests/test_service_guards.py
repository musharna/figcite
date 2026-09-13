"""Four guards in `service`, the layer both front ends go through.

`service.py` is where the CLI and the web UI meet, so a wrong answer here is
wrong in both at once. The sweep left seven survivors in it and they are not
evenly interesting: one decides what appears on the pending list, one decides
whether a just-filed image gets DELETED, one finds a library file by hash, and
one picks which corpus row a match's caption comes from.
"""

from __future__ import annotations

import json

import pytest
from PIL import Image

from figcite import corpus, match, service, session_tabs, store
from figcite.provenance import Record, sidecar_path


@pytest.fixture(autouse=True)
def _own_store(tmp_path, monkeypatch):
    """A manifest and library per test.

    conftest gives the whole session one FIGCITE_HOME, so records filed by
    other tests are visible to `store.all_records()` and a pending-list
    assertion counts theirs as well as this file's.
    """
    monkeypatch.setattr(store, "MANIFEST", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "LIBRARY", tmp_path / "library")


def _rec(sha, *, confirmed, source_kind):
    return Record(
        sha256=sha,
        dhash="0" * 16,
        source_kind=source_kind,
        confirmed=confirmed,
        citation="A citation",
        short_cite="Someone 2020",
    )


# ------------------------------------------- what belongs on the pending list


def test_only_unconfirmed_clipboard_captures_are_pending(tmp_path):
    """`if rec.confirmed or rec.source_kind != "clipboard": continue` survived
    `Or -> And`.

    The pending list is the queue of things still needing a human decision.
    Under `and` a record is skipped only when it is confirmed AND not a
    clipboard capture, so BOTH of the other combinations join the queue:
    every already-confirmed capture comes back asking to be confirmed again,
    and every deliberately-filed PDF crop appears as though it were an
    unresolved snip.

    One record per combination, because a fixture carrying only the pending
    case cannot tell an over-inclusive filter from a correct one.
    """
    store.put(_rec("a" * 64, confirmed=False, source_kind="clipboard"))
    store.put(_rec("b" * 64, confirmed=True, source_kind="clipboard"))
    store.put(_rec("c" * 64, confirmed=False, source_kind="pdf-crop"))
    store.put(_rec("d" * 64, confirmed=True, source_kind="pdf-crop"))

    refs = {item.ref for item in service.pending_items()}

    assert refs == {f"filed:{'a' * 64}"}, (
        f"the pending queue is not exactly the unconfirmed clipboard captures: {refs}"
    )


def test_a_confirmed_capture_does_not_come_back_to_the_queue(tmp_path):
    """The single-operand case stated on its own, so a regression names
    itself instead of showing up as a set-comparison diff."""
    store.put(_rec("b" * 64, confirmed=True, source_kind="clipboard"))

    assert service.pending_items() == []


# ------------------------------------ deleting the staging copy, and only it


def test_clearing_staging_never_deletes_the_file_it_just_filed(tmp_path):
    """`if png.exists() and png != dest` survived `And -> Or`.

    This runs at the end of a confirm, once the image has been filed. When
    the staged path IS the destination -- filing in place -- there is nothing
    to clean up. Under `or` the first operand alone is enough, so the freshly
    filed image is unlinked: the confirm reports success, the manifest gains a
    record, and the file the record points at is gone.
    """
    png = tmp_path / "clip-1.png"
    Image.new("RGB", (8, 8), "white").save(png)

    service._clear_staged(png, png)

    assert png.exists(), (
        "the destination file was deleted by the staging cleanup that ran after it was filed"
    )


def test_clearing_staging_does_delete_a_separate_staged_copy(tmp_path):
    """Positive control: "never delete anything" passes the test above and
    leaves every snip sitting in staging forever."""
    png = tmp_path / "clip-1.png"
    dest = tmp_path / "library" / "filed.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), "white").save(png)
    Image.new("RGB", (8, 8), "white").save(dest)

    service._clear_staged(png, dest)

    assert not png.exists(), "the staged copy was left behind"
    assert dest.exists(), "the filed copy was removed instead"


def test_clearing_staging_tolerates_an_already_gone_capture(tmp_path):
    """The other asymmetric half of the same `and`: the file is absent and
    the paths differ. Under `or` the second operand alone fires `unlink()` on
    a path that is not there, and a confirm that has already succeeded dies
    with FileNotFoundError during cleanup."""
    png = tmp_path / "never-existed.png"
    dest = tmp_path / "elsewhere.png"

    service._clear_staged(png, dest)  # must not raise


# ------------------------------------------- finding a library file by hash


def _file_with_sidecar(library, name, sha):
    library.mkdir(parents=True, exist_ok=True)
    img = library / name
    Image.new("RGB", (8, 8), "white").save(img)
    rec = Record(sha256=sha, dhash="0" * 16, source_kind="clipboard", confirmed=True)
    data = json.loads(rec.to_json())
    data["sha256"] = sha
    open(sidecar_path(img), "w", encoding="utf-8").write(json.dumps(data))
    return img


def test_a_library_file_is_found_by_the_sha_its_sidecar_records(tmp_path):
    """`if data.get("sha256") == sha` survived mutating the key name.

    The sidecar's recorded hash is the ONLY pointer from a manifest record
    back to its file -- the docstring above it explains that the filename is
    derived from the citation and cannot be rebuilt from the hash. Ask the
    sidecar for any other key and `.get` returns None, nothing ever equals
    `sha`, and every lookup raises LibraryFileMissing: the manifest is intact,
    the file is on disk, and the tool reports it as gone.
    """
    sha = "e" * 64
    img = _file_with_sidecar(store.LIBRARY, "paper--fig1.png", sha)

    assert service._library_path_for(sha) == img


def test_a_sha_no_sidecar_carries_is_reported_missing(tmp_path):
    """Positive control: "return the first file you find" passes the test
    above and hands back somebody else's figure."""
    _file_with_sidecar(store.LIBRARY, "paper--fig1.png", "e" * 64)

    with pytest.raises(service.LibraryFileMissing):
        service._library_path_for("f" * 64)


# --------------------------------- which corpus row a match's caption is from


class _Row:
    def __init__(self, pmcid, label, caption):
        self.pmcid, self.label, self.caption = pmcid, label, caption
        self.doi, self.dhash, self.image_path = "10.1/x", "0" * 16, "i.png"


def _whereis_with_rows(monkeypatch, tmp_path, rows, verdict):
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(corpus, "all_rows", lambda conn: rows)
    monkeypatch.setattr(match, "by_dhash", lambda b, r: verdict)
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])
    q = tmp_path / "q.png"
    Image.new("RGB", (40, 30), "blue").save(q)
    return service.whereis(str(q))


def test_the_caption_comes_from_the_row_matching_BOTH_container_and_label(tmp_path, monkeypatch):
    """`r.pmcid == verdict.pmcid and r.label == verdict.label` survived four
    mutants: `And -> Or`, `Eq -> NotEq` on either side, and mutating the
    "pmcid" attribute name.

    A figure is identified by container AND label; neither alone is unique --
    one paper has many figures, and every paper has a "Figure 1". The decoys
    here are built so each operand is wrong on its own: one row shares the
    label with a different paper, one shares the paper with a different
    label. Under `or` the first decoy wins and the card shows the caption of
    an unrelated figure, under the wrong attribute name no row matches at all
    and the caption silently degrades to the bare label.

    The displayed caption is what a reader uses to check the tool got it
    right, so a caption belonging to a different figure is worse than none.
    """
    rows = [
        _Row("PMC-OTHER", "Figure 3", "WRONG: same label, different paper"),
        _Row("PMC9", "Figure 1", "WRONG: same paper, different figure"),
        _Row("PMC9", "Figure 3", "RIGHT: pollen viability across crosses"),
    ]
    verdict = match.Match("10.1/found", "PMC9", "Figure 3", "dhash", 0.0, 9.0)

    out = _whereis_with_rows(monkeypatch, tmp_path, rows, verdict)

    assert out["verdict"] == "match"
    assert out["matches"][0]["title"] == "RIGHT: pollen viability across crosses", (
        f"the caption came from the wrong corpus row: {out['matches'][0]['title']!r}"
    )


def test_a_match_with_no_corpus_row_falls_back_to_the_label(tmp_path, monkeypatch):
    """Positive control, and the `else` of the same expression. With no row to
    read a caption from, the label is what is shown -- not an empty string,
    and not a caption borrowed from whichever row sorted first."""
    rows = [_Row("PMC-OTHER", "Figure 1", "some other figure")]
    verdict = match.Match("10.1/found", "PMC9", "Figure 3", "dhash", 0.0, 9.0)

    out = _whereis_with_rows(monkeypatch, tmp_path, rows, verdict)

    assert out["matches"][0]["title"] == "Figure 3", out["matches"][0]
