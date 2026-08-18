"""Round-4 finding I5: the browser never said the paper was retracted.

`ConfirmResult` was created (ruling 6) carrying `.record` and `.path` so BOTH
front ends could show what a confirm produced -- destination path, licence
verdict, retraction flag, and the sha256 that names which file to insert into
the deck. The CLI prints all four (`cli._print_filed`). `/api/confirm`
returned `{"ok": True, "citation": ...}` and `webui.py` discarded even that,
so confirming a RETRACTED DOI in the browser was completely silent. The deck
screen flags retraction, but only after the citation is already on a slide.

These tests drive the real HTTP route with CrossRef stubbed to a retracted
work, then run the page's OWN `confirmedHtml` under node against the exact
response the server sent -- not a pattern-match over PAGE's source text, and
not a re-implementation of the renderer in the test.
"""

import json
import re
import shutil
import subprocess
import threading

import pytest
from PIL import Image

from figcite import crossref, web
from figcite.webui import PAGE

RETRACTED_MESSAGE = {
    "DOI": "10.1234/retracted-work",
    "title": ["A paper that was later retracted"],
    "author": [{"family": "Doe", "given": "J."}],
    "issued": {"date-parts": [[2020]]},
    "container-title": ["Journal of Oops"],
    "update-to": [{"type": "retraction", "DOI": "10.1234/the-retraction"}],
    "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
}


class _StubResponse:
    status_code = 200

    def __init__(self, message):
        self._message = message

    def raise_for_status(self):
        return None

    def json(self):
        return {"message": self._message}


def _stage(tmp_path, monkeypatch, name):
    from figcite import service

    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    png = staging / f"{name}.png"
    Image.new("RGB", (4, 4), (200, 40, 40)).save(png)
    (staging / f"{name}.pending.json").write_text(
        json.dumps(
            {
                "png": str(png),
                "capture": {"process": "firefox", "title": "A retracted paper"},
                "inference": {},
            }
        )
    )
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))
    return png


def _confirm_over_http(payload):
    """POST /api/confirm to a real (loopback) server and return the JSON."""
    import urllib.error
    import urllib.request

    srv = web.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/confirm",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    finally:
        srv.shutdown()


# --- running the page's own renderer, rather than reading its source -------


def _js_source(name: str) -> str:
    """`function <name>(...) {...}` lifted out of PAGE by brace matching."""
    start = PAGE.index(f"function {name}(")
    depth, seen = 0, False
    for i in range(start, len(PAGE)):
        if PAGE[i] == "{":
            depth, seen = depth + 1, True
        elif PAGE[i] == "}":
            depth -= 1
            if seen and depth == 0:
                return PAGE[start : i + 1]
    raise AssertionError(f"unterminated function {name} in PAGE")


def _render(tmp_path, response: dict) -> str:
    """The HTML `confirmedHtml(response)` returns, from the shipped page.

    `confirmedHtml` is pure (JSON in, HTML string out) precisely so this can
    execute the real thing: a test that greps PAGE for "RETRACTED" passes on
    a page where the banner is built but never reached.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment-dependent
        pytest.skip("node is not installed; cannot execute the page's renderer")
    # `;\n`, not `;`: esc's own body contains `"&amp;"`, so stopping at the
    # first semicolon cuts the statement in half and node rejects the file.
    esc = re.search(r"const esc = .*?;\s*\n", PAGE, re.S)
    assert esc, "PAGE no longer defines `esc`"
    script = tmp_path / "render.js"
    script.write_text(
        esc.group(0)
        + "\n"
        + _js_source("confirmedHtml")
        + "\nprocess.stdout.write(confirmedHtml(JSON.parse(process.argv[2])));\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        [node, str(script), json.dumps(response)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return out.stdout


# --- the finding ----------------------------------------------------------


def test_confirming_a_retracted_doi_is_reported_by_the_route(tmp_path, monkeypatch):
    """The response must carry the retraction, the licence verdict, the
    destination path and the sha256 -- the same four facts `figcite confirm`
    prints. `citation` alone is what this route used to answer, and a
    retracted paper's citation looks exactly like any other.
    """
    _stage(tmp_path, monkeypatch, "clip-retracted")
    monkeypatch.setattr(
        crossref, "throttled_get", lambda url, **kw: _StubResponse(RETRACTED_MESSAGE)
    )

    status, body = _confirm_over_http(
        {"ref": "staged:clip-retracted.png", "doi": "10.1234/retracted-work"}
    )

    assert status == 200, body
    assert body["retracted"] is True, body
    assert body["license_url"] == "https://creativecommons.org/licenses/by/4.0/", body
    assert body["reuse"] == "reuse-ok-attribution-required", body
    assert body["path"].endswith(".png"), body
    assert len(body["sha256"]) == 64, body


def test_the_page_renders_a_retracted_confirm_as_a_warning(tmp_path, monkeypatch):
    """The response above, through the page's own renderer.

    "Unmissable" is asserted as structure, not vibes: its own block-level
    element, its own warn-coloured class (defined in PAGE's stylesheet), and
    role=alert, carrying the CLI's exact wording.
    """
    _stage(tmp_path, monkeypatch, "clip-retracted-render")
    monkeypatch.setattr(
        crossref, "throttled_get", lambda url, **kw: _StubResponse(RETRACTED_MESSAGE)
    )
    status, body = _confirm_over_http(
        {"ref": "staged:clip-retracted-render.png", "doi": "10.1234/retracted-work"}
    )
    assert status == 200, body

    html = _render(tmp_path, body)

    assert "THIS WORK IS FLAGGED AS RETRACTED IN CROSSREF" in html, html
    assert 'class="retracted-banner"' in html, html
    assert 'role="alert"' in html, html
    # The class has to actually mean something -- a banner styled by a rule
    # that does not exist is small print with extra steps.
    assert ".retracted-banner {" in PAGE
    assert "--warnbg" in PAGE and "--warnfg" in PAGE
    # ...and the rest of ruling 6's four lines came through too.
    assert body["path"] in html, html
    assert body["sha256"][:16] in html, html
    assert "insert THIS file into your deck" in html, html
    assert "reuse-ok-attribution-required" in html, html


def test_a_clean_confirm_renders_no_retraction_banner(tmp_path, monkeypatch):
    """Positive control. Without it, a renderer that shouted RETRACTED at
    every confirm would pass the test above -- and a warning that fires on
    everything is a warning nobody reads.
    """
    _stage(tmp_path, monkeypatch, "clip-clean")

    status, body = _confirm_over_http(
        {"ref": "staged:clip-clean.png", "cite": "Clean Control 2026"}
    )
    assert status == 200, body
    assert body["retracted"] is False, body

    html = _render(tmp_path, body)

    assert "RETRACTED" not in html, html
    assert "retracted-banner" not in html, html
    # Still shows the rest: the block is the confirm receipt, not a
    # retraction-only banner.
    assert "Clean Control 2026" in html, html
    assert body["path"] in html, html
    assert "not stated by publisher" in html, html


def test_both_confirm_buttons_render_the_result(tmp_path):
    """The Confirm button and "This is my own work" are two front doors to
    the same route; a result rendered on only one of them is the same
    silence, halved. Checked in PAGE's source because this is about the
    CALL SITES, not the renderer -- `confirmedHtml` above is what gets
    executed.
    """
    calls = [m.start() for m in re.finditer(r'post\("/api/confirm"', PAGE)]
    assert len(calls) == 2, f"expected two /api/confirm call sites, got {len(calls)}"
    for i in calls:
        assert PAGE[max(0, i - 22) : i].endswith("renderConfirmed(await "), (
            f"confirm result discarded at: ...{PAGE[max(0, i - 60) : i + 60]}..."
        )
