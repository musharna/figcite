# Changelog

Notable changes to figcite. Backfilled at the first entry; no releases are
tagged yet, so sections are dated by the commit that closed the milestone.

## Unreleased — figure corpus and reverse sourcing (2026-08-20)

Branch `feat/figure-corpus`, 17 commits. Suite 258 -> 383 non-live + 28 live.

### Added

- `figcite corpus build` / `corpus status` — indexes the open-access figures
  of the papers already in your library via Europe PMC and the PMC Open Data
  bucket, with captions and licences. Every DOI produces an outcome
  (`indexed`, `not-in-europe-pmc`, `no-pmc-copy`, `not-open-access`,
  `failed`), because a coverage count without reasons hides the bug. Measured
  on a 535-DOI library: 226 indexable (42%).
- `figcite whereis <image>` — which paper is this figure from? dhash first,
  ORB second for crops, open browser tabs last and always ranked below any
  pixel match. Answers `match` / `no-match` / `could-not-decide`, never two of
  the three, and a `could-not-decide` always carries its reason. Measured end
  to end on a real six-figure paper at two crop positions each: 11 matched,
  1 declined, 0 wrong.
- `figcite/corpus.py`, `figcite/match.py`, `figcite/pmc.py`.
- ORB descriptors *and keypoints* cached at build time — 4.6x faster queries.
  Keypoints matter: RANSAC cannot fit a homography without the coordinates,
  and they are not recoverable from descriptors.
- Duplicate detection on the deck audit — a figure credited to one paper that
  also appears under another DOI is flagged on its row. Reports only.
- `tests/test_pmc_live.py` — the real Europe PMC and S3 boundary, the only
  test that would notice PMC changing its published layout.

### Fixed

- **One failing request no longer erases every other answer.** A single
  transient 504 on batch 3 of 67 made all 535 DOIs report `failed`, because
  the error boundary enclosed the whole lookup loop rather than one request.
  `lookup_dois` now survives a bad batch and names the DOIs it could not
  reach, and those are reported `failed`-with-reason, never
  `not-in-europe-pmc` — an outage must never read as an absence.
- Bounded retries on transient HTTP codes (429/500/502/503/504) only.
- `python -m figcite.cli` printed nothing and exited 0.

### Notes

- `opencv` is optional (`pip install 'figcite[match]'`). Without it, crops
  cannot be matched; everything else works.
- Mutation testing found that the two featureless-image guards each masked the
  other, so removing either left the suite green. Their real coverage is the
  mirrored near-flat cases, now pinned.

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
