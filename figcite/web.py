"""A local, single-user web front end. Loopback only, no auth.

That is a deliberate choice for a tool that reads your figure library and
writes citations: it is reachable only from this machine, and adding auth to a
single-user localhost tool buys nothing it does not also cost in friction.
"""

from __future__ import annotations

import errno
import json
import sys
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
# One field, and still routed through `_fields`: the point of that helper is
# that no route in this file has a shape a later edit can widen by accident.
WHEREIS_FIELDS = {"ref"}

# Round-2 finding M5. `ThreadingHTTPServer` hands two THREADS in one process
# the identical hazard the brief's `allow_reuse_address = False` guard exists
# to prevent between two SERVERS: "two [writers] writing one manifest is a
# corruption path." `store.put` is an unlocked append and `service._skipped`
# is an unsynchronized module global with a check-then-act read-modify-write
# in `service.skip`. The service dispatch for each mutating route is
# serialized behind this lock so that invariant is actually enforced, not
# merely asserted in a docstring.
#
# Round-3 finding N1: a lock protects SHARED STATE, not the request
# lifecycle. This used to be held across `self.rfile.read(n)` -- unbounded,
# client-paced socket I/O -- and every response write. A client that sent a
# Content-Length promise and then went silent parked its handler thread
# inside that read while holding the lock, and every OTHER mutating request
# queued behind it: one slow client turned into a stall for the whole
# server, when before this lock existed a slow client only blocked its own
# thread. `do_POST` now takes `_LOCK` only around the service call itself;
# reading and parsing the body, and writing the response, happen outside it.
#
# Round-4 finding I1: that narrowing kept the lock off CLIENT-paced I/O but
# left it spanning SERVER-paced I/O. `/api/confirm` still held it across
# `service.confirm()`, whose CrossRef lookup can take ~65s (a 25s timeout
# plus a 429 retry that sleeps up to 15s and re-requests) -- longer than
# `_Handler.timeout`, so a queued client's socket could die before its turn
# came. The principle is the same one round 3 applied: this lock exists to
# serialize SHARED-STATE MUTATION, not slow work.
#
# So the confirm/audit/apply routes no longer take it. The invariant it was
# introduced (M5) to enforce did not move -- it moved DOWN, to the layer that
# owns the state: `service._WRITE_LOCK` covers the library write and the
# manifest append inside `confirm()`, with the network call outside it, so
# two concurrent confirms still cannot interleave their manifest writes.
# `audit()`/`apply()` only READ the manifest (see deck.py/pdfdeck.py, which
# call `store.all_records`/`find_similar` and never `store.put`), and
# `apply()` shelling out to Ghostscript under a global lock was the same
# defect one route over.
#
# `/api/skip` keeps it: `service.skip`'s check-then-act touches a process
# global and does no I/O at all, so serializing that route costs nothing and
# holds nothing slow.
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


def _confirm_payload(res) -> dict:
    """Everything the CLI prints after a confirm, as JSON.

    Round-4 finding I5. This route used to answer `{"ok": True, "citation":
    ...}` and nothing else, so the browser was SILENT about confirming a
    retracted paper -- while `figcite confirm` printed `** THIS WORK IS
    FLAGGED AS RETRACTED IN CROSSREF **` for the same DOI. An earlier ruling
    (6) restored those four lines to the CLI on the grounds that a
    destination path, a licence verdict, a retraction flag and the sha256
    that names which file to insert are correctness information, not
    decoration -- and created `ConfirmResult` carrying `.record` and `.path`
    so BOTH front ends could show them. Only one did.

    The deck screen does flag retraction, but only once the citation is
    already attached to a slide: that is a check the user runs later, if they
    run it, not a warning at the moment of the decision.

    Field-for-field this is `cli._print_filed` (figcite/cli.py): destination
    path, `Record.display()`, licence + reuse verdict, retraction, sha256.
    `path` is empty for a `filed:` ref, where the bytes were already in the
    library and nothing new was written -- the same case the CLI answers with
    "the image file itself is unchanged".
    """
    rec = res.record
    return {
        "ok": True,
        "citation": rec.display(),
        "retracted": bool(rec.retracted),
        "license_url": rec.license_url or "",
        "reuse": rec.reuse or "unknown",
        "path": str(res.path) if res.path else "",
        "sha256": rec.sha256 or "",
    }


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
        srv = self.server
        assert isinstance(srv, ThreadingHTTPServer)  # only ever mounted on one; narrows the type
        port = srv.server_port
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self) -> bool:
        """Round-2 finding I4: reject a forged `Host`.

        Without this, a page on a public domain that DNS-rebinds to
        127.0.0.1 is same-origin to the browser (its `Origin` header is
        still its own, so POSTs already 403 -- but nothing validated `Host`
        on GET). `/api/pending` leaks capture titles and process names;
        `/api/thumb` leaks the actual figure bytes. Applied to every
        request, GET included.

        Round-3 finding N2: `Host` header matching is case-insensitive per
        RFC 9110 -- a client sending `Host: LOCALHOST:<port>` is legitimate,
        not forged, and got an unnecessary 403 when this compared case-
        sensitively. `_own_origins()`'s two spellings are already lowercase,
        so lowering only the incoming header is enough.
        """
        return self.headers.get("Host", "").lower() in self._own_origins()

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
        # can fail on untrusted input still runs inside this try.
        #
        # Round-3 finding N1: reading the body used to happen under `_LOCK`
        # too. `self.rfile.read(n)` is unbounded, client-paced socket I/O --
        # a client that promised a Content-Length and then went silent
        # parked its thread there while holding the lock, stalling every
        # OTHER mutating request behind it. `_LOCK` now covers only the
        # service call for the matched route (the actual shared-state
        # mutation); reading/parsing the body and writing the response
        # happen outside it. `_fields()` touches no shared state, so it can
        # run outside the lock too.
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
                # No `_LOCK` here (round-4 I1): confirm() reaches CrossRef,
                # and it takes `service._WRITE_LOCK` itself around the part
                # that actually mutates the library and the manifest.
                res = service.confirm(ref, **fields)
                self._json(_confirm_payload(res))
            elif u.path == "/api/skip":
                fields = _fields(payload, SKIP_FIELDS)
                with _LOCK:
                    service.skip(fields["ref"])
                self._json({"ok": True})
            elif u.path == "/api/whereis":
                fields = _fields(payload, WHEREIS_FIELDS)
                # Read-only, like /api/audit: it reads the corpus index and
                # the query's own bytes and writes nothing, so it does not
                # take `_LOCK`. It can also run ORB over the whole corpus
                # (~9s measured at 1,214 figures when dhash declines), which
                # is exactly the slow-work-under-a-lock shape round-4 I1
                # removed from the confirm route.
                self._json(service.whereis(fields["ref"]))
            elif u.path == "/api/audit":
                fields = _fields(payload, AUDIT_FIELDS)
                report = service.audit(fields["path"])  # read-only; see _LOCK
                self._json(report)
            elif u.path == "/api/apply":
                fields = _fields(payload, APPLY_FIELDS)
                # Reads the manifest, writes only the caller's own output
                # file -- and can run Ghostscript to get there. See _LOCK.
                result = service.apply(
                    fields["path"],
                    fields.get("out"),
                    force=bool(fields.get("force", False)),
                )
                self._json(result)
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

    # Round-3 finding N1: without a timeout, `BaseHTTPRequestHandler`'s
    # socket operations (including our own `self.rfile.read(n)` above) block
    # for as long as a client holds the connection open and stays silent.
    # I2 already bounds how MUCH a client may claim to send; this bounds how
    # LONG any single request may take, independent of the lock change
    # above -- a silent client can no longer park a thread indefinitely even
    # if it isn't holding `_LOCK`. `StreamRequestHandler` (a base of
    # `BaseHTTPRequestHandler`) reads this class attribute and applies it as
    # the socket timeout for the whole request.
    timeout = 30


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


def serve(port: int = 8765, open_browser: bool = False) -> int:
    try:
        srv = make_server(port)
    except OSError as e:
        # Refusing a taken port is the point -- `allow_reuse_address = False`
        # above, because two servers writing one manifest is a corruption path.
        # Only the presentation changes here: this surfaced as an unhandled
        # traceback, and the commonest way to reach it is restarting the UI
        # within TIME_WAIT of stopping it, which resolves itself in under a
        # minute. Any other OSError still propagates -- a permission error is
        # not a busy port, and disguising it as one would be the fail-quiet
        # this project refuses.
        if e.errno != errno.EADDRINUSE:
            raise
        print(
            f"figcite ui: port {port} is already in use.\n"
            "  Another figcite ui may be running -- or, if you just stopped "
            "one, the\n"
            "  socket is still in TIME_WAIT and frees itself within about a "
            "minute.\n"
            "  figcite will not quietly move to another port: two servers "
            "writing one\n"
            "  manifest would corrupt it. Retry, or pass --port <n>.",
            file=sys.stderr,
        )
        return 2
    print(f"figcite ui: http://{HOST}:{srv.server_address[1]}")
    if open_browser:
        import webbrowser

        webbrowser.open(f"http://{HOST}:{srv.server_address[1]}")
    srv.serve_forever()
    return 0
