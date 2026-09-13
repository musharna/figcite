"""One unknown path cannot falsify an ordering.

`do_GET` and `do_POST` dispatch through a chain of `elif u.path == "..."`.
There is already a test that an unknown route 404s, and it is a real test --
but it probes ONE path, `/api/not-a-real-route`, and a single probe cannot
exercise an order. Widen `==` to `<=` or `>=` and a route stops matching a
NAME and starts matching a HALF-LINE; whether a probe falls inside that
half-line depends entirely on where its spelling sorts.

`"not-a-real-route"` sorts in the middle of the table, so it lands in a gap --
above `/api/apply`, below `/api/whereis` -- which is exactly the pair of
directions the reduced-ROR sweep found surviving:

    /api/whereis -> >=   needs a probe ABOVE it; "not-..." is below
    /api/apply   -> <=   needs a probe BELOW it; "not-..." is above

So the probes have to bracket every route on both sides, and
`test_every_route_is_reachable_from_both_sides` asserts that they do rather
than trusting that they do.

The awkward one is `/`. Every path a normal client sends begins with `/`, and
`/` is a prefix of all of them, so `/` is the LEAST such string and nothing
sorts below it -- which would make `u.path <= "/"` unkillable.

It is not. `GET * HTTP/1.1` is a well-formed request line, http.server hands
`self.path == "*"` straight to `do_GET`, and `"*" < "/"`. Measured, not
assumed: the probe below reaches dispatch and 404s correctly today. Widened,
it would serve the whole HTML application to a request for `*`. `urllib` will
not send a non-`/` path, so that probe needs a raw socket.
"""

from __future__ import annotations

import ast
import json
import pathlib
import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from figcite import web

# Chosen to bracket the tables; the bracketing itself is asserted below.
#   "*"          sorts below "/", and is reachable only in asterisk-form
#   "/api/aaa"   sorts below every /api/... route but above "/"
#   "/api/zzz"   sorts above every route
# The marker both the asterisk test and its positive control look for. ONE
# constant on purpose: the first version asserted `b"<html"` was absent, and
# the page opens `<!doctype html>` with no literal `<html` tag anywhere -- so
# that assertion would have passed with the entire UI served to `GET *`. The
# control below fails loudly if this marker ever stops identifying the page.
PAGE_MARKER = b"<!doctype html"

ASTERISK = "*"
BELOW_API = "/api/aaa"
ABOVE_ALL = "/api/zzz"
PROBES = (ASTERISK, BELOW_API, ABOVE_ALL)


@contextmanager
def _serving():
    srv = web.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()


def _request(port, path, method):
    data = json.dumps({"path": "x"}).encode() if method == "POST" else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()[:2000]
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:2000]


def _raw_request(port, request_target, method):
    """Send a request line verbatim, including targets urllib refuses to send."""
    s = socket.create_connection(("127.0.0.1", port), timeout=30)
    try:
        s.sendall(
            (
                f"{method} {request_target} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Content-Length: 0\r\n"
                f"Connection: close\r\n\r\n"
            ).encode()
        )
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    status = int(data.split(b" ")[1]) if data else 0
    return status, data[:2000]


def _route_table(fn_name: str) -> list[str]:
    """The literal paths `fn_name` compares `u.path` against, read from source.

    Read rather than hardcoded, so adding a route to web.py cannot leave this
    file quietly testing the old table.
    """
    tree = ast.parse(pathlib.Path(web.__file__).read_text())
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == fn_name:
            return [
                ast.literal_eval(ast.unparse(n.comparators[0]))
                for n in ast.walk(fn)
                if isinstance(n, ast.Compare)
                and len(n.ops) == 1
                and isinstance(n.ops[0], ast.Eq)
                and "u.path" in ast.unparse(n)
            ]
    raise AssertionError(f"{fn_name} not found in web.py")


@pytest.mark.parametrize("fn_name", ["do_GET", "do_POST"])
def test_every_route_is_reachable_from_both_sides(fn_name):
    """The premise the 404 tests rest on, asserted rather than assumed.

    Each route widened to `<=` admits everything below it and widened to `>=`
    everything above it. Unless some probe lies on each side of each route,
    the tests below keep passing while testing nothing -- which is exactly how
    the existing single-probe test came to miss two live mutants.
    """
    table = _route_table(fn_name)
    assert table, f"no routes found in {fn_name}"
    unknown = [p for p in PROBES if p not in table]
    for route in table:
        assert any(p < route for p in unknown), (
            f"no probe sorts below {route!r} in {fn_name}, so widening it to "
            f"`<=` cannot be observed. Probes: {unknown}"
        )
        assert any(p > route for p in unknown), (
            f"no probe sorts above {route!r} in {fn_name}, so widening it to "
            f"`>=` cannot be observed. Probes: {unknown}"
        )


# The dispatch's own fall-through, distinct from every handler's own 404.
#
# Asserting the STATUS alone is not enough, and the measurement said so: two
# `/api/thumb` mutants survived the first version of this file. `/api/thumb` is
# the last branch in the GET chain, so a widened comparison routes the probe
# INTO the thumbnail handler, which finds no `ref`, raises KeyError and answers
# ... 404. Same status, different handler, and the test could not tell them
# apart. The body can: this is the router saying "no such route", not a handler
# saying "no such thing".
NOT_FOUND = "not found"


def _error_of(body: bytes) -> str | None:
    """The `error` field of a JSON response, headers tolerated."""
    payload = body.split(b"\r\n\r\n", 1)[-1]
    try:
        return json.loads(payload).get("error")
    except (ValueError, AttributeError):
        return None


@pytest.mark.parametrize("probe", [BELOW_API, ABOVE_ALL])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_an_unknown_route_is_404_from_either_side(probe, method):
    """A route matching a half-line answers here with real data -- `GET
    /api/aaa` returning the pending list, for instance -- or, more subtly,
    reaches a handler that happens to fail with the same status."""
    with _serving() as port:
        status, body = _request(port, probe, method)
    assert status == 404, f"{method} {probe} answered {status}: {body[:200]!r}"
    assert _error_of(body) == NOT_FOUND, (
        f"{method} {probe} was answered by a HANDLER rather than falling "
        f"through the route table: {body[:200]!r}"
    )


def test_asterisk_form_does_not_reach_the_root_page():
    """`*` is the only request target that sorts BELOW `/`, so it is the only
    way `u.path == "/"` widened to `<=` can be caught. Serving the application
    to it would be handing out the whole UI for a request naming no resource
    at all."""
    with _serving() as port:
        status, body = _raw_request(port, ASTERISK, "GET")
    assert status == 404, f"GET * answered {status}: {body[:200]!r}"
    assert PAGE_MARKER not in body.lower(), "the HTML page was served to `GET *`"
    assert _error_of(body) == NOT_FOUND, (
        f"`GET *` was answered by a handler rather than the route table: {body[:200]!r}"
    )


def test_a_known_route_still_answers():
    """Positive control. Every test above asserts a NEGATIVE -- that nothing
    answers -- and a server that refused all requests would satisfy them."""
    with _serving() as port:
        status, body = _request(port, "/api/pending", "GET")
    assert status == 200, f"a real route stopped answering: {status} {body[:200]!r}"
    assert b"items" in body, body[:200]


def test_the_raw_client_can_reach_a_real_route():
    """Positive control for `_raw_request` specifically. The asterisk test
    asserts a 404, and a broken hand-rolled client that never reached the
    server at all would produce one just as convincingly."""
    with _serving() as port:
        status, body = _raw_request(port, "/", "GET")
    assert status == 200, f"the raw client could not fetch `/`: {status}"
    assert PAGE_MARKER in body.lower(), body[:200]
