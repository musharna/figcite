# figcite web UI + service layer (SP1)

Date: 2026-08-17
Status: implemented (branch `feat/web-ui`)

> **This file records the ORIGINAL design.** It is kept as written, not
> rewritten to match the code: decisions taken during implementation and the
> four review rounds that followed live in
> `.superpowers/sdd/2026-08-17-figcite-web-ui/progress.md`, which is the
> current record. Where the two disagree, progress.md and the code are right.
> Two specifics below have been corrected in place because they would mislead
> a reader about a shipped interface -- `confirm`'s return type, and the
> candidate dict's key name. Everything else is the design as approved,
> including places where implementation later ruled otherwise: "`confirm`
> argument exclusivity" below still says zero selectors raise `ValueError`,
> where the shipped `service.confirm` accepts zero and reads the item's own
> grounded DOI (controller ruling 1).

## Why

figcite works but every interaction is a command line, and every decision it
asks you to make is a decision _about a picture_: is this the paper that figure
came from; is this slide's figure sourced. You cannot see a picture in a
terminal. The interface and the task are mismatched.

## Decomposition

Three independent sub-projects. This spec covers SP1 only.

|         | Sub-project                                                            | Grouping rationale                                                                                      |
| ------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| **SP1** | Service layer + web UI (pending/confirm, deck audit, licensing badges) | Licensing is free here: `crossref.classify_reuse()` already computes the verdict and nothing renders it |
| **SP2** | Europe PMC figure index -> reverse sourcing + duplication detection    | One dhash index queried in two directions; building it twice would be absurd                            |
| **SP3** | Deck-usage index ("where have I used this figure")                     | Small, but needs apply-manifests to become queryable, which does not exist yet                          |

SP1 first: it is the stated pain and the only one with no dependency on the others.

## Non-goals for SP1

- No live reload. You refresh to see a new capture. Polling/SSE deferred until
  there is evidence it matters.
- No reverse image search (SP2).
- No duplication detection (SP2).
- No permissions-request drafting. Explicitly declined during design.
- No library browser screen. Deferred; CLI covers it.
- No UI equivalent of `--allow-unconfirmed`. Deliberate, see Safety.

## Architecture

Two new modules, one refactor.

- **`figcite/service.py`** (new) - single source of truth for actions.
  Structured returns, no printing, no argparse, no HTTP.
- **`figcite/web.py`** (new) - stdlib `ThreadingHTTPServer` + small route table.
  Serves one HTML page and a JSON API.
- **`figcite/cli.py`** (refactor) - `cmd_pending` and `cmd_confirm` hand their
  logic to `service.py` and become printers. Every other `cmd_*` is untouched.

### Why a service layer rather than letting the UI reimplement

`cmd_audit` already delegates to `deck.audit()` and returns a structured dict,
so the UI could consume that as-is. `cmd_pending` and `cmd_confirm` do not -
they mix the `m`-prefix filed-capture lookup and the resolve-and-finalize
sequence into their printing.

Two implementations of "attach a citation to an image" is the worst possible
drift in this codebase specifically. figcite's entire value is refusing to
guess; a tool that confirms differently depending on which door you came
through has stopped being trustworthy. The refactor is the cheap part.

Rejected alternative: UI shells out to the CLI and parses stdout. Parsing our
own human-readable output is fragile, and this repo already has an incident
where routing a command through a text filter hid a failing gate for two
commits (commit 81c3692). Same class of mistake.

### Dependency decision

stdlib `http.server`, not Flask/FastAPI. Current deps are four (pillow,
python-pptx, PyMuPDF, requests); a single-user localhost UI with ~7 endpoints
does not earn a fifth. Cost: routing is hand-written. Revisit if the API passes
roughly a dozen endpoints.

### Service contract

```python
pending_items() -> list[PendingItem]   # staged captures + filed-unconfirmed, unified
confirm(ref, *, doi=None, pick=None, cite=None,
        own_work=False, adapted_from=None, note=None) -> ConfirmResult
        # ^ ConfirmResult, not a bare Record: it carries `.record` AND the
        #   `.path` the image was filed to, which both front ends need
skip(ref)                     -> None
audit(path, min_inches=1.0)   -> Report          # delegates to deck.audit / pdfdeck.audit
apply(path, out=None, **opts) -> ApplyResult
thumbnail(ref)                -> (bytes, mimetype)
```

`ref` is an opaque string covering both of today's index forms (`0`, `m0`). The
UI never constructs CLI syntax, so the two front ends cannot drift on addressing.

**`PendingItem` fields** (the UI renders exactly these, nothing inferred):

| field             | meaning                                                                            |
| ----------------- | ---------------------------------------------------------------------------------- |
| `ref`             | opaque handle, passed back to `confirm`/`skip`/`thumbnail`                         |
| `kind`            | `staged` (png still in the staging dir) or `filed` (already a Record, unconfirmed) |
| `width`, `height` | pixel dimensions, for laying out the thumbnail                                     |
| `context`         | app, window title, capture timestamp - the `context_line()` data                   |
| `doi`             | auto-grounded DOI, or `None`                                                       |
| `doi_evidence`    | _why_ that DOI is trusted, e.g. "DOI in URL"; empty when `doi` is `None`           |
| `candidates`      | list of `{source, score, doi, title, container, year, type}`, possibly empty       |
| `error`           | the lookup failure message, or `None`. Distinct from an empty `candidates`         |

`candidates == []` with `error is None` means "looked, found nothing".
`error is not None` means "could not look". These must never render the same.

**`confirm` argument exclusivity.** Exactly one of `doi`, `pick`, `cite`, or
`own_work=True` may be supplied; supplying zero or more than one raises
`ValueError`. `adapted_from` and `note` are modifiers and may accompany any of them.

**`skip` is defer, not delete.** It moves the item to the back of the queue for
this server run and does not touch disk. Nothing in the UI deletes a capture:
an unresolved capture still carries its context line, which is the minimum this
project promises. Deleting stays a manual filesystem act.

## Screen 1 - Pending

One card per unresolved capture, newest first:

- the thumbnail (deciding by looking is the point)
- the context line - app, window title, timestamp - rendered **even when nothing
  resolved**, because that is the "at least some track of where I copied this
  from" case that motivated the project
- candidates as radio options, each tagged with its origin (Zotero / CrossRef /
  DOI-in-URL) plus score, title, container, year, type
- actions: Confirm selected | Enter a DOI by hand | This is my own work | Skip

When the automatic route already grounded a DOI, the card says so _and shows the
evidence_ ("DOI in URL", "citation_doi meta tag"). The UI never hides why
something is trusted.

**Hard rule:** nothing on this screen auto-confirms. Confirmation is a click.

## Screen 2 - Deck

Input is a typed/pasted path to a `.pptx` or `.pdf` (`pdfdeck` already handles
PDFs). Deliberately not an upload: the file lives on the Windows side, the
server is local, and a copy could silently drift from the deck actually
presented.

Grid of every picture: slide number, thumbnail, match status (ok / unconfirmed /
no source), how it matched (sha256, or dhash with distance), and the licensing
badge - `public-domain`, `reuse-ok-attribution-required`,
`reuse-ok-share-alike-attribution-required`, `noncommercial-only`,
`restricted-no-derivatives`, `publisher-terms-check-required`,
`unknown-ask-publisher` - plus a loud RETRACTED flag.

Header mirrors `audit`: N pictures, N with provenance, N unconfirmed, N
substantive but unsourced. The last is the number that matters.

Unsourced pictures resolve inline with the same control as screen 1. Apply
writes `<deck>.cited.pptx` and never overwrites the input.

## Data flow

```
browser -> 127.0.0.1 -> web.py route -> service.py
        -> clipboard / store / deck / crossref / zotero -> JSON
```

Thumbnails are a separate endpoint returning image bytes keyed by **opaque ref,
never a caller-supplied path**.

## Error handling and safety

- **Fail loud.** A CrossRef/Zotero/network failure surfaces the actual error on
  the card. "No candidates found" and "the lookup failed" must never render
  identically.
- Binds `127.0.0.1` only. No auth, because single-user local - a deliberate,
  documented choice rather than an omission.
- **Refuses to start if the port is taken** rather than quietly selecting
  another. Two servers writing one manifest is a corruption path.
- Apply never overwrites its input.
- Unconfirmed never prints a citation, and `--allow-unconfirmed` gets no UI
  equivalent in v1.
- The thumbnail endpoint accepts opaque refs only, so the browser cannot walk
  the filesystem.

## Testing

- **Service layer:** tests first, then move the logic. Existing CLI tests are
  the regression net and must stay green **without edits**. If a CLI test needs
  changing to pass, the refactor changed behavior - stop and report.
- **Web layer:** drive a real server on an ephemeral port with a real HTTP
  client. Not mocked handlers. Real execution at the boundary.
- **Central guard test, to be seen failing first:** clicking Confirm on a
  selected candidate produces a confirmed record; a candidate merely _displayed_
  produces nothing. Rendering a screen must not be able to confirm anything.
  Break the guard deliberately, confirm the test goes red for that reason, then
  restore.
- Every negative assertion ships a positive control in the same test, so a
  broken harness cannot masquerade as "safely blocked".
- **Visual output is not self-verifiable.** Builder bias on rendered UI is a
  perception failure. Both screens need either the user's eyes or a fresh
  critic before SP1 is called done.

## Success criteria

1. `figcite ui` starts a server on 127.0.0.1 and prints the URL.
2. A pending capture can be resolved end to end in the browser, and the
   resulting Record is equal, field for field, to what the CLI's `confirm`
   would have produced for the same choice - excluding `captured_utc` and
   `captured_local`, which are wall-clock stamps and cannot match. This is a
   real test, asserted against both front ends over one fixture, not an
   aspiration.
3. A real `.pptx` audits in the browser with correct per-slide status and
   licensing badges, matching `figcite audit` output for the same file.
4. Apply from the browser produces a deck PowerPoint opens without repair.
5. The full unit suite passes, CLI tests unmodified.
