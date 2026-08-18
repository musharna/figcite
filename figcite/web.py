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


class _Handler(BaseHTTPRequestHandler):
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
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        try:
            if u.path == "/api/confirm":
                res = service.confirm(payload.pop("ref"), **payload)
                self._json({"ok": True, "citation": res.record.display()})
            elif u.path == "/api/skip":
                service.skip(payload["ref"])
                self._json({"ok": True})
            elif u.path == "/api/audit":
                self._json(service.audit(payload["path"]))
            elif u.path == "/api/apply":
                self._json(service.apply(payload["path"], payload.get("out")))
            else:
                self._json({"error": "not found"}, 404)
        except service.NotGrounded as e:
            self._json({"error": str(e), "kind": "not-grounded"}, 409)
        except (ValueError, KeyError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:  # fail loud, with the real reason
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def log_message(self, *a):
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
