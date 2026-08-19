# Changelog

Notable changes to figcite. Backfilled at the first entry; no releases are
tagged yet, so sections are dated by the commit that closed the milestone.

## Unreleased — web UI and service layer (2026-08-18)

Branch `feat/web-ui`, 35 commits. Suite 130 -> 258 passing.

### Added

- `figcite/service.py` — one implementation of "attach a citation to an
  image", consumed by both front ends. `pending_items`, `confirm`, `skip`,
  `audit`, `apply`, `thumbnail`. Structured returns; no printing, no argparse,
  no HTTP.
- `figcite ui [--open]` — a loopback web UI with two screens: **Pending**
  (thumbnail, context line, candidates as radio options, confirm/own-work/skip)
  and **Deck** (per-picture match status, how it matched, licensing badge,
  RETRACTED flag, apply).
- `figcite/web.py` — stdlib `ThreadingHTTPServer` JSON API bound to 127.0.0.1,
  with `Host` and `Origin` allowlists on every verb and a per-route field
  allowlist. Refuses to start if the port is taken rather than selecting
  another; two servers writing one manifest is a corruption path.
- `crossref.REUSE_VERDICTS` — the reuse verdicts as one enumerable set, with a
  membership assertion in `classify_reuse`.
- A parity test asserting the browser and the CLI produce field-identical
  records for the same choice, so the two doors cannot drift.

### Changed

- `cmd_pending` and `cmd_confirm` are printers over `service.py`. Every other
  `cmd_*` is untouched and the pre-existing CLI tests passed unmodified.
- `slug`, `library_dest`, `finalize`, `record_for` moved to `figcite/_actions.py`
  with printing stripped out.
- The push gate (`.githooks/pre-push`) now covers the web server: 210 -> 239
  non-live tests.

### Fixed

- **Arbitrary file write via `/api/confirm`.** Client JSON was splatted into
  `service.confirm(**payload)`, exposing the `out=` destination path; a POST
  overwrote the target and returned `{"ok": true}`. Fixed by removing the splat
  shape, not by filtering the one field.
- **DNS-rebinding read exposure.** GET routes sat outside the Origin guard with
  no `Host` check, so a rebound page could read capture titles and figure bytes.
- **Every licence badge rendered blank** on a real deck: `Record.reuse` defaults
  to a sentinel `classify_reuse` never emits, and blank reads as "fine". Badge
  fallback is now structural.
- **`confirm` could file an uncited record.** With no `--doi`, `--pick`, or
  `--cite`, the staged capture was cleared while nothing citable was written.
- Self-inflicted serialization: `confirm` held a write lock across a CrossRef
  call, blocking unrelated requests for up to 25s. The lock now covers state
  mutation only.

### Deferred

- **SP2** — Europe PMC figure index for reverse sourcing and duplication
  detection.
- **SP3** — deck-usage index ("where have I used this figure").
- Deck-screen inline resolve; "newest first" pending order; per-test
  `FIGCITE_HOME` isolation.

## 0.1.0 (initial development)

- DOI/citation provenance carried from screen capture through to slides:
  embedded PNG `tEXt`/XMP and JPEG EXIF, a sidecar `<image>.figcite.json`, and
  a central manifest keyed by both sha256 and perceptual dhash.
- Automatic grounding for browser snips: foreground window title -> browser
  history row -> URL -> DOI -> CrossRef, with Zotero consulted before CrossRef.
- `audit` and `apply` over `.pptx` and PDF decks; apply never overwrites its
  input.
- A standalone clipboard watcher, one per staging dir, that waits for the
  capture sidecar rather than trusting the announced path.
- Register existing images without modifying them; self-register generated
  plots; hand off to `ghostcite`.
