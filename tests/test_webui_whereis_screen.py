"""The browser could not ask the question the corpus exists to answer.

`service.whereis()` was built as the one entry point BOTH front ends call --
the design spec says so in as many words -- and then only `cli.py` ever called
it. Its sibling `service.duplicates()` does reach the browser (the deck row's
"also published under" badge), so the asymmetry was not a policy, just a stop.

The risk in putting it on screen is not that it fails to find things. It is
that `matches` mixes two different KINDS of answer: a pixel match, which is
evidence, and an open browser tab, which is a lead about what you happened to
be reading. The service already ranks tabs below pixels, but a list is a list
-- render them the same way and a "no match" carrying two tabs reads exactly
like a hit. That is this project's cardinal sin (an absence dressed as an
answer) wearing a different coat, so it gets the load-bearing test here.

These tests drive the real HTTP route and then run the page's OWN
`whereisHtml` under node against the exact bytes the server sent.
"""

import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

import pytest
from PIL import Image

from figcite import corpus, match, service, session_tabs, web
from figcite.webui import PAGE

TAB = {
    "source": "open-tab",
    "score": "",
    "doi": "10.1/tab-lead",
    "title": "Something you had open",
    "container": "example.org",
    "year": "",
    "type": "",
}


def _png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), "blue").save(path)
    return path


def _no_corpus(monkeypatch):
    """A corpus that is present but whose rows never need reading."""
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(corpus, "all_rows", lambda conn: [object()])


def _whereis_over_http(payload):
    """POST /api/whereis to a real (loopback) server and return the JSON."""
    srv = web.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/whereis",
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
    """The HTML `whereisHtml(response)` returns, from the shipped page."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment-dependent
        pytest.skip("node is not installed; cannot execute the page's renderer")
    esc = re.search(r"const esc = .*?;\s*\n", PAGE, re.S)
    assert esc, "PAGE no longer defines `esc`"
    script = tmp_path / "render_whereis.js"
    script.write_text(
        esc.group(0)
        + "\n"
        + _js_source("whereisHtml")
        + "\nprocess.stdout.write(whereisHtml(JSON.parse(process.argv[2])));\n",
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


# --- the route ------------------------------------------------------------


def test_the_route_answers_a_pixel_match(tmp_path, monkeypatch):
    _no_corpus(monkeypatch)
    monkeypatch.setattr(
        match,
        "by_dhash",
        lambda b, rows: match.Match("10.1/found", "PMC9", "Figure 3", "dhash", 0.0, 9.0),
    )
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    status, body = _whereis_over_http({"ref": str(_png(tmp_path / "q.png"))})

    assert status == 200, body
    assert body["verdict"] == "match", body
    assert body["matches"][0]["doi"] == "10.1/found", body


def test_the_route_refuses_a_field_it_does_not_know(tmp_path, monkeypatch):
    """Every other route goes through `_fields`; this one must not be the
    exception that reintroduces the bare-`**payload` shape."""
    _no_corpus(monkeypatch)
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    status, body = _whereis_over_http({"ref": str(_png(tmp_path / "q.png")), "out": "/etc/passwd"})

    assert status == 400, body
    assert "out" in body["error"], body


def test_a_missing_file_is_an_error_not_an_empty_answer(tmp_path, monkeypatch):
    """ "I could not read that" must not arrive as "nothing matched".

    Written first as `status >= 400 and "error" in body`, which PASSED
    against a server that had no such route at all -- a 404 "not found"
    satisfies both halves. That version could not fail for the reason it
    names. Two changes fix it: pin the status a bad ref actually produces
    (`KeyError` -> 400, not the router's 404), and assert the message is
    about the ref rather than about routing.
    """
    _no_corpus(monkeypatch)
    monkeypatch.setattr(
        match,
        "by_dhash",
        lambda b, rows: match.Match("10.1/found", "PMC9", "Figure 3", "dhash", 0.0, 9.0),
    )
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    status, body = _whereis_over_http({"ref": str(tmp_path / "not-there.png")})

    assert status == 400, body
    assert body.get("error"), body
    assert body["error"] != "not found", "that is the router's 404, not a ref error"

    # The positive control: the identical request shape, with a file that
    # exists, is answered. Without it the assertions above pass just as well
    # on a server that refuses every request it is handed.
    ok_status, ok_body = _whereis_over_http({"ref": str(_png(tmp_path / "real.png"))})
    assert ok_status == 200, ok_body
    assert ok_body["verdict"] == "match", ok_body


# --- the three outcomes, through the page's own renderer ------------------


def test_a_pixel_match_renders_as_evidence(tmp_path, monkeypatch):
    _no_corpus(monkeypatch)
    monkeypatch.setattr(
        match,
        "by_dhash",
        lambda b, rows: match.Match("10.1/pixel", "PMC9", "Figure 1", "dhash", 0.0, 9.0),
    )
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])
    status, body = _whereis_over_http({"ref": str(_png(tmp_path / "q.png"))})
    assert status == 200, body

    html = _render(tmp_path, body)

    assert "10.1/pixel" in html, html
    assert "dhash" in html, html


def test_a_no_match_with_open_tabs_does_not_render_as_a_match(tmp_path, monkeypatch):
    """THE finding this screen exists to avoid.

    The corpus was searched and answered NO. Two browser tabs happen to be
    open. Those tabs are in `matches` -- the service puts them there on
    purpose, ranked last -- so a renderer that just lists `matches` shows two
    DOIs under a heading the user reads as "here is where your figure came
    from". The searched-and-found-nothing verdict has to survive contact with
    a non-empty list, and the tab DOI must never be labelled as a finding.
    """
    _no_corpus(monkeypatch)
    monkeypatch.setattr(
        match,
        "by_dhash",
        lambda b, rows: match.CouldNotDecide("a crop is invisible to dhash"),
    )
    monkeypatch.setattr(
        match,
        "by_orb",
        lambda b, rows, root, descriptor_dir=None: match.NoMatch(),
    )
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [dict(TAB)])

    status, body = _whereis_over_http({"ref": str(_png(tmp_path / "q.png"))})
    assert status == 200, body
    # The premise: the service really did hand the page a non-empty list
    # alongside a no-match verdict. Without this the test could pass on a
    # response that never contained a tab at all.
    assert body["verdict"] == "no-match", body
    assert body["matches"], "premise: a tab lead was returned with the no-match"

    html = _render(tmp_path, body)

    assert "not in your corpus" in html, html
    # The tab may be shown -- it is a real lead -- but never as the answer.
    assert "10.1/tab-lead" in html, html
    lead_idx = html.index("10.1/tab-lead")
    assert "lead" in html[:lead_idx].lower(), (
        "the tab DOI is rendered with nothing before it marking it a lead: " + html
    )


def test_could_not_decide_renders_differently_from_no_match(tmp_path, monkeypatch):
    """ "I looked and it is not there" and "I could not look" license
    different next actions, so they must not render the same."""
    _no_corpus(monkeypatch)
    monkeypatch.setattr(match, "by_dhash", lambda b, rows: match.CouldNotDecide("too smooth"))
    monkeypatch.setattr(
        match,
        "by_orb",
        lambda b, rows, root, descriptor_dir=None: match.CouldNotDecide("too smooth"),
    )
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    status, undecided = _whereis_over_http({"ref": str(_png(tmp_path / "q.png"))})
    assert status == 200, undecided
    assert undecided["verdict"] == "could-not-decide", undecided

    monkeypatch.setattr(
        match,
        "by_dhash",
        lambda b, rows: match.CouldNotDecide("a crop is invisible to dhash"),
    )
    monkeypatch.setattr(
        match,
        "by_orb",
        lambda b, rows, root, descriptor_dir=None: match.NoMatch(),
    )
    status, absent = _whereis_over_http({"ref": str(_png(tmp_path / "q2.png"))})
    assert status == 200, absent
    assert absent["verdict"] == "no-match", absent

    undecided_html = _render(tmp_path, undecided)
    absent_html = _render(tmp_path, absent)

    assert undecided_html != absent_html, "the two outcomes render identically"
    # And the undecided one has to say WHY, or it is just a no-match with
    # softer wording.
    assert "too smooth" in undecided_html, undecided_html
    assert "not in your corpus" in absent_html, absent_html
    assert "not in your corpus" not in undecided_html, undecided_html


def test_an_empty_corpus_says_so_rather_than_reporting_no_match(tmp_path, monkeypatch):
    """A positive control for the pair above: the third outcome has a third
    cause, and 'you have not built it yet' is not 'your figure is not in it'."""
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(corpus, "all_rows", lambda conn: [])
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    status, body = _whereis_over_http({"ref": str(_png(tmp_path / "q.png"))})

    assert status == 200, body
    assert body["verdict"] == "could-not-decide", body
    html = _render(tmp_path, body)
    assert "not in your corpus" not in html, html
    assert "corpus is empty" in html, html


# --- the wire contract ----------------------------------------------------


def test_every_key_the_whereis_renderer_reads_is_supplied_by_the_service(tmp_path, monkeypatch):
    """`tests/test_candidate_contract.py`, for the other producer.

    That test pins the keys the PENDING card reads off a candidate. This
    screen reads off a DIFFERENT producer (`service.whereis`), so it needs
    its own check or the page can grow a `m.foo` that is always undefined.
    """
    read_keys = set(re.findall(r"\bm\.(\w+)", _js_source("whereisHtml")))
    assert read_keys, "whereisHtml reads no candidate fields at all"

    _no_corpus(monkeypatch)
    monkeypatch.setattr(
        match,
        "by_dhash",
        lambda b, rows: match.Match("10.1/pixel", "PMC9", "Figure 1", "dhash", 0.0, 9.0),
    )
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [dict(TAB)])

    out = service.whereis(str(_png(tmp_path / "q.png")))
    assert out["matches"], "premise: the service returned candidates to check"
    for cand in out["matches"]:
        missing = read_keys - set(cand)
        assert not missing, f"{sorted(missing)} read by the page, absent from {cand}"


def test_the_service_marks_which_candidates_are_evidence(tmp_path, monkeypatch):
    """The page must not have to guess from a source-name allowlist.

    `source in {"dhash","orb"}` is a list of names standing in for an open
    set: add a third matcher and every one of its hits silently demotes to a
    lead. The producer knows which is which, so it says so.
    """
    _no_corpus(monkeypatch)
    monkeypatch.setattr(
        match,
        "by_dhash",
        lambda b, rows: match.Match("10.1/pixel", "PMC9", "Figure 1", "dhash", 0.0, 9.0),
    )
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [dict(TAB)])

    out = service.whereis(str(_png(tmp_path / "q.png")))

    by_doi = {m["doi"]: m for m in out["matches"]}
    assert by_doi["10.1/pixel"]["evidence"] is True, out
    assert by_doi["10.1/tab-lead"]["evidence"] is False, out


def test_a_score_of_zero_is_shown_rather_than_swallowed(tmp_path, monkeypatch):
    """Found by running the real corpus through the real route.

    A dhash score is a HAMMING DISTANCE, so 0 means the query and the indexed
    figure hash identically -- the strongest answer this screen can give. The
    renderer said `esc(m.score || "")`, and `0 || ""` is the empty string, so
    the best match in the corpus displayed a blank score. Every test here
    passed: they all asserted on the DOI, and the pending card next door uses
    the same `|| ""` idiom, where the score is a string and the bug is
    invisible.
    """
    _no_corpus(monkeypatch)
    monkeypatch.setattr(
        match,
        "by_dhash",
        lambda b, rows: match.Match("10.1/exact", "PMC9", "Figure 1", "dhash", 0.0, 9.0),
    )
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    status, body = _whereis_over_http({"ref": str(_png(tmp_path / "q.png"))})
    assert status == 200, body
    assert body["matches"][0]["score"] == 0, "premise: the score really is zero"

    html = _render(tmp_path, body)

    assert ">0<" in html, f"a zero score rendered as blank: {html}"
