"""Every capture leaves a record, even when no citation can be established.

"At least some track" -- a screenshot with no resolvable DOI still knows which
app was in front, what the window said, and when.
"""
import json

from PIL import Image

from figcite import clipboard as C
from figcite import store


def _staged(tmp_path, capture):
    png = tmp_path / "clip-test.png"
    Image.new("RGB", (200, 140), (30, 90, 140)).save(png)
    (tmp_path / "clip-test.capture.json").write_text(json.dumps(capture))
    return png


def test_ungrounded_capture_is_filed_with_its_context(tmp_path):
    capture = {"title": "Some Random Page - Mozilla Firefox", "process": "firefox",
               "captured_local": "2026-08-13T23:59:00-04:00", "width": 200, "height": 140}
    png = _staged(tmp_path, capture)
    pending = {"png": str(png), "capture": capture,
               "inference": {"kind": "clipboard-from-web", "doi": None, "grounded": False,
                             "url": "https://example.org/some-page",
                             "doi_evidence": "no history entry matched"}}

    dest = C.auto_finalize(png, pending)
    assert dest is not None and dest.exists(), "an ungrounded capture was not filed at all"

    rec = store.get(__import__("figcite").provenance.sha256_file(dest))
    assert rec is not None, "nothing reached the manifest"
    assert rec.confirmed is False, "an unresolved capture must not claim confirmation"
    assert not rec.doi and not rec.citation, "no citation should be invented"

    ctx = rec.context_line()
    assert "firefox" in ctx
    assert "Some Random Page" in ctx
    assert "example.org" in ctx
    assert "2026-08-13 23:59" in ctx
    assert "no history entry matched" in rec.note, "the reason for the gap was dropped"


def test_grounded_capture_is_still_confirmed(monkeypatch, tmp_path):
    """Positive control: filing everything must not stop grounded ones confirming."""
    from figcite.provenance import Record

    capture = {"title": "A Paper - Mozilla Firefox", "process": "firefox",
               "captured_local": "2026-08-13T23:59:00-04:00"}
    png = _staged(tmp_path, capture)
    pending = {"png": str(png), "capture": capture,
               "inference": {"kind": "clipboard-from-web", "doi": "10.1111/nph.71477",
                             "grounded": True, "url": "https://doi.org/10.1111/nph.71477",
                             "doi_evidence": "exact-title in Firefox history"}}

    import figcite.crossref as X

    def fake_record_from_doi(doi, *, confirmed=True, source_kind="manual",
                             source_detail=None):
        return Record(doi=doi, citation="Someone et al. (2026).",
                      short_cite="Someone et al. 2026", confirmed=confirmed,
                      source_kind=source_kind, source_detail=source_detail or {})

    monkeypatch.setattr(X, "record_from_doi", fake_record_from_doi)

    dest = C.auto_finalize(png, pending)
    rec = store.get(__import__("figcite").provenance.sha256_file(dest))
    assert rec.confirmed is True
    assert rec.doi == "10.1111/nph.71477"
# IRON_LAW_OK


# ------------------------------------------------- found by a REAL capture, 2026-08-16


def test_watcher_script_is_a_singleton_per_staging_dir():
    """Two watchers on one staging dir destroy captures.

    Filenames are clip-<second>-<md5[0..7]>, so two watchers seeing the SAME
    clipboard image in the same second compute the SAME path and race to write
    the sidecar. Measured: three watchers had accumulated, the collision failed
    the write with "Stream was not readable", and the capture was lost.

    The lock lives in the PowerShell script, not the Python launcher, because
    the launcher could only ever check whether a *supervisor* was running --
    which is not the thing being duplicated.
    """
    src = C.PS1.read_text(encoding="utf-8", errors="replace")
    assert "System.Threading.Mutex" in src, "no singleton lock"
    assert "WATCH_ALREADY_RUNNING" in src, "a refused start must be announced"
    assert "$StagingDir.ToLower()" in src, "lock must be keyed per staging dir"


def test_capture_is_announced_only_after_both_files_exist():
    """CAPTURED used to be emitted even when the sidecar write had failed, so
    the consumer was told about a capture whose context did not exist."""
    src = C.PS1.read_text(encoding="utf-8", errors="replace")
    assert "CAPTURE_FAILED" in src, "a failed sidecar write must be reported"
    assert src.index("$ok = $true") < src.index('if ($ok) { Write-Output ("CAPTURED '), (
        "the announcement must come after the write attempt"
    )


def test_malformed_capture_announcement_is_ignored(monkeypatch, tmp_path, capsys):
    """PowerShell writes errors to the same stream as CAPTURED, so an error
    raised mid-capture splices into the line and yields a path like
    'clip-....pngSet-Content : Stream was not readable.' -- which then failed
    far downstream as a confusing FileNotFoundError. Measured on a real capture.
    """
    import subprocess

    real = tmp_path / "clip-20260101-000000-AAAABBBB.png"
    Image.new("RGB", (40, 30), (10, 10, 10)).save(real)

    lines = [
        "WATCH_START x",
        f"CAPTURED {real}Set-Content : Stream was not readable.",  # corrupted
        f"CAPTURED {tmp_path / 'does-not-exist.png'}",             # announced, absent
        f"CAPTURED {real}",                                        # the good one
    ]

    class FakeProc:
        stdout = iter(lines)

        def terminate(self):
            pass

    monkeypatch.setattr(C, "win_to_wsl", lambda p: str(p))
    monkeypatch.setattr(C, "wsl_to_win", lambda p: str(p))
    monkeypatch.setattr(C, "staging_dirs", lambda: (str(tmp_path), tmp_path))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())

    seen = []
    monkeypatch.setattr(C, "enrich", lambda p: (seen.append(str(p)), {})[1])
    monkeypatch.setattr(C, "auto_finalize", lambda p, pending: None)

    C.watch(max_hours=0.001, resolve=True, auto_confirm=True)

    assert seen == [str(real)], (
        f"expected only the one real capture to be processed, got {seen}"
    )
    assert "malformed capture announcement" in capsys.readouterr().out
