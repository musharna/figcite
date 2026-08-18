import json
import socket
import threading
import urllib.error
import urllib.request

import pytest
from PIL import Image

from figcite import service, web


@pytest.mark.live
def test_the_server_serves_pending_over_a_real_socket():
    srv = web.make_server(0)  # ephemeral port
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/pending") as r:
            assert r.status == 200
            assert isinstance(json.loads(r.read())["items"], list)
    finally:
        srv.shutdown()


@pytest.mark.live
def test_it_binds_loopback_only():
    srv = web.make_server(0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


@pytest.mark.live
def test_it_refuses_a_port_already_in_use_rather_than_moving():
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = held.getsockname()[1]
    try:
        with pytest.raises(OSError):
            web.make_server(port)
    finally:
        held.close()


# --- round 1 security fix: /api/confirm's splat exposed confirm()'s whole
# signature to the wire, including `out` (an arbitrary file-write path) ---


def _stage(tmp_path, monkeypatch, name="clip-1"):
    """Mirrors tests/test_service_confirm.py's `_stage` helper."""
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    png = staging / f"{name}.png"
    Image.new("RGB", (4, 4), (30, 90, 140)).save(png)
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


def _post(port, path, payload, headers=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.live
def test_confirm_out_field_cannot_write_outside_the_library(tmp_path, monkeypatch):
    """The regression test for the arbitrary-file-write finding.

    `service.confirm(ref, out=...)` is a real CLI capability
    (`figcite confirm -o/--out`), but `/api/confirm` splatting `**payload`
    straight into `confirm()` made the wire contract equal to the whole
    function signature, so any client could set `out=` to any path this
    process can write and have PNG bytes land there. Must fail against the
    committed, un-allowlisted route: the victim file's bytes change.
    """
    _stage(tmp_path, monkeypatch, name="clip-write-atk")
    victim = tmp_path / "VICTIM.txt"
    victim.write_bytes(b"do not touch")

    srv = web.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, body = _post(
            port,
            "/api/confirm",
            {
                "ref": "staged:clip-write-atk.png",
                "cite": "Attacker 2026",
                "out": str(victim),
            },
        )
        assert status == 200, body
        assert not body.get("error"), body
        assert victim.read_bytes() == b"do not touch", (
            "the victim file's bytes changed -- `out` reached finalize()"
        )
    finally:
        srv.shutdown()


@pytest.mark.live
def test_confirm_drops_an_unknown_field_instead_of_crashing(tmp_path, monkeypatch):
    _stage(tmp_path, monkeypatch, name="clip-junk")

    srv = web.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, body = _post(
            port,
            "/api/confirm",
            {"ref": "staged:clip-junk.png", "cite": "Junk 2026", "nonsense": 1},
        )
        assert status == 200, body
        assert "TypeError" not in json.dumps(body)
        assert body["citation"] == "Junk 2026"
    finally:
        srv.shutdown()


@pytest.mark.live
def test_confirm_still_files_a_real_capture_to_the_library(tmp_path, monkeypatch):
    """Positive control: a normal confirm through the API still works."""
    _stage(tmp_path, monkeypatch, name="clip-positive")

    srv = web.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, body = _post(
            port,
            "/api/confirm",
            {"ref": "staged:clip-positive.png", "cite": "Positive Control 2026"},
        )
        assert status == 200, body
        assert body["ok"] is True
        assert body["citation"] == "Positive Control 2026"

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/pending") as r:
            refs = [i["ref"] for i in json.loads(r.read())["items"]]
        assert "staged:clip-positive.png" not in refs
    finally:
        srv.shutdown()

    matches = [
        r
        for r in service.store.all_records().values()
        if r.citation == "Positive Control 2026" and r.confirmed
    ]
    assert matches, "no confirmed library record was filed"


@pytest.mark.live
def test_apply_route_never_forwards_allow_unconfirmed(monkeypatch):
    """No path from this API to `allow_unconfirmed`, verified, not just read."""
    calls = []

    def _stub_apply(path, out=None, **opts):
        calls.append(opts)
        return {"ok": True}

    monkeypatch.setattr(web.service, "apply", _stub_apply)

    srv = web.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, body = _post(
            port, "/api/apply", {"path": "irrelevant.pptx", "allow_unconfirmed": True}
        )
        assert status == 200, body
    finally:
        srv.shutdown()

    assert calls == [{}], f"allow_unconfirmed reached service.apply's opts: {calls!r}"


@pytest.mark.live
def test_it_refuses_a_cross_origin_post(tmp_path, monkeypatch):
    _stage(tmp_path, monkeypatch, name="clip-csrf-evil")

    srv = web.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, body = _post(
            port,
            "/api/skip",
            {"ref": "staged:clip-csrf-evil.png"},
            headers={"Origin": "http://evil.example"},
        )
        assert status == 403, body
    finally:
        srv.shutdown()


@pytest.mark.live
def test_same_origin_and_no_origin_posts_still_work(tmp_path, monkeypatch):
    _stage(tmp_path, monkeypatch, name="clip-csrf-ok1")

    srv = web.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # no Origin header at all (curl, or a same-page fetch without CORS mode)
        status, body = _post(port, "/api/skip", {"ref": "staged:clip-csrf-ok1.png"})
        assert status == 200, body

        # Origin matching the server's own address
        status, body = _post(
            port,
            "/api/skip",
            {"ref": "irrelevant"},
            headers={"Origin": f"http://127.0.0.1:{port}"},
        )
        assert status == 200, body
    finally:
        srv.shutdown()
