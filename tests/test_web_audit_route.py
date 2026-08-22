"""`/api/audit`, which no test had ever requested.

The route string `"/api/audit"` in `figcite/web.py:301` survived
`-> "MUTANT"` in the string sweep, and `grep -rl "api/audit" tests/` came
back empty: nothing in the suite drives that endpoint. Under the mutation
the route simply stops existing -- every request for it falls through to the
404 branch -- and the whole suite still passes.

That is a route, not a message. `service.audit()` is well covered directly,
which is exactly what makes this easy to miss: the work behind the endpoint
is tested, and the endpoint is not. The web UI's audit view calls this and
would get a 404.

The dispatch is a chain of `elif u.path == ...`. A test that only asserts
"/api/audit returns 200" also passes if the handler is wired to the wrong
branch, so the response is checked for content that only the audit handler
produces, and a neighbouring route is asserted to still work -- one shared
handler answering everything is the failure that a single-route test cannot
see.
"""

from __future__ import annotations

import json
import random
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import fitz
import pytest
from PIL import Image, ImageDraw

from figcite import store, web


@pytest.fixture(autouse=True)
def _own_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MANIFEST", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)


@contextmanager
def _serving():
    srv = web.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()


def _post(port, path, payload, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"_raw": body[:200].decode(errors="replace")}


def _a_pdf(tmp_path):
    rng = random.Random(5)
    im = Image.new("RGB", (420, 300), (250, 250, 248))
    d = ImageDraw.Draw(im)
    for _ in range(20):
        x, y = rng.randrange(360), rng.randrange(240)
        d.rectangle(
            [x, y, x + rng.randrange(15, 55), y + rng.randrange(15, 50)],
            fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)),
        )
    img = tmp_path / "fig.png"
    im.save(img)

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(60, 60, 400, 300), filename=str(img))
    p = tmp_path / "deck.pdf"
    doc.save(str(p))
    doc.close()
    return p


def test_the_audit_route_answers_and_returns_an_audit(tmp_path):
    """THE finding: this request had never been made.

    The assertion is on keys only `service.audit` produces, so wiring the
    path to some other handler fails here rather than passing on a 200.
    """
    pdf = _a_pdf(tmp_path)

    with _serving() as port:
        status, body = _post(port, "/api/audit", {"path": str(pdf)})

    assert status == 200, f"/api/audit did not answer: {status} {body}"
    for key in ("kind", "rows", "pictures"):
        assert key in body, f"the response is not an audit report: {sorted(body)}"
    assert body["kind"] == "pdf", body
    assert body["pictures"] == 1, body
    assert body["rows"] and body["rows"][0]["location"].startswith("page "), body


def test_a_route_that_does_not_exist_is_still_a_404(tmp_path):
    """Positive control on the dispatch itself.

    Without it, a handler that answers 200 for ANY path satisfies the test
    above -- and the route table would have stopped discriminating while
    both tests stayed green.
    """
    with _serving() as port:
        status, _ = _post(port, "/api/not-a-real-route", {"path": "x"})

    assert status == 404, f"an unknown route answered {status}"


def test_the_audit_route_does_not_answer_for_its_neighbour(tmp_path):
    """The routes are a chain of `elif u.path == ...`, so a mis-wired branch
    shows up as one handler answering for two paths. `/api/whereis` sits
    immediately above `/api/audit` in that chain and returns a different
    shape."""
    pdf = _a_pdf(tmp_path)

    with _serving() as port:
        _, audit_body = _post(port, "/api/audit", {"path": str(pdf)})
        whereis_status, whereis_body = _post(port, "/api/whereis", {"ref": "nope"})

    assert "rows" in audit_body, audit_body
    assert "rows" not in whereis_body or whereis_body.get("kind") != "pdf", (
        f"/api/whereis answered with an audit report: {whereis_body}"
    )
    assert whereis_status in (200, 400, 404, 410), whereis_status


def test_the_audit_route_rejects_a_request_with_no_path(tmp_path):
    """`AUDIT_FIELDS = {"path"}` is the contract. A missing field must be a
    refusal, not a traceback and not a report about nothing."""
    with _serving() as port:
        status, body = _post(port, "/api/audit", {})

    assert status != 200, f"/api/audit accepted a request with no path: {body}"
    assert status < 500, f"a missing field produced a server error: {status} {body}"
