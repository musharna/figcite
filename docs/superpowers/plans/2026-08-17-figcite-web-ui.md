# figcite web UI + service layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give figcite a local web UI for the two decisions that require looking at a picture — resolving a pending capture, and auditing a deck — without letting the UI and CLI diverge on what "confirm" means.

**Architecture:** A new `figcite/service.py` becomes the single source of truth for actions; `cli.py`'s `cmd_pending`/`cmd_confirm` are reduced to printers over it, and a new `figcite/web.py` serves a one-page UI plus a JSON API over the same functions. No new dependencies: stdlib `http.server`.

**Tech Stack:** Python >=3.10, stdlib `http.server`/`socketserver`, existing deps (pillow, python-pptx, PyMuPDF, requests), pytest.

**Spec:** `docs/superpowers/specs/2026-08-17-figcite-web-ui-design.md`

## Global Constraints

- Python >=3.10 (`pyproject.toml requires-python`). Code must not use 3.11+ syntax.
- **No new runtime dependencies.** Deps stay exactly: pillow, python-pptx, PyMuPDF, requests.
- Server binds `127.0.0.1` only. Never `0.0.0.0`.
- Server refuses to start if the port is in use; it must not auto-select another port.
- `apply` never overwrites its input file.
- No UI path to `allow_unconfirmed`. The web layer must never pass it as `True`.
- Existing CLI tests must pass **unmodified**. If one needs editing to pass, stop and report — behavior changed.
- Tests touching a real socket, real PowerPoint, or the network get `@pytest.mark.live` (see `pytest.ini`).
- `tests/conftest.py` already redirects `FIGCITE_HOME` and strips Zotero credentials. Do not add per-test isolation; it is global.

---

### Task 1: `service.PendingItem` and `pending_items()`

**Files:**

- Create: `figcite/service.py`
- Test: `tests/test_service_pending.py`

**Interfaces:**

- Consumes: `figcite.clipboard.list_pending() -> list[dict]`, `figcite.store.all_records() -> dict[str, Record]`
- Produces: `PendingItem` dataclass; `pending_items() -> list[PendingItem]`; `ref` strings of the form `staged:<png filename>` and `filed:<sha256>`

**Why stable refs rather than list indices:** the CLI addresses items as `0` and `m0`, which shift when an item is resolved. A UI holds refs across a round trip, so a shifting index is a wrong-image-confirmed bug. The CLI keeps its index syntax and maps index -> ref internally (Task 3).

- [ ] **Step 1: Write the failing test**

```python
import json
from pathlib import Path

from figcite import service, store
from figcite.provenance import Record, now_stamps


def test_a_filed_unconfirmed_capture_becomes_a_pending_item():
    u, loc = now_stamps()
    store.put(Record(
        sha256="a" * 64, dhash="0" * 16, source_kind="clipboard",
        confirmed=False, captured_utc=u, captured_local=loc,
        source_detail={"clipboard_capture": {"process": "firefox", "title": "A paper"}},
    ))
    items = service.pending_items()
    refs = [i.ref for i in items]
    assert f"filed:{'a' * 64}" in refs
    it = [i for i in items if i.ref == f"filed:{'a' * 64}"][0]
    assert it.kind == "filed"
    assert it.doi is None
    assert it.candidates == []
    assert it.error is None          # "looked, found nothing"


def test_a_lookup_failure_is_not_an_empty_candidate_list(tmp_path, monkeypatch):
    """The distinction the whole fail-loud rule rests on."""
    staging = tmp_path / "staging"
    staging.mkdir()
    png = staging / "clip-1.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    (staging / "clip-1.pending.json").write_text(json.dumps({
        "png": str(png),
        "capture": {"process": "firefox", "title": "A paper", "width": 800, "height": 600},
        "inference": {"kind": "browser", "candidates": [], "error": "CrossRef unreachable"},
    }))
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))

    it = [i for i in service.pending_items() if i.kind == "staged"][0]
    assert it.candidates == []
    assert it.error == "CrossRef unreachable"
    assert it.ref == "staged:clip-1.png"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_service_pending.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'figcite.service'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Actions, without a front end attached.

Both the CLI and the web UI call these. Nothing here prints, parses argv, or
knows about HTTP. The reason this module exists at all: two implementations of
"attach a citation to an image" is the one drift this codebase cannot survive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import clipboard, store


@dataclass
class PendingItem:
    ref: str                       # opaque; "staged:<png name>" or "filed:<sha256>"
    kind: str                      # "staged" | "filed"
    context: str                   # app, window title, timestamp
    width: Optional[int] = None
    height: Optional[int] = None
    doi: Optional[str] = None
    doi_evidence: str = ""
    grounded: bool = False         # True => the DOI is evidence, not a guess
    candidates: list[dict] = field(default_factory=list)
    error: Optional[str] = None    # None + empty candidates == "looked, found nothing"


def pending_items() -> list[PendingItem]:
    out: list[PendingItem] = []

    for rec in store.all_records().values():
        if rec.confirmed or rec.source_kind != "clipboard":
            continue
        out.append(PendingItem(
            ref=f"filed:{rec.sha256}",
            kind="filed",
            context=rec.context_line(),
            error=None,
        ))

    for it in clipboard.list_pending():
        png = Path(it["png"])
        cap = it.get("capture", {}) or {}
        inf = it.get("inference", {}) or {}
        out.append(PendingItem(
            ref=f"staged:{png.name}",
            kind="staged",
            context=_context_of(cap),
            width=cap.get("width"),
            height=cap.get("height"),
            doi=inf.get("doi"),
            doi_evidence=inf.get("doi_evidence", "") or "",
            grounded=bool(inf.get("grounded")),
            candidates=list(inf.get("candidates") or []),
            error=inf.get("error"),
        ))
    return out


def _context_of(cap: dict[str, Any]) -> str:
    bits = [b for b in (cap.get("process"), cap.get("title"), cap.get("when")) if b]
    return " - ".join(str(b) for b in bits)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_service_pending.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add figcite/service.py tests/test_service_pending.py
git commit -m "feat: a front-end-agnostic view of what is pending"
```

---

### Task 2: `service.confirm()` and `service.skip()`

**Files:**

- Create: `figcite/_actions.py` — the record-building helpers moved out of `cli.py`
- Modify: `figcite/service.py`
- Test: `tests/test_service_confirm.py`

**Interfaces:**

- Consumes: `PendingItem`/`pending_items()` from Task 1; `figcite.crossref.record_from_doi(doi, *, confirmed, source_kind, source_detail) -> Record`; `figcite.provenance.embed(src, dst, rec) -> Record`; `figcite.store.put(rec) -> None`
- Produces: `confirm(ref, *, doi=None, pick=None, cite=None, own_work=False, adapted_from=None, note=None, out=None) -> Record`; `skip(ref) -> None`; `_actions.record_for(...)`, `_actions.finalize(src, rec, out) -> Path`

**The guard being preserved.** `cli.cmd_confirm` currently refuses an inferred DOI unless it was grounded:

```python
if doi and not inf.get("grounded"):
    print("that DOI was only guessed; pass --doi explicitly to accept it", ...)
    return 2
```

That is the never-auto-confirm rule. It must survive the move, and Step 2 below exists to prove it can fail.

- [ ] **Step 1: Write the failing test**

```python
import json
import pytest
from pathlib import Path

from figcite import service, store
from figcite.provenance import Record, now_stamps


def _stage(tmp_path, monkeypatch, inference):
    staging = tmp_path / "staging"
    staging.mkdir()
    png = staging / "clip-1.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    (staging / "clip-1.pending.json").write_text(json.dumps({
        "png": str(png),
        "capture": {"process": "firefox", "title": "A paper"},
        "inference": inference,
    }))
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))
    return png


def test_a_guessed_doi_is_refused_without_an_explicit_doi(tmp_path, monkeypatch):
    """Merely displaying a guess must never be enough to confirm it."""
    _stage(tmp_path, monkeypatch, {"doi": "10.1/guess", "grounded": False, "candidates": []})
    with pytest.raises(service.NotGrounded):
        service.confirm("staged:clip-1.png")


def test_the_same_guess_is_accepted_when_named_explicitly(tmp_path, monkeypatch):
    """Positive control: the legitimate path still works, so a broken harness
    cannot read as 'safely refused'."""
    _stage(tmp_path, monkeypatch, {"doi": "10.1/guess", "grounded": False, "candidates": []})
    monkeypatch.setattr(service, "record_for", _fake_record_for)
    rec = service.confirm("staged:clip-1.png", doi="10.1/guess")
    assert rec.confirmed is True
    assert rec.doi == "10.1/guess"


def _fake_record_for(doi, cite, url, *, confirmed, kind, detail,
                     adapted_from=None, note=""):
    u, loc = now_stamps()
    return Record(doi=doi, confirmed=confirmed, source_kind=kind,
                  source_detail=detail, captured_utc=u, captured_local=loc)


@pytest.mark.parametrize("kwargs", [
    {"doi": "10.1/a", "own_work": True},       # two selectors
    {"pick": 0, "cite": "Someone 2020"},       # two selectors
])
def test_confirm_refuses_more_than_one_selector(tmp_path, monkeypatch, kwargs):
    _stage(tmp_path, monkeypatch, {"candidates": [{"doi": "10.1/a"}]})
    with pytest.raises(ValueError):
        service.confirm("staged:clip-1.png", **kwargs)


def test_zero_selectors_means_use_this_item_s_own_grounded_doi(tmp_path, monkeypatch):
    """Controller Ruling 1. Zero selectors is the `figcite confirm 0` case that
    cmd_pending itself prints as the instruction for a grounded capture. It is
    valid, and the grounded check -- not an arity check -- is what guards it."""
    _stage(tmp_path, monkeypatch,
           {"doi": "10.1/real", "grounded": True, "candidates": []})
    monkeypatch.setattr(service, "record_for", _fake_record_for)
    rec = service.confirm("staged:clip-1.png")
    assert rec.doi == "10.1/real"


def test_skip_defers_and_does_not_delete(tmp_path, monkeypatch):
    png = _stage(tmp_path, monkeypatch, {"candidates": []})
    service.skip("staged:clip-1.png")
    assert png.exists(), "skip must not touch disk"
    assert service.pending_items()[-1].ref == "staged:clip-1.png", "deferred to the back"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_service_confirm.py -v`
Expected: FAIL — `AttributeError: module 'figcite.service' has no attribute 'NotGrounded'`

- [ ] **Step 3: Write minimal implementation**

Create `figcite/_actions.py` by moving `_slug`, `_library_dest`, `_finalize`, and `_record_for` out of `cli.py` verbatim, renamed to `slug`, `library_dest`, `finalize`, `record_for`, with all printing deleted from `finalize` (the CLI prints; the service does not).

```python
# figcite/service.py  (additions)

class NotGrounded(Exception):
    """An inferred DOI was offered for confirmation without being named."""


_skipped: list[str] = []          # deferred for this process only; never persisted


def skip(ref: str) -> None:
    if ref not in _skipped:
        _skipped.append(ref)


def confirm(ref, *, doi=None, pick=None, cite=None, own_work=False,
            adapted_from=None, note=None, out=None):
    # Controller Ruling 1: zero selectors is VALID and means "use this item's
    # own grounded DOI" -- the `figcite confirm 0` invocation cmd_pending prints
    # for a grounded capture. What guards a guess is the NotGrounded check
    # below, not an arity check up here. Requiring exactly one would both break
    # that invocation and make the NotGrounded branch unreachable dead code.
    selectors = [doi is not None, pick is not None, cite is not None, bool(own_work)]
    if sum(selectors) > 1:
        raise ValueError(
            "confirm takes at most one of doi=, pick=, cite=, own_work=True "
            f"(got {sum(selectors)})"
        )

    item = _item_for(ref)
    if item is None:
        raise KeyError(f"no pending item {ref!r}")

    if item.kind == "filed":
        return _confirm_filed(item, doi=doi, adapted_from=adapted_from, note=note)

    raw = _raw_staged(ref)
    inf = raw.get("inference", {}) or {}

    if pick is not None:
        try:
            doi = inf["candidates"][pick]["doi"]
        except (KeyError, IndexError, TypeError):
            raise KeyError(f"no candidate {pick} on {ref}")
    elif own_work:
        cite = "This work"
    elif doi is None and cite is None:
        doi = inf.get("doi")
        if doi and not inf.get("grounded"):
            raise NotGrounded(
                "that DOI was only guessed; name it explicitly to accept it"
            )

    detail = {
        "clipboard_capture": raw.get("capture", {}),
        "inference_kind": inf.get("kind", ""),
        "doi_evidence": inf.get("doi_evidence", ""),
    }
    rec = record_for(doi, cite, None, confirmed=True, kind="clipboard",
                     detail=detail, adapted_from=adapted_from, note=note or "")
    png = Path(raw["png"])
    dest = finalize(png, rec, out)
    _clear_staged(png, dest)
    return rec
```

`service.py` imports the moved helpers into its own namespace — `from ._actions import record_for, finalize` — which is what makes the `monkeypatch.setattr(service, "record_for", ...)` in the test above work. The private helpers, in full:

```python
def _item_for(ref: str):
    for it in pending_items():
        if it.ref == ref:
            return it
    return None


def _raw_staged(ref: str) -> dict:
    """The original pending dict behind a staged ref."""
    name = ref.split(":", 1)[1]
    for raw in clipboard.list_pending():
        if Path(raw["png"]).name == name:
            return raw
    raise KeyError(f"no staged capture {ref!r}")


def _clear_staged(png: Path, dest: Path) -> None:
    """Exactly the deletions cmd_confirm performed, no more."""
    for suffix in (".pending.json", ".capture.json"):
        p = Path(str(png)[:-4] + suffix)
        if p.exists():
            p.unlink()
    if png.exists() and png != dest:
        png.unlink()


def _confirm_filed(item, *, doi, adapted_from, note):
    """A capture already in the manifest, still unconfirmed.

    Only a DOI can resolve one: the image bytes are already filed, so there is
    nothing to finalize -- the manifest simply gains a citation for that sha.
    """
    if not doi:
        raise ValueError("a filed capture can only be resolved with doi=")
    target = store.get(item.ref.split(":", 1)[1])
    if target is None:
        raise KeyError(f"no filed record {item.ref!r}")
    rec = record_for(doi, None, None, confirmed=True, kind="clipboard",
                     detail=target.source_detail, adapted_from=adapted_from,
                     note=note or "")
    rec.sha256, rec.dhash = target.sha256, target.dhash
    rec.captured_utc, rec.captured_local = target.captured_utc, target.captured_local
    store.put(rec)
    return rec
```

`pending_items()` from Task 1 gains a final reorder that moves any ref present in `_skipped` to the end of the list:

```python
    out.sort(key=lambda i: i.ref in _skipped)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_service_confirm.py -v`
Expected: 6 passed

- [ ] **Step 5: Prove the guard test can fail**

Temporarily delete the `raise NotGrounded(...)` line, then run:
`python3 -m pytest tests/test_service_confirm.py::test_a_guessed_doi_is_refused_without_an_explicit_doi -v`
Expected: **FAIL** — `DID NOT RAISE`. Restore the line and confirm it passes again. A guard test never seen red is not evidence.

- [ ] **Step 6: Commit**

```bash
git add figcite/_actions.py figcite/service.py tests/test_service_confirm.py
git commit -m "feat: confirm and skip, with the never-auto-confirm guard intact"
```

---

### Task 3: Reduce `cmd_pending` and `cmd_confirm` to printers

**Files:**

- Modify: `figcite/cli.py:237-368` (`cmd_pending`, `cmd_confirm`), and delete `_slug`/`_library_dest`/`_finalize`/`_record_for` at `figcite/cli.py:16-75`
- Test: existing `tests/` suite, unmodified

**Interfaces:**

- Consumes: `service.pending_items()`, `service.confirm()`, `service.NotGrounded`, `_actions.finalize`
- Produces: no new interface. CLI output text and exit codes are unchanged.

The CLI keeps accepting `0` and `m0`. It maps them to refs itself: the Nth `staged` item and the Nth `filed` item of `service.pending_items()` respectively. `cmd_tag`/`cmd_grab`/`cmd_register` import the helpers from `_actions` instead of defining them.

- [ ] **Step 1: Build the regression net this task claimed it already had**

**Controller Ruling 2.** The plan originally asserted that "the existing CLI tests are the regression net". They are not. No test in the suite calls `cli.main(["confirm", ...])` or `cli.main(["pending"])` — the only `cli.main` callers are `tests/test_live.py:75` and `tests/test_bibtex.py:197,210`, none of which touch these two functions. Refactoring them under a green suite would prove nothing, because nothing in the suite executes them. A test's name is not its coverage.

So write the characterization tests FIRST, against the current code, and only then refactor. Create `tests/test_cli_confirm_characterization.py`:

```python
import json
import pytest
from pathlib import Path
from PIL import Image

from figcite import cli, clipboard, store


def _stage(tmp_path, monkeypatch, inference):
    staging = tmp_path / "staging"
    staging.mkdir()
    png = staging / "clip-1.png"
    Image.new("RGB", (40, 40), "white").save(png)
    (staging / "clip-1.pending.json").write_text(json.dumps({
        "png": str(png),
        "capture": {"process": "firefox", "title": "A paper", "width": 40, "height": 40},
        "inference": inference,
    }))
    monkeypatch.setattr(clipboard, "staging_dirs", lambda: (None, staging))
    return png


def test_pending_lists_a_staged_capture_with_its_window(tmp_path, monkeypatch, capsys):
    _stage(tmp_path, monkeypatch, {"kind": "browser", "candidates": []})
    assert cli.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert "[0]" in out
    assert "firefox" in out
    assert "A paper" in out


def test_pending_says_so_when_nothing_is_staged(tmp_path, monkeypatch, capsys):
    staging = tmp_path / "empty"
    staging.mkdir()
    monkeypatch.setattr(clipboard, "staging_dirs", lambda: (None, staging))
    assert cli.main(["pending"]) == 0
    assert "nothing pending" in capsys.readouterr().out


def test_confirm_refuses_a_guessed_doi(tmp_path, monkeypatch, capsys):
    """The behavior that must survive the refactor."""
    _stage(tmp_path, monkeypatch, {"doi": "10.1/guess", "grounded": False, "candidates": []})
    assert cli.main(["confirm", "0"]) == 2
    assert "only guessed" in capsys.readouterr().err


def test_confirm_with_a_cite_files_the_capture(tmp_path, monkeypatch):
    png = _stage(tmp_path, monkeypatch, {"candidates": []})
    assert cli.main(["confirm", "0", "--cite", "Band et al. 2014"]) == 0
    rec = max(store.all_records().values(), key=lambda r: r.captured_utc)
    assert rec.citation == "Band et al. 2014"
    assert rec.confirmed is True
    assert not png.exists(), "the staged png is consumed on confirm"


def test_confirm_rejects_an_out_of_range_index(tmp_path, monkeypatch, capsys):
    _stage(tmp_path, monkeypatch, {"candidates": []})
    assert cli.main(["confirm", "7"]) == 2
    assert "no pending item" in capsys.readouterr().err
```

- [ ] **Step 1b: Run them against the UNREFACTORED code and record the baseline**

Run: `python3 -m pytest tests/test_cli_confirm_characterization.py -v`
Expected: **5 passed against the current, un-refactored `cli.py`.** If any fails now, it encodes an assumption the current code does not honor — fix the test, not the code; the point is to pin what exists.

Then: `python3 -m pytest tests/ -q -m "not live" | tail -1`
Expected: `135 passed` (130 + these 5). Record it; this is the number Step 4 must reproduce.

Commit the characterization tests on their own, before touching `cli.py`:

```bash
git add tests/test_cli_confirm_characterization.py
git commit -m "test: pin cmd_pending and cmd_confirm before refactoring them"
```

- [ ] **Step 2: Rewrite `cmd_pending` as a printer**

```python
def cmd_pending(a) -> int:
    from . import service

    items = service.pending_items()
    if not items:
        print("nothing pending (run `figcite watch`, then snip something)")
        return 0
    staged = [i for i in items if i.kind == "staged"]
    filed = [i for i in items if i.kind == "filed"]
    for i, it in enumerate(filed):
        print(f"[m{i}] {it.context[:100]}")
        print(f"      resolve: figcite confirm m{i} --doi 10.x/y")
    for i, it in enumerate(staged):
        print(f"[{i}] {it.ref.split(':', 1)[1]}  {it.width or '?'}x{it.height or '?'}")
        if it.context:
            print(f"     window: {it.context[:90]}")
        if it.error:
            print(f"     LOOKUP FAILED: {it.error}")
        elif it.doi:
            print(f"     DOI: {it.doi}   (from {it.doi_evidence})")
            print(f"     confirm: figcite confirm {i}")
        elif it.candidates:
            for ci, c in enumerate(it.candidates):
                print(f"     cand {ci}: score {c['score']:>5}  {c['doi']}")
                print(f"               {c['title'][:80]} ({c.get('container', '')} {c.get('year', '')}) [{c.get('type', '')}]")
            print(f"     confirm: figcite confirm {i} --pick <n>   (or --doi 10.x/y)")
        else:
            print(f"     no source inferred: {it.doi_evidence}")
            print(f"     confirm: figcite confirm {i} --doi 10.x/y")
    return 0
```

Note the added `LOOKUP FAILED` branch — the old CLI printed a failed lookup and a genuine no-match identically. That is the bug the spec's `error` field exists to fix, and fixing it in the CLI too keeps the front ends honest.

- [ ] **Step 3: Rewrite `cmd_confirm` as a printer over `service.confirm`**

```python
def cmd_confirm(a) -> int:
    from . import service

    items = service.pending_items()
    idx = str(a.index)
    if idx.startswith("m"):
        pool = [i for i in items if i.kind == "filed"]
        n = idx[1:]
    else:
        pool = [i for i in items if i.kind == "staged"]
        n = idx
    try:
        ref = pool[int(n)].ref
    except (ValueError, IndexError):
        print(f"no pending item {a.index} (have {len(pool)})", file=sys.stderr)
        return 2

    try:
        rec = service.confirm(
            ref, doi=a.doi, pick=a.pick, cite=a.cite,
            adapted_from=a.adapted_from, note=a.note or "", out=a.out,
        )
    except service.NotGrounded as e:
        print(str(e), file=sys.stderr)
        return 2
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"resolved {a.index}: {rec.display()}")
    return 0
```

- [ ] **Step 4: Run the full suite and diff against the baseline**

Run: `python3 -m pytest tests/ -q -m "not live"`
Expected: `135 passed` — the number recorded in Step 1b, **with no test file edited**, and in particular with all 5 characterization tests still green. If a test needed editing, stop: behavior changed and the refactor is wrong. The characterization tests are the ones that matter here; the other 130 never executed these two functions.

- [ ] **Step 5: Commit**

```bash
git add figcite/cli.py
git commit -m "refactor: the CLI becomes a printer over the service layer"
```

---

### Task 4: `service.audit()` and `service.apply()` with a normalized report

**Files:**

- Modify: `figcite/service.py`
- Test: `tests/test_service_deck.py`

**Interfaces:**

- Consumes: `figcite.deck.audit(path, min_inches) -> dict` (key `"pptx"`, rows keyed `slide`/`shape`); `figcite.pdfdeck.audit(path, min_inches) -> dict` (key `"file"`, rows keyed `page`/`xref`); `figcite.deck.apply(pptx, out, *, captions, credits, caption_own_work, manifest_path, allow_unconfirmed, min_inches) -> dict`
- Produces: `audit(path, min_inches=1.0) -> dict` with keys `path, kind, pictures, tagged, unconfirmed, untagged_substantive, rows`; each row `{ref, location, label, status, matched_by, decorative, citation, doi, license_url, reuse, retracted}`; `apply(path, out=None, **opts) -> dict`

**Why normalize:** the two audit functions disagree on their own key names. Left alone, every consumer grows a branch. One shape here means the UI has none.

- [ ] **Step 1: Write the failing test**

```python
from figcite import service


def test_audit_normalizes_the_pptx_and_pdf_report_shapes(monkeypatch):
    monkeypatch.setattr(service.deck, "audit", lambda p, min_inches=1.0: {
        "pptx": "/x/deck.pptx", "pictures": 1, "tagged": 0,
        "unconfirmed": 0, "untagged_substantive": 1,
        "rows": [{"slide": 3, "shape": "Picture 4", "size_in": [2.0, 2.0],
                  "decorative": False, "matched_by": "none", "record": None,
                  "alt_text": ""}],
    })
    rep = service.audit("/x/deck.pptx")
    assert rep["path"] == "/x/deck.pptx"
    assert rep["kind"] == "pptx"
    assert rep["rows"][0]["location"] == "slide 3"
    assert rep["rows"][0]["status"] == "no-source"


def test_audit_reports_the_licensing_verdict_for_a_matched_row(monkeypatch):
    from figcite.provenance import Record
    rec = Record(sha256="b" * 64, doi="10.1/x", short_cite="Band et al. 2014",
                 confirmed=True, license_url="https://creativecommons.org/licenses/by/4.0/",
                 reuse="reuse-ok-attribution-required", retracted=False)
    monkeypatch.setattr(service.deck, "audit", lambda p, min_inches=1.0: {
        "pptx": "/x/deck.pptx", "pictures": 1, "tagged": 1,
        "unconfirmed": 0, "untagged_substantive": 0,
        "rows": [{"slide": 7, "shape": "Picture 1", "size_in": [3.0, 3.0],
                  "decorative": False, "matched_by": "manifest-sha256",
                  "record": rec, "alt_text": ""}],
    })
    row = service.audit("/x/deck.pptx")["rows"][0]
    assert row["status"] == "ok"
    assert row["reuse"] == "reuse-ok-attribution-required"
    assert row["retracted"] is False
    assert row["ref"] == f"sha:{'b' * 64}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_service_deck.py -v`
Expected: FAIL — `AttributeError: module 'figcite.service' has no attribute 'audit'`

- [ ] **Step 3: Write minimal implementation**

```python
from . import deck, pdfdeck


def _is_pdf(path) -> bool:
    return str(path).lower().endswith(".pdf")


def audit(path, min_inches: float = 1.0) -> dict:
    if _is_pdf(path):
        raw, kind = pdfdeck.audit(path, min_inches=min_inches), "pdf"
        where = lambda r: f"page {r['page']}"
        label = lambda r: f"xref {r['xref']}"
    else:
        raw, kind = deck.audit(path, min_inches=min_inches), "pptx"
        where = lambda r: f"slide {r['slide']}"
        label = lambda r: r["shape"]

    rows = []
    for r in raw["rows"]:
        rec = r["record"]
        if rec is None:
            status = "no-source"
        elif rec.confirmed:
            status = "ok"
        else:
            status = "unconfirmed"
        rows.append({
            "ref": f"sha:{rec.sha256}" if rec is not None else "",
            "location": where(r),
            "label": label(r),
            "status": status,
            "matched_by": r["matched_by"],
            "decorative": r["decorative"],
            "citation": (rec.short_cite or rec.doi or "") if rec else "",
            "doi": (rec.doi or "") if rec else "",
            "license_url": (rec.license_url or "") if rec else "",
            "reuse": (rec.reuse or "unknown") if rec else "",
            "retracted": bool(rec.retracted) if rec else False,
        })
    return {
        "path": raw.get("pptx") or raw.get("file"),
        "kind": kind,
        "pictures": raw["pictures"],
        "tagged": raw["tagged"],
        "unconfirmed": raw["unconfirmed"],
        "untagged_substantive": raw["untagged_substantive"],
        "rows": rows,
    }


def apply(path, out=None, **opts) -> dict:
    opts.pop("allow_unconfirmed", None)          # no UI path to it, ever
    out = out or _default_out(path)
    if Path(out).resolve() == Path(path).resolve():
        raise ValueError("apply refuses to overwrite its input")
    fn = pdfdeck.apply if _is_pdf(path) else deck.apply
    return fn(path, out, allow_unconfirmed=False, **opts)


def _default_out(path) -> str:
    p = Path(path)
    return str(p.with_name(p.stem + ".cited" + p.suffix))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_service_deck.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add figcite/service.py tests/test_service_deck.py
git commit -m "feat: one report shape for decks and PDFs alike"
```

---

### Task 5: `service.thumbnail()`

**Files:**

- Modify: `figcite/service.py`
- Test: `tests/test_service_thumbnail.py`

**Interfaces:**

- Consumes: refs produced by Tasks 1 and 4 (`staged:<name>`, `filed:<sha>`, `sha:<sha>`)
- Produces: `thumbnail(ref, max_px=480) -> tuple[bytes, str]` returning PNG bytes and `"image/png"`

**Security requirement:** the ref is the only addressing scheme. A ref that is not present in `pending_items()` or the manifest raises `KeyError`. No path component of a ref is ever joined to a directory, so a browser cannot request `staged:../../etc/passwd` and read it.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from figcite import service


def test_an_unknown_ref_is_refused_rather_than_read_from_disk():
    with pytest.raises(KeyError):
        service.thumbnail("staged:../../../../etc/passwd")


def test_a_real_staged_capture_renders(tmp_path, monkeypatch):
    """Positive control in the same file: the refusal above must not be
    passing because thumbnail() is simply broken for everything."""
    from PIL import Image
    staging = tmp_path / "staging"
    staging.mkdir()
    Image.new("RGB", (1200, 900), "white").save(staging / "clip-1.png")
    (staging / "clip-1.pending.json").write_text(
        '{"png": "%s", "capture": {}, "inference": {}}' % (staging / "clip-1.png")
    )
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))

    blob, mime = service.thumbnail("staged:clip-1.png")
    assert mime == "image/png"
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    assert max(Image.open(__import__("io").BytesIO(blob)).size) <= 480
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_service_thumbnail.py -v`
Expected: FAIL — `AttributeError: module 'figcite.service' has no attribute 'thumbnail'`

- [ ] **Step 3: Write minimal implementation**

```python
import io
from PIL import Image


def thumbnail(ref: str, max_px: int = 480) -> tuple[bytes, str]:
    src = _resolve_ref_to_path(ref)      # raises KeyError for anything unknown
    im = Image.open(src)
    im.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="PNG")
    return buf.getvalue(), "image/png"
```

`_resolve_ref_to_path` looks the ref up in `pending_items()` (for `staged:`/`filed:`) or `store.all_records()` plus the library dir (for `sha:`), and raises `KeyError` when absent. It never constructs a path from the ref's text.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_service_thumbnail.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add figcite/service.py tests/test_service_thumbnail.py
git commit -m "feat: thumbnails addressed by ref, never by path"
```

---

### Task 6: `figcite/web.py` — server and JSON API

**Files:**

- Create: `figcite/web.py`
- Test: `tests/test_web_server.py`

**Interfaces:**

- Consumes: everything in `service` from Tasks 1-5
- Produces: `serve(port=8765, open_browser=False) -> None`; `make_server(port) -> ThreadingHTTPServer`; routes `GET /`, `GET /api/pending`, `POST /api/confirm`, `POST /api/skip`, `GET /api/thumb?ref=`, `POST /api/audit`, `POST /api/apply`

- [ ] **Step 1: Write the failing test**

```python
import json
import socket
import threading
import urllib.request
import pytest

from figcite import web


@pytest.mark.live
def test_the_server_serves_pending_over_a_real_socket():
    srv = web.make_server(0)                       # ephemeral port
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_web_server.py -v -m live`
Expected: FAIL — `ModuleNotFoundError: No module named 'figcite.web'`

- [ ] **Step 3: Write minimal implementation**

**First** create `figcite/webui.py` as a stub, so this task's import resolves and its tests can run before Task 7 exists:

```python
# figcite/webui.py  -- replaced wholesale in Task 7
PAGE = "<!doctype html><title>figcite</title><p>ui not built yet"
```

**Then** `figcite/web.py`:

```python
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
from .webui import PAGE            # stub created in this task, filled in Task 7

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
                rec = service.confirm(payload.pop("ref"), **payload)
                self._json({"ok": True, "citation": rec.display()})
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
        except Exception as e:                      # fail loud, with the real reason
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def log_message(self, *a):
        pass


def make_server(port: int) -> ThreadingHTTPServer:
    ThreadingHTTPServer.allow_reuse_address = False   # a taken port must raise
    return ThreadingHTTPServer((HOST, port), _Handler)


def serve(port: int = 8765, open_browser: bool = False) -> None:
    srv = make_server(port)
    print(f"figcite ui: http://{HOST}:{srv.server_address[1]}")
    if open_browser:
        import webbrowser
        webbrowser.open(f"http://{HOST}:{srv.server_address[1]}")
    srv.serve_forever()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_web_server.py -v -m live`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add figcite/web.py tests/test_web_server.py
git commit -m "feat: a loopback JSON API over the service layer"
```

---

### Task 7: The pending screen

**Files:**

- Create: `figcite/webui.py` — the single-page HTML/CSS/JS as a module-level `PAGE` string
- Test: `tests/test_webui_page.py`

**Interfaces:**

- Consumes: `GET /api/pending`, `POST /api/confirm`, `POST /api/skip`, `GET /api/thumb?ref=`
- Produces: `PAGE: str`

Kept as a Python string rather than a data file so `pip install figcite` needs no `package-data` entry beyond the existing `*.ps1`, and the server has no filesystem lookup at request time.

**Required rendering rules (each is asserted in Step 1):**

1. An item with `error` renders the error text — never the words "no candidates".
2. An item with `doi` and `grounded` renders the evidence string next to the DOI.
3. No candidate radio is pre-selected. Confirm is disabled until one is chosen.

Rule 3 is the UI half of the never-auto-confirm rule: a default selection plus a stray click is an unreviewed citation.

- [ ] **Step 1: Write the failing test**

```python
import re
from figcite.webui import PAGE


def test_the_page_never_preselects_a_candidate():
    """Scoped to <input> tags on purpose. A blanket `"checked" not in PAGE`
    is unsatisfiable -- any JS that reads a radio says `el.checked` -- and an
    assertion that cannot pass gets deleted by the next person, taking the
    real guarantee with it."""
    for tag in re.findall(r"<input[^>]*>", PAGE):
        assert "checked" not in tag, f"pre-selected candidate: {tag}"


def test_the_page_renders_lookup_errors_distinctly_from_no_matches():
    assert "LOOKUP FAILED" in PAGE
    assert "item.error" in PAGE


def test_the_page_shows_why_a_doi_is_trusted():
    assert "doi_evidence" in PAGE


def test_the_page_is_self_contained():
    assert not re.search(r'src="https?://', PAGE), "no third-party assets"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_webui_page.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'figcite.webui'`

- [ ] **Step 3: Write the page**

Replace the Task 6 stub `figcite/webui.py` with the real page:

```python
"""The whole front end: one HTML string, no build step, no assets.

Kept in Python rather than a data file so packaging needs no new
package-data entry and the server does no filesystem lookup per request.
"""

PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>figcite</title>
<style>
  :root { color-scheme: light dark; --bg:#fff; --fg:#111; --mut:#666;
          --line:#ddd; --warn:#a40000; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#151515; --fg:#eee; --mut:#999; --line:#333; --warn:#ff8a80; }
  }
  body { background:var(--bg); color:var(--fg); font:14px/1.5 system-ui, sans-serif;
         margin:0; padding:1.5rem; }
  nav button { font:inherit; padding:.4rem .9rem; border:1px solid var(--line);
               background:transparent; color:inherit; cursor:pointer; }
  nav button[aria-selected=true] { border-bottom:2px solid var(--fg); font-weight:600; }
  .card { display:flex; gap:1rem; border:1px solid var(--line); padding:1rem;
          margin:1rem 0; align-items:flex-start; }
  .card img { max-width:260px; max-height:260px; border:1px solid var(--line); }
  .ctx { color:var(--mut); }
  .fail { color:var(--warn); font-weight:600; }
  .ev { color:var(--mut); font-style:italic; }
  label { display:block; margin:.2rem 0; }
</style>
<nav>
  <button id="tab-pending" aria-selected="true" onclick="show('pending')">Pending</button>
  <button id="tab-deck" aria-selected="false" onclick="show('deck')">Deck</button>
</nav>
<section id="pending"></section>
<section id="deck" hidden></section>
<script>
const $ = (s) => document.querySelector(s);

function show(which) {
  for (const t of ["pending", "deck"]) {
    $("#" + t).hidden = (t !== which);
    $("#tab-" + t).setAttribute("aria-selected", String(t === which));
  }
}

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function loadPending() {
  const r = await fetch("/api/pending");
  const { items } = await r.json();
  $("#pending").innerHTML = items.length
    ? items.map(card).join("")
    : "<p>nothing pending. Run <code>figcite watch</code>, then snip something.</p>";
}

function card(item) {
  let body;
  if (item.error) {
    // A failed lookup and a genuine no-match must never read the same.
    body = `<p class="fail">LOOKUP FAILED: ${esc(item.error)}</p>
            <p>Enter a DOI by hand below.</p>`;
  } else if (item.doi && item.grounded) {
    body = `<p><strong>${esc(item.doi)}</strong>
              <span class="ev">evidence: ${esc(item.doi_evidence)}</span></p>`;
  } else if (item.candidates.length) {
    body = item.candidates.map((c, i) => `
      <label><input type="radio" name="pick-${esc(item.ref)}" value="${i}"
                    onchange="enable('${esc(item.ref)}')">
        ${esc(c.origin || "")} ${esc(c.score || "")} &mdash;
        ${esc(c.title)} (${esc(c.container || "")} ${esc(c.year || "")})
      </label>`).join("");
  } else {
    body = `<p class="ctx">no source inferred${
      item.doi_evidence ? ": " + esc(item.doi_evidence) : ""}</p>`;
  }
  return `<div class="card" data-ref="${esc(item.ref)}">
    <img src="/api/thumb?ref=${encodeURIComponent(item.ref)}" alt="">
    <div>
      <p class="ctx">${esc(item.context)}</p>
      ${body}
      <p><input placeholder="10.xxxx/yyyy" id="doi-${esc(item.ref)}"
                oninput="enable('${esc(item.ref)}')"></p>
      <p>
        <button id="ok-${esc(item.ref)}" disabled
                onclick="confirmRef('${esc(item.ref)}')">Confirm</button>
        <button onclick="ownWork('${esc(item.ref)}')">This is my own work</button>
        <button onclick="post('/api/skip', {ref:'${esc(item.ref)}'}).then(loadPending)">Skip</button>
      </p>
    </div></div>`;
}

function enable(ref) {
  const picked = document.querySelector(`input[name="pick-${ref}"]:checked`);
  const typed = $("#doi-" + CSS.escape(ref)).value.trim();
  $("#ok-" + CSS.escape(ref)).disabled = !(picked || typed);
}

async function post(url, payload) {
  const r = await fetch(url, {method:"POST",
    headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
  const out = await r.json();
  if (!r.ok) { alert(out.error || "failed"); throw new Error(out.error); }
  return out;
}

async function confirmRef(ref) {
  const picked = document.querySelector(`input[name="pick-${ref}"]:checked`);
  const typed = $("#doi-" + CSS.escape(ref)).value.trim();
  const body = typed ? {ref, doi: typed} : {ref, pick: Number(picked.value)};
  await post("/api/confirm", body);
  loadPending();
}

async function ownWork(ref) {
  await post("/api/confirm", {ref, own_work: true});
  loadPending();
}

loadPending();
</script>
"""
```

Note `input[name="pick-..."]:checked` in the JS — the selector is inside a JS string, not an `<input>` tag, so the Step 1 test passes while the guarantee holds.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_webui_page.py -v`
Expected: 4 passed

- [ ] **Step 5: Look at it**

Run: `python3 -m figcite ui` and open the URL. Confirm a real pending capture end to end.
**This step is not self-verifiable.** Rendered output must be checked by the user or a fresh reviewer — the implementer's own read of their own UI is not evidence.

- [ ] **Step 6: Commit**

```bash
git add figcite/webui.py tests/test_webui_page.py
git commit -m "feat: the pending screen, where you decide by looking"
```

---

### Task 8: The deck screen with licensing badges

**Files:**

- Modify: `figcite/webui.py`
- Test: `tests/test_webui_deck.py`

**Interfaces:**

- Consumes: `POST /api/audit`, `POST /api/apply`, the normalized row shape from Task 4
- Produces: no new module interface

- [ ] **Step 1: Write the failing test**

```python
from figcite.webui import PAGE

REUSE_VERDICTS = [
    "public-domain",
    "reuse-ok-attribution-required",
    "reuse-ok-share-alike-attribution-required",
    "noncommercial-only",
    "restricted-no-derivatives",
    "publisher-terms-check-required",
    "unknown-ask-publisher",
]


def test_every_reuse_verdict_the_classifier_can_emit_has_a_badge():
    """A verdict with no badge renders as blank, which reads as 'fine'."""
    for verdict in REUSE_VERDICTS:
        assert verdict in PAGE, f"no badge for {verdict}"


def test_retraction_is_rendered_loudly():
    assert "RETRACTED" in PAGE


def test_the_headline_number_is_the_unsourced_count():
    assert "untagged_substantive" in PAGE
```

The first test is the guard against a badge map that silently drops a verdict. Its list is copied from `crossref.classify_reuse`; if that function gains a verdict, this test fails until the badge exists.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_webui_deck.py -v`
Expected: FAIL — the verdict strings are not in `PAGE` yet

- [ ] **Step 3: Write the deck tab**

Append to the `<style>` block:

```css
.badge {
  display: inline-block;
  padding: 0 0.4rem;
  border: 1px solid var(--line);
  border-radius: 3px;
  font-size: 12px;
}
.badge.warn {
  color: var(--warn);
  border-color: var(--warn);
}
.retracted {
  color: var(--warn);
  font-weight: 700;
}
table {
  border-collapse: collapse;
  width: 100%;
}
td,
th {
  border-bottom: 1px solid var(--line);
  padding: 0.4rem;
  text-align: left;
  vertical-align: top;
}
```

Replace the empty `<section id="deck">` with the markup, and append the script:

```html
<section id="deck" hidden>
  <p>
    <input id="deckpath" size="60" placeholder="/path/to/deck.pptx" />
    <button onclick="auditDeck()">Audit</button>
    <button onclick="applyDeck()">Apply</button>
  </p>
  <p id="decksummary"></p>
  <table id="deckrows"></table>
</section>
```

```javascript
// Every verdict crossref.classify_reuse can return. A verdict missing from
// this map would render blank, and blank reads as "fine" -- the opposite of
// what an unknown license means.
const REUSE = {
  "public-domain": ["public domain", ""],
  "reuse-ok-attribution-required": ["CC-BY: cite it", ""],
  "reuse-ok-share-alike-attribution-required": [
    "CC-BY-SA: cite + share alike",
    "",
  ],
  "noncommercial-only": ["noncommercial only", "warn"],
  "restricted-no-derivatives": ["no derivatives", "warn"],
  "publisher-terms-check-required": ["check publisher terms", "warn"],
  "unknown-ask-publisher": ["license unknown: ask", "warn"],
};

async function auditDeck() {
  const path = $("#deckpath").value.trim();
  const rep = await post("/api/audit", { path });
  $("#decksummary").textContent =
    `${rep.pictures} picture(s), ${rep.tagged} with provenance, ` +
    `${rep.unconfirmed} unconfirmed, ${rep.untagged_substantive} substantive but unsourced`;
  $("#deckrows").innerHTML =
    "<tr><th>where<th>figure<th>status<th>citation<th>licence</tr>" +
    rep.rows.map(deckRow).join("");
}

function deckRow(r) {
  const [text, cls] = REUSE[r.reuse] || ["", ""];
  const badge = r.reuse ? `<span class="badge ${cls}">${esc(text)}</span>` : "";
  const flag = r.retracted ? ' <span class="retracted">RETRACTED</span>' : "";
  const thumb = r.ref
    ? `<img src="/api/thumb?ref=${encodeURIComponent(r.ref)}" alt="" height="80">`
    : "";
  return `<tr><td>${esc(r.location)}<td>${thumb}
    <td>${esc(r.status)} <span class="ctx">[${esc(r.matched_by)}]</span>
    <td>${esc(r.citation)}<td>${badge}${flag}</tr>`;
}

async function applyDeck() {
  const out = await post("/api/apply", { path: $("#deckpath").value.trim() });
  alert("wrote " + (out.out || out.path || "the cited deck"));
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_webui_deck.py -v`
Expected: 3 passed

- [ ] **Step 5: Verify against a real deck**

Run `python3 -m figcite ui`, audit `demo/auxin_talk-COPY.pptx`, and confirm the browser reports the same counts as `python3 -m figcite audit demo/auxin_talk-COPY.pptx` (`15 picture(s), 15 with provenance, 0 unconfirmed, 0 substantive but unsourced`). A disagreement between the two front ends is the bug this whole design exists to prevent.

- [ ] **Step 6: Prove a browser-applied deck opens in real PowerPoint**

Spec success criterion 4. `service.apply` delegates to `deck.apply`, and the existing live test only covers the deck written by the CLI — a deck written through the web path has never been opened by PowerPoint. Add to `tests/test_pptx_powerpoint.py`, reusing that file's existing `_ps` helper and `requires_powerpoint` marker:

```python
@requires_powerpoint
def test_powerpoint_opens_a_deck_applied_through_the_web_path(tmp_path):
    from figcite import service

    src = Path("demo/auxin_talk-COPY.pptx")
    out = tmp_path / "web-applied.pptx"
    service.apply(str(src), str(out))
    assert out.exists()

    win = _win_userprofile()
    r = _ps(f"""
        $a = New-Object -ComObject PowerPoint.Application
        $p = $a.Presentations.Open("{out}", $true, $false, $false)
        $n = $p.Slides.Count
        $p.Close(); $a.Quit()
        "SLIDES=$n"
    """)
    assert "SLIDES=" in r.stdout, r.stderr
    assert "repair" not in (r.stdout + r.stderr).lower()
```

Run: `python3 -m pytest tests/test_pptx_powerpoint.py -v -m live -rs`
Expected: 4 passed, **0 skipped**. A skip here means PowerPoint COM was unavailable and the criterion is unverified — not that it passed.

- [ ] **Step 7: Commit**

```bash
git add figcite/webui.py tests/test_webui_deck.py
git commit -m "feat: the deck screen, with the licensing verdict finally visible"
```

---

### Task 9: The `figcite ui` verb and README

**Files:**

- Modify: `figcite/cli.py` (add `cmd_ui` and its subparser next to `cmd_watch`)
- Modify: `README.md`
- Test: `tests/test_cli_ui_verb.py`

**Interfaces:**

- Consumes: `web.serve(port, open_browser)`
- Produces: `figcite ui [--port N] [--open]`

- [ ] **Step 1: Write the failing test**

```python
from figcite import cli


def test_ui_verb_passes_the_port_through(monkeypatch):
    seen = {}
    monkeypatch.setattr("figcite.web.serve",
                        lambda port=8765, open_browser=False: seen.update(
                            port=port, open_browser=open_browser))
    assert cli.main(["ui", "--port", "9001", "--open"]) == 0
    assert seen == {"port": 9001, "open_browser": True}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_cli_ui_verb.py -v`
Expected: FAIL — `invalid choice: 'ui'`

- [ ] **Step 3: Add the verb**

```python
def cmd_ui(a) -> int:
    from . import web

    web.serve(port=a.port, open_browser=a.open)
    return 0
```

and beside the other subparsers:

```python
ui = sub.add_parser("ui", help="open the browser UI for pending captures and decks")
ui.add_argument("--port", type=int, default=8765)
ui.add_argument("--open", action="store_true", help="open a browser window too")
ui.set_defaults(fn=cmd_ui)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_cli_ui_verb.py -v`
Expected: 1 passed

- [ ] **Step 5: Update the README**

Add under the usage section:

```markdown
### The browser UI

    figcite ui --open

Resolve pending captures and audit a deck by looking at the pictures rather
than reading paths. Loopback only (127.0.0.1), no auth, single user.
```

Also update the test count line in the README to the new total from Task 10.

- [ ] **Step 6: Commit**

```bash
git add figcite/cli.py README.md tests/test_cli_ui_verb.py
git commit -m "feat: figcite ui"
```

---

### Task 10: Front-end parity test

**Files:**

- Test: `tests/test_frontend_parity.py`

**Interfaces:**

- Consumes: `cli.main()`, `service.confirm()`, `store.all_records()`

This is success criterion 2 from the spec, and the reason the service layer exists. If it ever fails, the front ends have drifted.

- [ ] **Step 1: Write the test**

```python
import json
from pathlib import Path

from figcite import cli, service, store

VOLATILE = {"captured_utc", "captured_local", "sha256", "dhash"}


def _stage(tmp_path, monkeypatch, name):
    staging = tmp_path / name
    staging.mkdir()
    png = staging / "clip-1.png"
    from PIL import Image
    Image.new("RGB", (40, 40), "white").save(png)
    (staging / "clip-1.pending.json").write_text(json.dumps({
        "png": str(png),
        "capture": {"process": "firefox", "title": "A paper"},
        "inference": {"kind": "browser", "candidates": [], "doi_evidence": ""},
    }))
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))


def test_both_front_doors_produce_the_same_record(tmp_path, monkeypatch):
    _stage(tmp_path, monkeypatch, "a")
    cli.main(["confirm", "0", "--cite", "Band et al. 2014"])
    via_cli = max(store.all_records().values(), key=lambda r: r.captured_utc)

    _stage(tmp_path, monkeypatch, "b")
    via_service = service.confirm("staged:clip-1.png", cite="Band et al. 2014")

    a = {k: v for k, v in vars(via_cli).items() if k not in VOLATILE}
    b = {k: v for k, v in vars(via_service).items() if k not in VOLATILE}
    assert a == b
```

`sha256`/`dhash` join the volatile set because the two fixtures are separate files on disk, not because content may differ; the citation fields are the ones under test.

- [ ] **Step 2: Run it**

Run: `python3 -m pytest tests/test_frontend_parity.py -v`
Expected: 1 passed

- [ ] **Step 3: Run the whole suite**

Run: `python3 -m pytest tests/ -q -m "not live"`
Expected: all pass, including the 5 Task 3 characterization tests that pin `cmd_pending`/`cmd_confirm`.

- [ ] **Step 4: Commit**

```bash
git add tests/test_frontend_parity.py
git commit -m "test: the two front doors must agree, or the service layer failed"
```

---

## Deviations from the spec, recorded

1. **`PendingItem` gained a `grounded` field** not in the spec's table. `confirm` needs it to distinguish an evidenced DOI from a guess, and the spec's own "show the evidence" rule implies it.
2. **Refs are stable strings, not list indices.** The spec said "opaque"; indices shift when an item is resolved, which in a UI that holds a ref across a round trip is a wrong-image-confirmed bug.
3. **`cmd_pending` gains a `LOOKUP FAILED` branch.** The spec required the distinction in the UI; leaving the CLI unable to express it would put a known defect in the older front end.
