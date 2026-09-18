"""Every capture leaves a record, even when no citation can be established.

"At least some track" -- a screenshot with no resolvable DOI still knows which
app was in front, what the window said, and when.
"""

import hashlib
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
    capture = {
        "title": "Some Random Page - Mozilla Firefox",
        "process": "firefox",
        "captured_local": "2026-08-13T23:59:00-04:00",
        "width": 200,
        "height": 140,
    }
    png = _staged(tmp_path, capture)
    pending = {
        "png": str(png),
        "capture": capture,
        "inference": {
            "kind": "clipboard-from-web",
            "doi": None,
            "grounded": False,
            "url": "https://example.org/some-page",
            "doi_evidence": "no history entry matched",
        },
    }

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

    capture = {
        "title": "A Paper - Mozilla Firefox",
        "process": "firefox",
        "captured_local": "2026-08-13T23:59:00-04:00",
    }
    png = _staged(tmp_path, capture)
    pending = {
        "png": str(png),
        "capture": capture,
        "inference": {
            "kind": "clipboard-from-web",
            "doi": "10.1111/nph.71477",
            "grounded": True,
            "url": "https://doi.org/10.1111/nph.71477",
            "doi_evidence": "exact-title in Firefox history",
        },
    }

    import figcite.crossref as X

    def fake_record_from_doi(doi, *, confirmed=True, source_kind="manual", source_detail=None):
        return Record(
            doi=doi,
            citation="Someone et al. (2026).",
            short_cite="Someone et al. 2026",
            confirmed=confirmed,
            source_kind=source_kind,
            source_detail=source_detail or {},
        )

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
    import sys

    real = tmp_path / "clip-20260101-000000-AAAABBBB.png"
    Image.new("RGB", (40, 30), (10, 10, 10)).save(real)

    lines = [
        "WATCH_START x",
        f"CAPTURED {real}Set-Content : Stream was not readable.",  # corrupted
        f"CAPTURED {tmp_path / 'does-not-exist.png'}",  # announced, absent
        f"CAPTURED {real}",  # the good one
    ]

    class FakeProc:
        stdout = iter(lines)

        def terminate(self):
            pass

    monkeypatch.setattr(C, "win_to_wsl", lambda p: str(p))
    monkeypatch.setattr(C, "wsl_to_win", lambda p: str(p))
    monkeypatch.setattr(C, "staging_dirs", lambda: (str(tmp_path), tmp_path))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    # watch() checks that PS_EXE exists before the (faked) Popen; any real file
    # satisfies it off-Windows, as test_clipboard_guards already does
    monkeypatch.setattr(C, "PS_EXE", sys.executable)

    seen = []
    monkeypatch.setattr(C, "enrich", lambda p: (seen.append(str(p)), {})[1])
    monkeypatch.setattr(C, "auto_finalize", lambda p, pending: None)

    C.watch(max_hours=0.001, resolve=True, auto_confirm=True)

    assert seen == [str(real)], f"expected only the one real capture to be processed, got {seen}"
    assert "malformed capture announcement" in capsys.readouterr().out


# ------------------------------------------- the watcher stopped polling, 2026-08-26


def _ps1_param_block() -> str:
    """The `param(...)` header of the watcher script, brackets balanced.

    Extracted rather than grepped because the script now TALKS about polling at
    length in order to explain why it does not poll, so `"PollMs" in src` would
    be satisfied by a comment saying the opposite of what the test wants to
    know. A parameter is a structural thing; ask the structure.
    """
    src = C.PS1.read_text(encoding="utf-8", errors="replace")
    start = src.index("param(")
    depth = 0
    for i in range(start + len("param"), len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError("param( block in watch_clipboard.ps1 is never closed")


def test_the_watcher_has_no_poll_interval_because_it_does_not_poll():
    """It used to ask the clipboard every 800ms whether anything had changed --
    75 times a minute, and every ask OPENS the clipboard so nothing else can.

    A `-PollMs` parameter surviving the rewrite would mean either a poll loop
    came back or the script advertises a knob that controls nothing. Both are
    worth failing on.
    """
    assert "PollMs" not in _ps1_param_block(), (
        "the watcher takes a poll interval again; it is supposed to be woken by "
        "WM_CLIPBOARDUPDATE, not to ask on a timer"
    )


def test_the_watcher_subscribes_and_then_sleeps_on_the_event():
    """The positive half of the test above: absence of a poll knob would also be
    satisfied by a watcher that does nothing at all."""
    src = C.PS1.read_text(encoding="utf-8", errors="replace")
    assert "AddClipboardFormatListener(sink.Handle)" in src, (
        "nothing subscribes to clipboard changes"
    )
    # WaitAny, not WaitOne, since 2026-09-17: the loop also wakes for a HANDBACK
    # command on stdin. It must still block on the change event, not a timer.
    assert "[System.Threading.WaitHandle]::WaitAny(" in src and "@($listener.Changed," in src, (
        "the main loop does not wait on the change event"
    )


def test_an_unreadable_clipboard_is_not_reported_as_an_empty_one():
    """The old loop wrapped the whole read in `catch { }`, so a clipboard that
    would not open reported exactly as a clipboard with nothing on it -- an
    outage folded into an absence, which is the failure this project exists to
    avoid everywhere else. The read has three outcomes now, and the third is
    announced rather than swallowed."""
    src = C.PS1.read_text(encoding="utf-8", errors="replace")
    assert 'state = "Unreadable"' in src, "the read cannot express 'I could not tell'"
    assert 'state = "NoImage"' in src, "the read cannot express 'nothing there'"
    assert "CLIPBOARD_UNREADABLE" in src, "an unreadable clipboard is never announced"


def test_failing_to_subscribe_is_fatal_rather_than_falling_back_to_polling():
    """A polling fallback would restore the cost the subscription exists to
    remove, silently, in the one situation nobody is looking at the logs."""
    src = C.PS1.read_text(encoding="utf-8", errors="replace")
    assert "WATCH_FAILED" in src, "a failed subscription is not announced"
    assert "exit 4" in src, "a failed subscription does not stop the watcher"
    assert src.index("WATCH_FAILED") < src.index("WATCH_START"), (
        "the failure check must come before the watcher claims to be started"
    )


# ------------------------------------------- handback, 2026-09-17


def _run_watch_once(monkeypatch, tmp_path, *, finalized, handback=True, stdout_extra=()):
    import subprocess
    import sys

    real = tmp_path / "clip-20260101-000000-AAAABBBB.png"
    Image.new("RGB", (40, 30), (200, 10, 10)).save(real)

    class FakeStdin:
        def __init__(self):
            self.sent = []

        def write(self, s):
            self.sent.append(s)

        def flush(self):
            pass

        def close(self):
            pass

    class FakeProc:
        stdin = FakeStdin()
        stdout = iter(["WATCH_START x", f"CAPTURED {real}", *stdout_extra])

        def terminate(self):
            pass

    proc = FakeProc()
    monkeypatch.setattr(C, "win_to_wsl", lambda p: str(p))
    monkeypatch.setattr(C, "wsl_to_win", lambda p: "WIN:" + str(p))
    monkeypatch.setattr(C, "staging_dirs", lambda: (str(tmp_path), tmp_path))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(C, "PS_EXE", sys.executable)
    monkeypatch.setattr(C, "enrich", lambda p: {})
    monkeypatch.setattr(C, "auto_finalize", lambda p, pending: finalized)
    expected_md5 = hashlib.md5(real.read_bytes()).hexdigest()
    C.watch(max_hours=0.001, resolve=True, auto_confirm=True, handback=handback)
    return proc.stdin.sent, expected_md5


def test_a_filed_capture_is_handed_back_keyed_to_the_captured_bytes(monkeypatch, tmp_path):
    """The watcher compares the md5 it is sent against the image still on the
    clipboard, so it must be the md5 of the staged bytes -- the same bytes the
    watcher hashed -- and the path must be the Windows view of the FILED copy,
    not the staged one that filing deletes."""
    dest = tmp_path / "library" / "10.1-x--20260101T000000.png"
    sent, md5 = _run_watch_once(monkeypatch, tmp_path, finalized=dest)
    # "-": nothing in the manifest for this file, so no caption
    assert sent == [f"HANDBACK {md5} - WIN:{dest}\n"], sent


def test_no_handback_when_disabled_or_nothing_was_filed(monkeypatch, tmp_path):
    dest = tmp_path / "library" / "x.png"
    sent, _ = _run_watch_once(monkeypatch, tmp_path, finalized=dest, handback=False)
    assert sent == [], "--no-handback still touched the clipboard"
    sent, _ = _run_watch_once(monkeypatch, tmp_path, finalized=None)
    assert sent == [], "a capture that was never filed has no file to hand back"


def test_handback_outcomes_are_explained_not_echoed(monkeypatch, tmp_path, capsys):
    _run_watch_once(
        monkeypatch,
        tmp_path,
        finalized=None,
        stdout_extra=[
            r"HANDBACK_OK \\wsl.localhost\U\lib\fig.png",
            r"HANDBACK_SKIPPED \\wsl.localhost\U\lib\fig.png :: a different image was copied since the capture",
            r"HANDBACK_FAILED \\wsl.localhost\U\lib\fig.png :: file not found",
        ],
    )
    out = capsys.readouterr().out
    assert "clipboard now carries the tagged file: fig.png" in out, out
    assert "clipboard left alone: a different image was copied" in out, out
    assert "clipboard NOT updated: file not found" in out, out
    assert "HANDBACK_" not in out, f"a raw protocol line reached the user: {out!r}"


def _filed_capture(library, name, md5_8, w=40, h=30):
    """A library file plus the sidecar the watcher's filing writes for it."""
    library.mkdir(parents=True, exist_ok=True)
    img = library / name
    Image.new("RGB", (w, h), (1, 2, 3)).save(img)
    rec = {
        "source_detail": {
            "clipboard_capture": {
                "png": rf"C:\st\clip-20260917-210754-{md5_8}.png",
                "width": w,
                "height": h,
            }
        }
    }
    (library / (name + ".figcite.json")).write_text(json.dumps(rec))
    return img


def _watch_lines(monkeypatch, tmp_path, lines):
    import subprocess
    import sys

    class FakeStdin:
        def __init__(self):
            self.sent = []

        def write(self, s):
            self.sent.append(s)

        def flush(self):
            pass

        def close(self):
            pass

    class FakeProc:
        stdin = FakeStdin()
        stdout = iter(lines)

        def terminate(self):
            pass

    proc = FakeProc()
    monkeypatch.setattr(C, "wsl_to_win", lambda p: "WIN:" + str(p))
    monkeypatch.setattr(C, "staging_dirs", lambda: (str(tmp_path), tmp_path))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(C, "PS_EXE", sys.executable)
    C.watch(max_hours=0.001)
    return proc.stdin.sent


def test_copying_an_already_filed_figure_again_hands_its_file_back(monkeypatch, tmp_path):
    """Measured 2026-09-17: the user copied the high-res figure again from
    Firefox. Its pixels matched the image the watcher had last seen, so dedupe
    rightly did not file it twice -- but nothing handed the file back either, and
    the paste was a bare bitmap. Already filed must mean "hand back the filed
    copy", not "do nothing"."""
    md5 = "3142010B5699303E1DF75FDF4E27CCCB"
    library = tmp_path / "library"
    _filed_capture(library, "other--1.png", "AAAABBBB")
    filed = _filed_capture(library, "10.1016-x--2.png", md5[:8], 2239, 2098)
    monkeypatch.setattr(store, "LIBRARY", library)

    sent = _watch_lines(monkeypatch, tmp_path, ["WATCH_START x", f"RECOPIED {md5} 2239 2098"])
    assert sent == [f"HANDBACK {md5} - WIN:{filed}\n"], sent


def test_a_recopy_is_not_matched_to_a_different_sized_image(monkeypatch, tmp_path, capsys):
    """Eight hex characters of md5 in a filename are a strong hint, not proof;
    the dimensions the watcher reports have to agree too."""
    md5 = "3142010B5699303E1DF75FDF4E27CCCB"
    library = tmp_path / "library"
    _filed_capture(library, "x--1.png", md5[:8], 100, 100)
    monkeypatch.setattr(store, "LIBRARY", library)

    sent = _watch_lines(monkeypatch, tmp_path, [f"RECOPIED {md5} 2239 2098"])
    assert sent == []
    assert "RECOPIED" not in capsys.readouterr().out, "raw protocol line reached the user"


# ------------------------------------------- paste-time caption, 2026-09-17


def _store_in(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MANIFEST", tmp_path / "m.jsonl")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "LIBRARY", tmp_path / "library")


def _registered(tmp_path, *, confirmed):
    from figcite.provenance import Record

    img = tmp_path / "library" / "fig.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), (9, 9, 9)).save(img)
    rec = Record(
        doi="10.1016/j.celrep.2026.117555",
        short_cite="Ghafouri et al. 2026",
        confirmed=confirmed,
    )
    store.register_existing(img, rec)
    return img


def _sent_caption(line):
    import base64

    b64 = line.split(" ")[2]
    return None if b64 == "-" else base64.b64decode(b64).decode("utf-8")


def test_a_confirmed_figure_is_handed_back_with_its_caption(monkeypatch, tmp_path):
    """PowerPoint is offered the figure plus this caption at paste time, so the
    DOI shows up with no command run. It comes from the manifest -- the same
    source `figcite apply` captions from."""
    _store_in(tmp_path, monkeypatch)
    dest = _registered(tmp_path, confirmed=True)
    sent, _ = _run_watch_once(monkeypatch, tmp_path, finalized=dest)
    assert len(sent) == 1, sent
    assert _sent_caption(sent[0]) == (
        "Ghafouri et al. 2026 · https://doi.org/10.1016/j.celrep.2026.117555"
    )


def test_an_unconfirmed_figure_is_handed_back_without_a_caption(monkeypatch, tmp_path):
    """A guessed citation never reaches a slide -- and a paste is a slide. The
    file still goes back (its record says unconfirmed); the caption does not."""
    _store_in(tmp_path, monkeypatch)
    dest = _registered(tmp_path, confirmed=False)
    sent, _ = _run_watch_once(monkeypatch, tmp_path, finalized=dest)
    assert len(sent) == 1 and _sent_caption(sent[0]) is None, sent


def test_the_watcher_is_asked_to_exit_before_it_is_killed():
    """While a handback is on the clipboard the watcher owns it and serves each
    paste on demand; killed outright, it leaves a clipboard that pastes nothing.
    Closing stdin lets it write the file variant in for good and exit."""
    events = []

    class P:
        class stdin:
            @staticmethod
            def close():
                events.append("close")

        def wait(self, timeout):
            events.append("wait")

        def terminate(self):
            events.append("terminate")

    C._stop(P())
    assert events == ["close", "wait"], events
