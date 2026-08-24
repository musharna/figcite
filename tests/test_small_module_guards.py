"""The last GUARD survivors, across the six smallest modules.

Nothing here is architectural. They are range checks, rate limiters, a
duplicate-detection threshold and a nearest-neighbour comparison -- the kind
of one-off boundary that is never wrong in a way anyone notices until it is.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from figcite import corpus, mplhook, pdfgrab, pmc, store, web
from figcite.provenance import Record


# ---------------------------------------------------------------- pmc


class _R200:
    status_code = 200
    headers: dict = {}
    content = b"ok"

    def raise_for_status(self):
        return None


def test_the_pmc_throttle_waits_for_a_partial_interval(monkeypatch):
    """`if wait > 0` survived `0 -> 1` and `Gt -> GtE` -- the same pair as
    crossref's, in the other rate limiter.

    Under `> 1` every sub-second wait is skipped, which is every wait the
    interval produces. Europe PMC's limit is what stops a corpus build being
    refused halfway through.
    """
    naps = []
    monkeypatch.setattr(pmc.time, "sleep", lambda s: naps.append(s))
    monkeypatch.setattr(pmc.requests, "get", lambda url, **kw: _R200())
    monkeypatch.setattr(pmc, "_last_call", time.monotonic() - 0.05, raising=False)

    pmc._get("https://www.ebi.ac.uk/europepmc/webservices/rest/search")

    assert naps and 0 < naps[0] < 1, f"a sub-second wait was skipped: {naps}"


def test_the_pmc_throttle_does_not_sleep_at_the_boundary(monkeypatch):
    """`wait > 0` survived `Gt -> GtE`, which sleeps zero seconds at exactly
    the interval. Pinned with a frozen clock and the interval set to 0, so
    `wait` is exactly 0 rather than nearly it."""
    naps = []
    frozen = 5000.0
    monkeypatch.setattr(pmc.time, "sleep", lambda s: naps.append(s))
    monkeypatch.setattr(pmc.time, "monotonic", lambda: frozen)
    monkeypatch.setattr(pmc.requests, "get", lambda url, **kw: _R200())
    monkeypatch.setattr(pmc, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(pmc, "_last_call", frozen, raising=False)

    pmc._get("https://www.ebi.ac.uk/europepmc/webservices/rest/search")

    assert naps == [], naps


def test_a_thumbnail_graphic_is_not_indexed_as_its_own_figure(monkeypatch):
    """`if g.get("content-type") == "thumb"` survived mutating both the key
    and the value.

    A `<graphic content-type="thumb">` is a PREVIEW of the figure it sits in.
    The comment above the line says what happens without the skip: every row
    doubles and the corpus indexes a downsampled copy -- which then competes
    with the real figure in a perceptual match and wins about half the time.
    """
    xml = b"""<root><fig><label>Figure 1</label><p>A caption</p>
      <graphic content-type="thumb" xmlns:xlink="http://www.w3.org/1999/xlink"
               xlink:href="fig1-thumb.jpg"/>
      <graphic xmlns:xlink="http://www.w3.org/1999/xlink"
               xlink:href="fig1.jpg"/>
    </fig></root>"""
    monkeypatch.setattr(pmc, "_get", lambda url, **kw: xml)

    got = pmc.figures_of("PMC1")

    assert [f.filename for f in got] == ["fig1.jpg"], (
        f"the thumbnail was indexed as a separate figure: {[f.filename for f in got]}"
    )


# ---------------------------------------------------------------- mplhook


def test_the_git_commit_is_recorded_only_when_git_answered(monkeypatch):
    """`if r.returncode == 0` survived `Eq -> NotEq` and `0 -> 1` -- the third
    copy of this guard in the codebase.

    `mplhook` stamps the commit that produced a figure. Inverted, a failed
    `git rev-parse` outside a repository has its (empty, or error) output
    recorded as the commit, and a real one is discarded.
    """

    class _R:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _R(0, "deadbeef\n"))
    assert mplhook._git_head(".") == "deadbeef"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _R(128, "not a repo\n"))
    assert mplhook._git_head(".") is None, (
        "a failed git call was recorded as the producing commit"
    )


def _savefig_that_cannot_register(monkeypatch, tmp_path, defaults):
    """Drive the real `_wrapped` with registration made to fail.

    The strict check is inline in `_wrapped`, not in a helper -- there is no
    `_register_or_raise` (I invented that name). Driving the wrapper is the
    only way to reach it.
    """
    written = tmp_path / "fig.png"

    def _original(self, fname, *a, **kw):
        Path(str(fname)).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 16)
        return "savefig-result"

    monkeypatch.setattr(mplhook, "_ORIGINAL", _original, raising=False)
    monkeypatch.setattr(mplhook, "_DEFAULTS", defaults)
    monkeypatch.setattr(
        mplhook,
        "_register_written_file",
        lambda path: (_ for _ in ()).throw(RuntimeError("no manifest")),
    )
    return written


def test_a_provenance_failure_raises_by_default(tmp_path, monkeypatch):
    """`if _DEFAULTS.get("strict", True)` survived `True -> False`.

    The default is STRICT: a figure that was written but whose provenance
    could not be recorded must fail loudly, because the file is now on disk
    looking exactly like a properly attributed one. Under the mutant the
    default flips to a warning, and warnings in a plotting script scroll past.

    `strict` is ABSENT from _DEFAULTS rather than set to True -- setting it
    would read the stored value and never reach the fallback that is under
    test.
    """
    written = _savefig_that_cannot_register(monkeypatch, tmp_path, {})

    with pytest.raises(mplhook.RegistrationError, match="could NOT record"):
        mplhook._wrapped(object(), str(written))

    assert written.exists(), "the figure itself should still have been written"


def test_a_provenance_failure_only_warns_when_strict_is_off(tmp_path, monkeypatch):
    """Positive control: "always raise" passes the test above and makes the
    documented opt-out impossible."""
    written = _savefig_that_cannot_register(monkeypatch, tmp_path, {"strict": False})

    with pytest.warns(UserWarning, match="could NOT record"):
        got = mplhook._wrapped(object(), str(written))

    assert got == "savefig-result", "savefig's own return value was changed"


# ---------------------------------------------------------------- web


def _post(port, path, body: bytes, content_length=None):
    """POST with a Content-Length we choose, which is the whole point."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    n = len(body) if content_length is None else content_length
    conn.request("POST", path, body=body, headers={"Content-Length": str(n)})
    r = conn.getresponse()
    out = (r.status, r.read())
    conn.close()
    return out


@pytest.mark.parametrize(
    "declared,expect_ok",
    [(0, True), (-1, False), ("MAX", True), ("OVER", False)],
    ids=["zero", "negative", "at-max", "over-max"],
)
def test_the_content_length_range_check(declared, expect_ok):
    """`if n < 0 or n > MAX_BODY` survived `Lt -> LtE`, `Gt -> GtE` and
    `0 -> 1` -- three boundaries on one line, none of them tested.

    A body of exactly MAX_BODY is allowed; one byte more is not. Zero is
    allowed -- it is what a POST with no arguments sends, and under `<= 0`
    every one of those is refused. A NEGATIVE length must be refused rather
    than handed to `rfile.read`.

    My first version of this test computed `n < 0 or n > MAX_BODY` in the
    test itself and asserted the result. That is an assertion about Python,
    not about figcite, and all three mutants survived it. Driven through a
    real server now, so the answer comes from the code under test.
    """
    import threading

    srv = web.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # The at-max case must actually SEND its declared bytes: declaring
        # MAX_BODY and sending none left the server blocked in `rfile.read`
        # until the client timed out, which is a hung fixture rather than a
        # finding. 5 MiB of padding is cheap over loopback.
        if declared == "MAX":
            head, tail = b'{"pad":"', b'"}'
            pad = head + b"a" * (web.MAX_BODY - len(head) - len(tail)) + tail
            assert len(pad) == web.MAX_BODY
            n, payload = web.MAX_BODY, pad
        elif declared == "OVER":
            n, payload = web.MAX_BODY + 1, b""
        else:
            n, payload = declared, b""
        status, body = _post(port, "/api/pending", payload, content_length=n)

        if expect_ok:
            assert b"invalid Content-Length" not in body, (
                f"a Content-Length of {n} was refused: status {status} {body[:200]!r}"
            )
        else:
            assert b"invalid Content-Length" in body or status >= 400, (
                f"a Content-Length of {n} was accepted: status {status} {body[:200]!r}"
            )
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- corpus


def test_the_build_limit_stops_at_the_requested_number(monkeypatch):
    """`if limit is not None and indexed_papers >= limit` survived
    `GtE -> Gt`.

    `--limit 3` means three papers, not four. Under `>` the build indexes one
    more than asked for every time -- which on a throttled API is an extra
    paper's worth of requests, and on a `--limit 1` smoke test doubles it.
    """
    calls = []

    monkeypatch.setattr(
        corpus.pmc,
        "lookup_dois",
        lambda dois, batch=8: pmc.Lookup(
            records=[
                pmc.PmcRecord(
                    doi=d,
                    pmcid=f"PMC{i}",
                    title="T",
                    year="2020",
                    is_open_access=True,
                )
                for i, d in enumerate(dois)
            ],
            unreachable={},
        ),
    )
    monkeypatch.setattr(
        corpus.pmc, "figures_of", lambda pmcid: calls.append(pmcid) or []
    )
    monkeypatch.setattr(corpus.pmc, "image_urls", lambda p: {})
    monkeypatch.setattr(corpus.pmc, "licence_of", lambda p: "cc-by")

    # PmcRecord requires title and year; omitting them raised a TypeError
    # inside the stub, which `build`'s own total-failure handler swallowed --
    # so the test reported "0 papers indexed" and looked like a real finding.
    corpus.build(["10.1/a", "10.1/b", "10.1/c"], limit=1)

    assert len(calls) == 1, f"a limit of 1 indexed {len(calls)} paper(s): {calls}"


def test_a_duplicate_exactly_at_the_dhash_threshold_counts(monkeypatch):
    """`hamming(dh, row.dhash) <= DHASH_THRESHOLD` survived `LtE -> Lt`.

    The threshold is the furthest distance still considered the same figure,
    matching `match.by_dhash`. Under `<` the two disagree by one, so a figure
    the matcher calls a hit is not reported as a duplicate -- and the
    duplicate warning exists to stop the same figure being credited twice in
    one deck.
    """
    from figcite.provenance import dhash_bytes  # noqa: F401

    class _Row:
        def __init__(self, dh):
            self.dhash, self.doi = dh, "10.1/x"
            self.pmcid, self.label, self.image_path = "P", "F1", "i.png"

    # `can_compare_dhash` refuses an all-zero (or all-one) hash outright: a
    # flat fill hashes identically whatever colour it is. So the base carries
    # real signal and the neighbour differs from it by exactly the threshold.
    base_int = 0xFF
    base = format(base_int, "016x")
    at_edge = format(base_int ^ (((1 << corpus.DHASH_THRESHOLD) - 1) << 8), "016x")
    assert corpus.can_compare_dhash(base) and corpus.can_compare_dhash(at_edge)
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(corpus, "all_rows", lambda conn: [_Row(at_edge)])

    got = corpus.duplicates_of_dhash(base)

    assert len(got) == 1, (
        f"a figure exactly at hamming {corpus.DHASH_THRESHOLD} was not "
        f"reported as a duplicate"
    )


# ---------------------------------------------------------------- store


def test_the_nearest_record_wins_ties_by_first_seen(monkeypatch):
    """`if d < best_d` survived `Lt -> LtE`.

    Under `<=` the LAST equally-distant record wins instead of the first.
    With two records at the same perceptual distance the answer becomes
    manifest-order-dependent -- and `all_records()` is keyed by sha, so that
    order is effectively arbitrary. Two figures tie all the time: a figure
    filed twice under different citations is exactly the case this fallback
    exists for.
    """
    a = Record(sha256="a" * 64, dhash="0" * 16, citation="FIRST", confirmed=True)
    b = Record(sha256="b" * 64, dhash="0" * 16, citation="SECOND", confirmed=True)
    monkeypatch.setattr(store, "all_records", lambda: {a.sha256: a, b.sha256: b})

    got = store.find_similar("0" * 16)

    assert got is not None
    assert got[0].citation == "FIRST", (
        f"a tie was won by the later record: {got[0].citation}"
    )


def test_a_match_exactly_at_max_distance_is_returned(monkeypatch):
    """`if best is not None and best_d <= max_distance` survived
    `LtE -> Lt`.

    max_distance is the furthest still acceptable. Under `<` a record exactly
    at the limit is discarded and the caller is told nothing matched.
    """
    rec = Record(sha256="c" * 64, dhash="0" * 15 + "f", citation="C", confirmed=True)
    monkeypatch.setattr(store, "all_records", lambda: {rec.sha256: rec})

    d = 4  # 0xf is four set bits away from 0x0
    assert store.find_similar("0" * 16, max_distance=d) is not None, (
        f"a record at exactly distance {d} was rejected"
    )
    assert store.find_similar("0" * 16, max_distance=d - 1) is None


# ---------------------------------------------------------------- pdfgrab


def test_page_one_is_in_range(tmp_path):
    """`if not (1 <= page <= doc.page_count)` survived `1 -> 2`.

    Pages are 1-based, so page 1 is the first valid page. Under `2 <= page`
    the first page of every PDF is reported out of range -- and page 1 is
    where the figure usually is.
    """
    import fitz

    doc = fitz.open()
    doc.new_page(width=300, height=300)
    p = tmp_path / "one.pdf"
    doc.save(str(p))
    doc.close()

    assert pdfgrab.list_images(str(p), 1) == []  # no images, but IN RANGE

    with pytest.raises(ValueError, match="out of range"):
        pdfgrab.list_images(str(p), 2)


def test_image_index_zero_is_in_range(tmp_path):
    """`if not (0 <= image_index < len(infos))` survived `0 -> 1`.

    Image indices are 0-based -- `figcite images` prints them that way and
    `figcite grab --image 0` is the commonest call there is. Under
    `1 <= image_index` the first image on every page is refused.
    """
    import random

    import fitz
    from PIL import Image, ImageDraw

    img = tmp_path / "fig.png"
    rng = random.Random(3)
    im = Image.new("RGB", (200, 150), "white")
    d = ImageDraw.Draw(im)
    for _ in range(12):
        x, y = rng.randrange(150), rng.randrange(100)
        d.rectangle([x, y, x + 30, y + 25], fill=(rng.randrange(256), 40, 90))
    im.save(img)

    doc = fitz.open()
    page = doc.new_page(width=300, height=300)
    page.insert_image(fitz.Rect(20, 20, 220, 170), filename=str(img))
    p = tmp_path / "withimg.pdf"
    doc.save(str(p))
    doc.close()

    out = pdfgrab.crop(str(p), 1, str(tmp_path / "o.png"), image_index=0)
    assert out and (tmp_path / "o.png").exists(), out

    with pytest.raises(ValueError, match="image index"):
        pdfgrab.crop(str(p), 1, str(tmp_path / "x.png"), image_index=5)
