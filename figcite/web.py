"""A local, single-user web front end. Loopback only, no auth.

That is a deliberate choice for a tool that reads your figure library and
writes citations: it is reachable only from this machine, and adding auth to a
single-user localhost tool buys nothing it does not also cost in friction.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import service
from .webui import PAGE  # stub created in this task, filled in Task 7

HOST = "127.0.0.1"

# Wire fields each mutating route accepts. `out` is deliberately absent from
# CONFIRM_FIELDS: `service.confirm(ref, out=...)` is a real CLI capability
# (`figcite confirm -o/--out`), but a route is a trust boundary, and a client
# JSON body is untrusted input. Splatting the whole payload into confirm()
# made the wire contract equal to confirm()'s full signature, so `out=` --
# a filesystem destination -- became settable by any local network caller.
# That was round-1's arbitrary-file-write finding. The web UI has no business
# choosing a destination path on this machine; the library path confirm()
# uses when `out` is None is always correct here. Do not add `out` back to
# this set -- if `confirm()` grows a new privileged kwarg, it must be added
# here explicitly too, never restored by re-introducing `**payload`.
CONFIRM_FIELDS = {"doi", "pick", "cite", "own_work", "adapted_from", "note"}
SKIP_FIELDS = {"ref"}
AUDIT_FIELDS = {"path"}
APPLY_FIELDS = {"path", "out"}  # never "allow_unconfirmed" -- service.apply()
# also refuses it, but the route must not even offer a path to try.


def _fields(payload: dict, allowed: set) -> dict:
    """Wire fields -> kwargs, allowlisted.

    A route is a trust boundary. Splatting client JSON straight into a
    service function makes the wire contract equal to that function's whole
    signature, so every parameter it ever gains is exposed to the network --
    which is how `out=` became an arbitrary file write. Every mutating route
    below goes through this, even the ones that only ever named one or two
    fields, so no route in this file has a bare `**payload` shape for a
    future edit to reintroduce.
    """
    return {k: v for k, v in payload.items() if k in allowed}


class _Handler(BaseHTTPRequestHandler):
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
        return origin == f"http://{HOST}:{self.server.server_address[1]}"

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
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
        if not self._same_origin():
            return self._json({"error": "cross-origin request refused"}, 403)
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        try:
            if u.path == "/api/confirm":
                fields = _fields(payload, CONFIRM_FIELDS)
                res = service.confirm(payload["ref"], **fields)
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
                self._json(service.apply(fields["path"], fields.get("out")))
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


def make_server(port: int) -> ThreadingHTTPServer:
    ThreadingHTTPServer.allow_reuse_address = False  # a taken port must raise
    return ThreadingHTTPServer((HOST, port), _Handler)


def serve(port: int = 8765, open_browser: bool = False) -> None:
    srv = make_server(port)
    print(f"figcite ui: http://{HOST}:{srv.server_address[1]}")
    if open_browser:
        import webbrowser

        webbrowser.open(f"http://{HOST}:{srv.server_address[1]}")
    srv.serve_forever()
