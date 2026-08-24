"""Default arguments, and the lists of names that ARE the logic.

I classified 1,060 mutants as CALLARG or COSMETIC and called them the noise
floor without reading them. Measuring instead of asserting: 57 of them are
either a DEFAULT ARGUMENT VALUE or a MEMBERSHIP LIST being iterated. Neither
is decoration.

The tier classifier put them there because it asks what the mutated node's
nearest enclosing construct is, and for both of these the answer is a Call or
a function signature -- never a guard. But a default argument is what the
tool does when called plainly, which is how it is almost always called; and a
tuple of field names being iterated is a dispatch table.

So the tier was right about the shape and wrong about the consequence, and
"noise floor" was a claim I had not earned.
"""

from __future__ import annotations

import inspect

import pytest
from PIL import Image

from figcite import (
    browser,
    clipboard,
    crossref,
    deck,
    mplhook,
    pdfdeck,
    pdfgrab,
    provenance,
    service,
    store,
    web,
    zotero,
)


def _default(fn, name):
    return inspect.signature(fn).parameters[name].default


# ------------------------------------------------- the perceptual constants


def test_the_dhash_grid_is_eight_by_eight():
    """`def dhash_bytes(b, size: int = 8)` -- `8 -> 9` survived.

    The grid size decides the WIDTH of every hash: 8 gives 64 bits and a
    16-character hex string, which is what `hamming` requires both sides to
    share and what every row already in a manifest carries. Changing it does
    not degrade matching, it ENDS it -- `hamming` returns its 999 sentinel for
    any pair of differing widths, so every stored hash becomes uncomparable
    at once and the corpus silently matches nothing.
    """
    assert _default(provenance.dhash_bytes, "size") == 8

    flat = Image.new("RGB", (32, 32), (10, 20, 30))
    import io

    b = io.BytesIO()
    flat.save(b, "PNG")
    assert len(provenance.dhash_bytes(b.getvalue())) == 16, (
        "the hash width changed; every hash in every existing manifest becomes "
        "uncomparable"
    )


@pytest.mark.parametrize(
    "fn,param",
    [
        (store.find_similar, "max_distance"),
        (deck.match_picture, "fuzzy_distance"),
        (pdfdeck.match_image, "fuzzy_distance"),
    ],
    ids=["store", "deck", "pdfdeck"],
)
def test_the_perceptual_distance_default_matches_the_matcher(fn, param):
    """Three independent copies of the same tuning constant, each a default
    argument, each survived `6 -> 7`.

    They must agree with `match.DHASH_THRESHOLD` or the same figure is a
    duplicate in one surface and not in another -- and because they are
    defaults, nothing in the codebase passes them explicitly, so a drift here
    is invisible until two screens disagree in front of a user.
    """
    from figcite.match import DHASH_THRESHOLD

    assert _default(fn, param) == DHASH_THRESHOLD, (
        f"{fn.__name__}({param}=) has drifted from match.DHASH_THRESHOLD"
    )


# ------------------------------------------------- what a plain call does


@pytest.mark.parametrize(
    "fn,param,expected,why",
    [
        (pdfdeck.apply, "captions", True, "a plain apply writes captions"),
        (pdfdeck.apply, "credits", True, "a plain apply writes a credits page"),
        (deck.apply, "captions", True, "a plain apply writes captions"),
        (
            browser.url_to_doi,
            "allow_fetch",
            True,
            "a plain lookup may read the page's own citation_doi",
        ),
        (
            crossref.fetch_work,
            "use_cache",
            True,
            "a plain lookup uses the cache rather than re-requesting",
        ),
        (mplhook.install, "strict", True, "provenance failures fail loudly"),
        (
            clipboard.watch,
            "resolve",
            True,
            "a plain watch infers a source rather than only staging bytes",
        ),
        (
            service.apply,
            "force",
            False,
            "a plain apply does not overwrite without being asked",
        ),
        (
            web.serve,
            "open_browser",
            False,
            "serving does not seize the user's browser",
        ),
        (
            pdfgrab.crop,
            "frac",
            False,
            "a rect is in points unless the caller says otherwise",
        ),
    ],
)
def test_the_default_that_decides_what_a_plain_call_does(fn, param, expected, why):
    """Ten boolean defaults, each survived `True -> False` or the reverse.

    These are not decoration: almost every call in the CLI and the web UI
    takes them as given, so flipping one changes what the tool does for
    nearly every user while every existing test -- which passes the value
    explicitly, or does not reach the branch -- goes on passing.
    """
    assert _default(fn, param) is expected, why


def test_the_default_serve_port_is_the_one_the_docs_and_the_ui_use():
    """`def serve(port: int = 8765)` survived `8765 -> 8766`.

    The port is in the README, in the loopback-exclusion tests, and in every
    bookmark a user has made. A silent move is a tool that "stops working"
    with no error anywhere.
    """
    assert _default(web.serve, "port") == 8765


def test_a_figure_made_by_the_user_is_credited_to_this_work_by_default():
    """`cite: str = "This work"` survived being mutated.

    `mplhook` files every figure a plotting script writes. The default
    citation is what lands in the manifest when the user did not name one --
    and "This work" is what `bibtex` and the deck writers key their own-work
    handling on.
    """
    assert _default(mplhook.install, "cite") == "This work"


# ------------------------------------------------- the lists that ARE logic


def test_the_history_snapshot_copies_the_write_ahead_log():
    """`for ext in ("-wal", "-shm")` -- both entries survived being mutated.

    The module docstring states the consequence outright: "Firefox holds the
    DB open; without the -wal the newest visits -- exactly the ones a
    just-taken snip needs -- are missing." So the tuple is not a list of
    files to be tidy about; the FIRST entry is the whole reason history
    grounding works at all for a page you are looking at right now.
    """
    src = inspect.getsource(browser.snapshot_history)
    assert '"-wal"' in src and '"-shm"' in src

    # And behaviourally: both siblings are copied when present.
    import tempfile
    from pathlib import Path

    profile = Path(tempfile.mkdtemp())
    (profile / "places.sqlite").write_bytes(b"SQLite format 3\x00")
    (profile / "places.sqlite-wal").write_bytes(b"wal-bytes")
    (profile / "places.sqlite-shm").write_bytes(b"shm-bytes")

    got = browser.snapshot_history(profile)

    assert got is not None
    assert (got.parent / "places.sqlite-wal").read_bytes() == b"wal-bytes", (
        "the write-ahead log was not copied, so the newest visits are invisible"
    )
    assert (got.parent / "places.sqlite-shm").exists()


def test_a_year_is_read_from_whichever_crossref_date_field_carries_it():
    """`for k in ("issued", "published-print", "published-online", "created")`
    -- all four entries survived.

    CrossRef records do not all carry the same date field, and the order is
    a preference, not a formality. Each entry is driven ALONE: a record
    carrying only that field must still yield its year, which is the case a
    combined fixture cannot observe.
    """
    for key in ("issued", "published-print", "published-online", "created"):
        msg = {key: {"date-parts": [[2019]]}}
        assert crossref._year(msg) == 2019, (
            f"a record whose only date field is {key!r} lost its year"
        )

    assert crossref._year({"some-other-field": {"date-parts": [[2019]]}}) is None


def test_alt_text_is_found_on_every_shape_container_including_groups():
    """`for attr in ("_nvXxPr", "nvPicPr", "nvSpPr", "nvGrpSpPr")` -- all four
    survived.

    Each name is a different kind of pptx shape, and `nvGrpSpPr` is the
    GROUPED one -- pictures inside a group are exactly the ones a designer
    produces and the ones this project already has a test file for. Dropping
    a name does not error; the shape simply reports no alt text and the audit
    calls a credited figure unsourced.
    """
    for attr in ("_nvXxPr", "nvPicPr", "nvSpPr", "nvGrpSpPr"):

        class _Cnv:
            pass

        el = type("_El", (), {attr: type("_Nv", (), {"cNvPr": _Cnv()})()})()
        shape = type("_Shape", (), {"_element": el})()

        assert deck._cnvpr(shape) is not None, (
            f"a shape whose container is {attr} was treated as having none"
        )


def test_a_doi_is_looked_for_in_every_pdf_metadata_field(monkeypatch):
    """`for k in ("doi", "subject", "keywords", "title", "producer")` -- three
    of the five entries survived.

    Publishers put the DOI in different fields and none is reliable, which is
    why there is a list at all. Each entry is driven ALONE.

    The document is supplied rather than written: PyMuPDF's `set_metadata`
    refuses a non-standard key, so a real PDF cannot carry a "doi" metadata
    field written by this test -- but a PDF in the wild can, and the code
    reads it. Stubbing the metadata is the only way to reach that entry.
    """

    class _FakeDoc:
        def __init__(self, meta):
            self.metadata = meta
            self.page_count = 0

        def xref_xml_metadata(self):
            return ""

        def __getitem__(self, i):
            raise IndexError

        def close(self):
            pass

    for key in ("doi", "subject", "keywords", "title", "producer"):
        monkeypatch.setattr(
            pdfgrab.fitz, "open", lambda p, k=key: _FakeDoc({k: "10.1111/nph.71477"})
        )

        got, how = pdfgrab.discover_doi("ignored.pdf")

        assert got == "10.1111/nph.71477", (
            f"a DOI recorded only in the {key!r} metadata field was not found ({how})"
        )
        assert key in how, f"the report named {how!r} rather than the {key!r} field"

    monkeypatch.setattr(pdfgrab.fitz, "open", lambda p: _FakeDoc({"author": "nobody"}))
    got, how = pdfgrab.discover_doi("ignored.pdf")
    assert got is None, (got, how)


@pytest.mark.parametrize(
    "mod,fn", [(clipboard, "auto_finalize"), (service, "_clear_staged")]
)
def test_both_capture_sidecars_are_cleaned_up(mod, fn, tmp_path, monkeypatch):
    """`for suffix in (".pending.json", ".capture.json")` -- both entries
    survived, in BOTH of the two places this loop appears.

    They hold the inference and the capture context. Leaving either behind
    means the next scan of the staging directory finds an orphan describing
    a PNG that has already been filed, and re-offers it.
    """
    png = tmp_path / "clip-9.png"
    Image.new("RGB", (8, 8), "white").save(png)
    pending = tmp_path / "clip-9.pending.json"
    capture = tmp_path / "clip-9.capture.json"
    pending.write_text("{}")
    capture.write_text("{}")

    if fn == "_clear_staged":
        service._clear_staged(png, tmp_path / "elsewhere.png")
    else:
        monkeypatch.setattr(store, "MANIFEST", tmp_path / "m.jsonl")
        monkeypatch.setattr(store, "DATA_DIR", tmp_path)
        monkeypatch.setattr(
            store, "finalize_into_library", lambda src, rec: tmp_path / "filed.png"
        )
        Image.new("RGB", (8, 8), "white").save(tmp_path / "filed.png")
        clipboard.auto_finalize(
            png,
            {
                "png": str(png),
                "capture": {},
                "inference": {"grounded": False, "doi": "", "candidates": []},
            },
        )

    assert not pending.exists(), ".pending.json was left behind"
    assert not capture.exists(), ".capture.json was left behind"


def test_zotero_creators_are_read_from_the_creators_key(monkeypatch):
    """`for c in (d.get("creators") or [])` -- the key name survived.

    Under the mutant every item comes back with an empty creator list, so
    every short cite degrades to a bare year: "2020" as the credit under a
    figure. The item shape here is Zotero's own -- `lastName` for a person,
    `name` for an institution -- and both branches are driven, since the
    fallback chain is on the same line.

    My first version of this test called a helper that does not exist and
    skipped itself rather than failing. A test that always skips measures
    nothing and reads as coverage in a summary line.
    """
    raw = [
        {
            "data": {
                "key": "K1",
                "title": "A paper",
                "DOI": "10.1/x",
                "itemType": "journalArticle",
                "date": "2020",
                "creators": [
                    {"lastName": "Smith", "creatorType": "author"},
                    {"name": "The Pollen Consortium", "creatorType": "author"},
                ],
            }
        }
    ]
    monkeypatch.setattr(zotero, "_get", lambda path, key, **params: _FakeResp(raw))
    monkeypatch.setattr(zotero, "credentials", lambda: ("k", "6532713", "group"))

    items = zotero.fetch_library()

    assert items and items[0]["creators"] == ["Smith", "The Pollen Consortium"], (
        f"creators were lost between Zotero's shape and ours: {items}"
    )


class _FakeResp:
    """One page of Zotero results, then no more."""

    def __init__(self, payload):
        self._payload = payload
        self.headers: dict = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def test_the_context_line_names_the_process_the_title_and_the_time():
    """`(cap.get("process"), cap.get("title"), cap.get("when"))` -- all three
    keys survived.

    A capture that grounds nothing is still filed, and this line is the whole
    of what it records: which app was in front, what the window said, and
    when. Each key alone, because a fixture supplying all three cannot
    observe which one went missing.
    """
    for key, value in (
        ("process", "firefox.exe"),
        ("title", "A paper about pollen"),
        ("when", "2026-08-23T10:00:00"),
    ):
        line = service._context_of({key: value})
        assert value in line, (
            f"the capture's {key!r} is missing from the context line: {line!r}"
        )


def test_a_hand_entered_doi_is_recorded_as_manual(monkeypatch):
    """`source_kind: str = "manual"` survived being mutated.

    Nothing in the codebase BRANCHES on "manual" -- `source_kind` is compared
    against "clipboard" and "generated" only -- so the mutant changes no
    behaviour, and a test that drove behaviour could never catch it. What it
    changes is the value written into the manifest and into every sidecar for
    a hand-entered DOI, which is a data format a user reads and other tools
    parse. Pinned for the same reason as the sidecar suffix: self-consistent
    code notices nothing, and the artefacts on disk are the contract.
    """
    monkeypatch.setattr(
        crossref,
        "fetch_work",
        lambda doi, **kw: {
            "DOI": doi,
            "title": ["A paper"],
            "author": [{"family": "Smith", "given": "A"}],
            "issued": {"date-parts": [[2020]]},
        },
    )

    rec = crossref.record_from_doi("10.1/x", confirmed=True)

    assert rec.source_kind == "manual", (
        f"a hand-entered DOI was filed as {rec.source_kind!r}"
    )
