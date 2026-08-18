"""A local, single-user web front end. Loopback only, no auth.

That is a deliberate choice for a tool that reads your figure library and
writes citations: it is reachable only from this machine, and adding auth to a
single-user localhost tool buys nothing it does not also cost in friction.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import service
from .webui import PAGE  # stub created in this task, filled in Task 7

HOST = "127.0.0.1"

# A POST body is a small JSON control message (a ref, a doi, a path) -- never
# an image. Round-2 finding I2: an unvalidated Content-Length lets a client
# send `Content-Length: -1`, which makes `self.rfile.read(-1)` read until EOF
# and blocks the handler thread for as long as the client holds the
# connection open. Bounding it both rejects the negative case and caps how
# much memory/time a single request can claim.
MAX_BODY = 5 * 1024 * 1024  # 5 MiB

# Wire fields each mutating route accepts, INCLUDING the field that names the
# target ("ref" or "path") -- `_fields` validates the whole payload against
# this set (round-2 finding M1: an unknown field used to be dropped silently,
# which meant the allowlist could never report being too narrow). `out` is
# deliberately absent from CONFIRM_FIELDS: `service.confirm(ref, out=...)` is
# a real CLI capability (`figcite confirm -o/--out`), but a route is a trust
# boundary, and a client JSON body is untrusted input. Splatting the whole
# payload into confirm() made the wire contract equal to confirm()'s full
# signature, so `out=` -- a filesystem destination -- became settable by any
# local network caller. That was round-1's arbitrary-file-write finding. The
# web UI has no business choosing a destination path on this machine; the
# library path confirm() uses when `out` is None is always correct here. Do
# not add `out` back to this set -- if `confirm()` grows a new privileged
# kwarg, it must be added here explicitly too, never restored by
# re-introducing `**payload`.
CONFIRM_FIELDS = {"ref", "doi", "pick", "cite", "own_work", "adapted_from", "note"}
SKIP_FIELDS = {"ref"}
AUDIT_FIELDS = {"path"}
# "force" is allowed through: service.apply() enforces the actual invariant
# (refuse an existing `out` unless force=True; refuse a mismatched suffix).
# Never "allow_unconfirmed" -- service.apply() also refuses it, but the route
# must not even offer a path to try.
APPLY_FIELDS = {"path", "out", "force"}

# Round-2 finding M5. `ThreadingHTTPServer` hands two THREADS in one process
# the identical hazard the brief's `allow_reuse_address = False` guard exists
# to prevent between two SERVERS: "two [writers] writing one manifest is a
# corruption path." `store.put` is an unlocked append and `service._skipped`
# is an unsynchronized module global with a check-then-act read-modify-write
# in `service.skip`. Every mutating request is serialized behind this lock so
# that invariant is actually enforced, not merely asserted in a docstring.
_LOCK = threading.Lock()


def _fields(payload: dict, allowed: set) -> dict:
    """Wire fields -> kwargs, allowlisted -- and unknown fields are a 400.

    A route is a trust boundary. Splatting client JSON straight into a
    service function makes the wire contract equal to that function's whole
    signature, so every parameter it ever gains is exposed to the network --
    which is how `out=` became an arbitrary file write. Every mutating route
    below goes through this, even the ones that only ever named one or two
    fields, so no route in this file has a bare `**payload` shape for a
    future edit to reintroduce.

    Round-2 finding M1: earlier this silently dropped anything not in
    `allowed`. That is defensible for the security property alone, but it
    means the allowlist can never report being too narrow -- if the UI later
    sends a field this route doesn't know about yet, the server would answer
    200 and quietly ignore it. Raising means that gap fails loud instead.
    """
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    return dict(payload)


class _Handler(BaseHTTPRequestHandler):
    def _own_origins(self) -> set[str]:
        """Both loopback spellings, at this server's actual bound port.

        Round-2 finding I5: the round-1 `Origin` check only accepted
        `http://127.0.0.1:<port>`, so a user who typed `localhost:<port>`
        (a spelling browsers treat as equivalent, and the one people
        actually type) got 403 on every mutating request. `localhost` and
        `127.0.0.1` are the only two names this server is ever legitimately
        reached under -- both accepted, nothing else.
        """
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self) -> bool:
        """Round-2 finding I4: reject a forged `Host`.

        Without this, a page on a public domain that DNS-rebinds to
        127.0.0.1 is same-origin to the browser (its `Origin` header is
        still its own, so POSTs already 403 -- but nothing validated `Host`
        on GET). `/api/pending` leaks capture titles and process names;
        `/api/thumb` leaks the actual figure bytes. Applied to every
        request, GET included.
        """
        return self.headers.get("Host", "") in self._own_origins()

    def _same_origin(self) -> bool:
        """CSRF guard: refuse a POST carrying a foreign `Origin`.

        This is not authentication -- the spec is "no auth, single-user
        local" and that stands -- but a page the user merely visits can POST
        to 127.0.0.1 from JavaScript, and this server writes files. Browsers
        always attach `Origin` to a cross-origin POST; same-origin requests
        and non-browser clients (curl, this module's own `serve()` caller)
        may omit it entirely, so a missing `Origin` is allowed through.
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin in {f"http://{h}" for h in self._own_origins()}

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "unrecognized Host"}, 403)
        u = urlparse(self.path)
        if u.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/pending":
            items = [i.__dict__ for i in service.pending_items()]
            self._json({"items": items})
        elif u.path == "/api/thumb":
            ref = (parse_qs(u.query).get("ref") or [""])[0]
            try:
                blob, mime = service.thumbnail(ref)
            except service.LibraryFileMissing:
                # Ruling 10. The ref is real; its bytes are not. 410 rather than
                # 404, because "this never existed" and "this existed and its
                # file is gone" are different problems for whoever is debugging.
                return self._json({"error": "image file missing"}, 410)
            except KeyError:
                return self._json({"error": "unknown ref"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "unrecognized Host"}, 403)
        if not self._same_origin():
            return self._json({"error": "cross-origin request refused"}, 403)
        # Round-2 finding I1: body parsing used to sit OUTSIDE this try, so a
        # malformed request (bad Content-Length, unparsable JSON) dropped the
        # connection with no HTTP response at all -- strictly worse than the
        # bare 500 this handler otherwise exists to prevent. Everything that
        # can fail on untrusted input, including reading the body, now runs
        # inside the try. The whole dispatch is under the module lock (M5):
        # cheap to hold across one small JSON body, and it is what actually
        # serializes the manifest/skip-list writes below.
        with _LOCK:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n < 0 or n > MAX_BODY:
                    raise ValueError(f"invalid Content-Length: {n}")
                raw = self.rfile.read(n) if n else b"{}"
                payload = json.loads(raw or b"{}")
                if not isinstance(payload, dict):
                    # Round-2 finding M3: a JSON body that parses but isn't
                    # an object (e.g. `[1, 2]`) used to reach `payload.items()`
                    # / `payload["ref"]` and 500 with an AttributeError.
                    raise ValueError("request body must be a JSON object")

                u = urlparse(self.path)
                if u.path == "/api/confirm":
                    fields = _fields(payload, CONFIRM_FIELDS)
                    ref = fields.pop("ref")
                    res = service.confirm(ref, **fields)
                    self._json({"ok": True, "citation": res.record.display()})
                elif u.path == "/api/skip":
                    fields = _fields(payload, SKIP_FIELDS)
                    service.skip(fields["ref"])
                    self._json({"ok": True})
                elif u.path == "/api/audit":
                    fields = _fields(payload, AUDIT_FIELDS)
                    self._json(service.audit(fields["path"]))
                elif u.path == "/api/apply":
                    fields = _fields(payload, APPLY_FIELDS)
                    self._json(
                        service.apply(
                            fields["path"],
                            fields.get("out"),
                            force=bool(fields.get("force", False)),
                        )
                    )
                else:
                    self._json({"error": "not found"}, 404)
            except service.NotGrounded as e:
                self._json({"error": str(e), "kind": "not-grounded"}, 409)
            except (ValueError, KeyError) as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:  # fail loud, with the real reason
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def log_message(self, format, *args):  # noqa: A002 - matches base signature
        pass


class _Server(ThreadingHTTPServer):
    # Round-2 finding M4: setting this on `ThreadingHTTPServer` itself (the
    # imported stdlib class) mutates a class object shared by the whole
    # process -- any other `ThreadingHTTPServer` anywhere in this process
    # inherits it, and anything that later resets it to True silently voids
    # the port-refusal guarantee `test_it_refuses_a_port_already_in_use_
    # rather_than_moving` depends on. Scoping the override to a private
    # subclass makes it local to this module's servers only.
    allow_reuse_address = False  # a taken port must raise


def make_server(port: int) -> ThreadingHTTPServer:
    return _Server((HOST, port), _Handler)


def serve(port: int = 8765, open_browser: bool = False) -> None:
    srv = make_server(port)
    print(f"figcite ui: http://{HOST}:{srv.server_address[1]}")
    if open_browser:
        import webbrowser

        webbrowser.open(f"http://{HOST}:{srv.server_address[1]}")
    srv.serve_forever()
