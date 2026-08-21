"""`/api/thumb` driven over a real socket, which nothing did.

This is the route that serves actual figure bytes, and it was reachable in
tests only through `service.thumbnail()` -- never over HTTP. The gap shows up
most plainly in `test_web_server.py::test_it_rejects_a_forged_host_header...`,
whose docstring gives `/api/thumb` as the reason the Host guard exists ("leaks
actual figure bytes") and then drives `/api/pending` instead. The rationale
named a route the test never touched, so the guard was never observed doing the
job it was justified by.

The sweep's evidence for the same hole: `u.path == "/api/thumb"` survives
`Eq -> NotEq`. Under that mutant every other GET is answered by the thumbnail
handler and `/api/thumb` itself falls through to 404 -- a total inversion of
the route table that no test could see, because no test asked this route for
bytes.

What is pinned here, beyond "it returns 200":

  - the bytes are a decodable PNG, and a SMALLER one than the source, so the
    route cannot pass through an untouched original;
  - `Content-Length` matches the body, because a browser `<img>` either
    truncates or hangs when it does not, and neither shows up as an exception
    in a test that only reads `.status`;
  - 404 and 410 stay distinct (Ruling 10: "no such ref" and "the ref is real,
    its file is gone" are different problems for whoever is debugging);
  - an opaque ref cannot be steered into a path outside the library;
  - a forged `Host` is refused ON THIS ROUTE, and no image bytes come back
    with the refusal.
"""

from __future__ import annotations

import io
import json
import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest
from PIL import Image

from figcite import service, store, web
from figcite.provenance import Record, now_stamps

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@contextmanager
def _serving():
    srv = web.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()


def _get(port, path, timeout=5):
    """Status, headers and RAW BYTES -- the body must not be decoded as text."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _raw_get(port, path, host, timeout=5):
    """A GET with a `Host` urllib will not let us forge."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
        )
        s.settimeout(timeout)
        chunks = []
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pytest.fail(f"no response within {timeout}s -- the handler thread hung")
    return b"".join(chunks)


def _stage(tmp_path, monkeypatch, name="thumb-1", size=(1200, 900)):
    """A staged capture big enough that a thumbnail is visibly not the original.

    A 4x4 fixture (what the other web tests use) is already smaller than the
    480px bound, so it would pass a "the thumbnail is small" assertion without
    the resize ever running.
    """
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    png = staging / f"{name}.png"
    im = Image.new("RGB", size)
    for x in range(0, size[0], 2):  # noise, so PNG cannot trivially compress
        for y in range(0, size[1], 2):
            im.putpixel((x, y), ((x * 7) % 256, (y * 13) % 256, (x + y) % 256))
    im.save(png)
    (staging / f"{name}.pending.json").write_text(
        json.dumps(
            {
                "png": str(png),
                "capture": {"process": "firefox", "title": "A paper"},
                "inference": {},
            }
        )
    )
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))
    return png


# --- the happy path, which is what kills the route-table mutant -----------


def test_a_staged_capture_comes_back_as_decodable_png_bytes(tmp_path, monkeypatch):
    """THE positive control for this file, and the test that kills
    `u.path == "/api/thumb"` -> `!=` (which sends this request to 404)."""
    _stage(tmp_path, monkeypatch)

    with _serving() as port:
        status, headers, body = _get(port, "/api/thumb?ref=staged:thumb-1.png")

    assert status == 200, body[:200]
    assert headers["Content-Type"] == "image/png", headers
    assert body.startswith(PNG_MAGIC), body[:32]
    Image.open(io.BytesIO(body)).verify()  # decodes, not just PNG-shaped


def test_the_declared_length_matches_the_bytes_sent(tmp_path, monkeypatch):
    """A wrong `Content-Length` makes a browser truncate the image or hold the
    connection open, and neither shows up as an exception in a test that reads
    `.status`.

    This has to be read off the RAW socket, and the first version of it did
    not. Comparing `headers["Content-Length"]` against `len(r.read())` from
    urllib compares two numbers that move together: urllib hands back exactly
    `Content-Length` bytes, so understating the length shortens the body to
    match and the assertion holds either way. Caught by mutating the header to
    `len(blob) - 1` and watching the test pass -- a test that could not fail,
    written to guard against tests that cannot fail.

    Reading the socket directly is what makes the two quantities independent:
    the declared number comes from the header, the real one from counting what
    actually arrived.
    """
    _stage(tmp_path, monkeypatch)

    with _serving() as port:
        raw = _raw_get(port, "/api/thumb?ref=staged:thumb-1.png", f"127.0.0.1:{port}")

    head, _, body = raw.partition(b"\r\n\r\n")
    declared = int(
        [h for h in head.split(b"\r\n") if h.lower().startswith(b"content-length:")][0]
        .split(b":", 1)[1]
        .strip()
    )

    assert declared == len(body), f"declared {declared}, actually sent {len(body)}"
    assert body.startswith(PNG_MAGIC), body[:32]
    Image.open(io.BytesIO(body)).verify()  # a truncated PNG must not decode


def test_it_serves_a_thumbnail_rather_than_the_original_file(tmp_path, monkeypatch):
    """`max_px` is the difference between a preview and shipping the full
    capture to the browser on every row of the pending list."""
    src = _stage(tmp_path, monkeypatch, size=(1200, 900))

    with _serving() as port:
        _status, _headers, body = _get(port, "/api/thumb?ref=staged:thumb-1.png")

    served = Image.open(io.BytesIO(body))
    assert max(served.size) <= 480, f"served at {served.size}, not thumbnailed"
    assert served.size != (1200, 900)
    assert len(body) < src.stat().st_size, (
        f"the 'thumbnail' ({len(body)}B) is no smaller than the original "
        f"({src.stat().st_size}B)"
    )


# --- the three ways it can decline, kept distinct -------------------------


def test_an_unknown_ref_is_404_and_returns_no_image(tmp_path, monkeypatch):
    _stage(tmp_path, monkeypatch)

    with _serving() as port:
        status, headers, body = _get(port, "/api/thumb?ref=staged:no-such-file.png")

    assert status == 404, body[:200]
    assert PNG_MAGIC not in body, "an error response carried image bytes"
    assert headers["Content-Type"] == "application/json"
    assert "unknown ref" in json.loads(body)["error"]


def test_a_real_record_with_no_file_is_410_not_404(tmp_path, monkeypatch):
    """Ruling 10, and the same three-outcome discipline the rest of the tool
    runs on: "there is no such figure" and "there is, and its bytes are gone"
    send whoever is debugging to different places. A single 404 for both
    erases the distinction at the only point it is observable.
    """
    u, loc = now_stamps()
    sha = "5c" * 32
    store.put(
        Record(
            sha256=sha,
            dhash="0" * 16,
            source_kind="clipboard",
            confirmed=False,
            captured_utc=u,
            captured_local=loc,
        )
    )

    with _serving() as port:
        status, _headers, body = _get(port, f"/api/thumb?ref=filed:{sha}")
        # Positive control: the SAME route, a sha with no record at all.
        unknown, _h2, _b2 = _get(port, "/api/thumb?ref=filed:" + "ab" * 32)

    assert status == 410, body[:200]
    assert "image file missing" in json.loads(body)["error"]
    assert unknown == 404, (
        f"a nonexistent record also returned {unknown}; 410 and 404 are not "
        f"actually being told apart"
    )


def test_a_missing_ref_parameter_is_declined_not_crashed(tmp_path, monkeypatch):
    _stage(tmp_path, monkeypatch)

    with _serving() as port:
        status, _headers, body = _get(port, "/api/thumb")

    assert status == 404, body[:200]
    assert PNG_MAGIC not in body


# --- the security properties, at the layer a browser reaches --------------


@pytest.mark.parametrize(
    "ref",
    [
        "staged:../../../../etc/passwd",
        "staged:/etc/passwd",
        "filed:../../../../etc/passwd",
        "sha:../../../../etc/hostname",
    ],
)
def test_an_opaque_ref_cannot_address_a_file_outside_the_library(
    tmp_path, monkeypatch, ref
):
    """`_resolve_ref_to_path`'s stated property -- never build a path out of
    the ref's own characters -- asserted from the wire, where the ref actually
    comes from."""
    _stage(tmp_path, monkeypatch)

    with _serving() as port:
        status, _headers, body = _get(port, f"/api/thumb?ref={ref}")

    assert status in (404, 410), f"{ref} -> {status}: {body[:200]}"
    assert b"root:" not in body, "a passwd file came back over the thumb route"
    assert PNG_MAGIC not in body


def test_a_readable_image_outside_staging_is_still_unreachable(tmp_path, monkeypatch):
    """The traversal test that can actually fail for the reason it names.

    Pointing a traversal at `/etc/passwd` only shows the route did not return a
    PNG -- a vulnerable implementation fails that assertion by *erroring* on a
    non-image, which reads as a pass for the wrong reason. So the target here
    is a real, decodable PNG one directory above staging: under
    `Path(staging) / ref`, this request succeeds and hands back its bytes.

    Verified against exactly that implementation before being committed.
    """
    _stage(tmp_path, monkeypatch)
    secret = tmp_path / "not-yours.png"
    Image.new("RGB", (64, 64), (200, 30, 30)).save(secret)

    with _serving() as port:
        status, _headers, body = _get(port, "/api/thumb?ref=staged:../not-yours.png")

    assert status == 404, f"a file outside staging was addressable: {status}"
    assert PNG_MAGIC not in body


def test_a_forged_host_is_refused_on_the_thumb_route_itself(tmp_path, monkeypatch):
    """The route the Host guard was justified by, finally driving it.

    A public page that DNS-rebinds to 127.0.0.1 is same-origin to the browser
    and `Origin` is not sent on a plain GET, so `Host` is the only thing
    standing between that page and the user's captured figures.
    """
    _stage(tmp_path, monkeypatch)

    with _serving() as port:
        forged = _raw_get(port, "/api/thumb?ref=staged:thumb-1.png", "evil.example")
        # Positive control: same request, honest Host, on the same server.
        honest = _raw_get(
            port, "/api/thumb?ref=staged:thumb-1.png", f"127.0.0.1:{port}"
        )

    assert b" 403 " in forged.split(b"\r\n", 1)[0], forged[:120]
    assert PNG_MAGIC not in forged, "a forged Host still received figure bytes"
    assert b" 200 " in honest.split(b"\r\n", 1)[0], honest[:120]
    assert PNG_MAGIC in honest, (
        "the honest request got no image either, so the 403 above proves nothing"
    )


def test_the_localhost_spelling_reaches_the_thumb_route(tmp_path, monkeypatch):
    """I5 again, on this route: `localhost:<port>` is what people type, and it
    must not be treated as a forged Host."""
    _stage(tmp_path, monkeypatch)

    with _serving() as port:
        resp = _raw_get(port, "/api/thumb?ref=staged:thumb-1.png", f"localhost:{port}")

    assert b" 200 " in resp.split(b"\r\n", 1)[0], resp[:120]
    assert PNG_MAGIC in resp
