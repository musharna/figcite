# Changelog

Notable changes to figcite. Sections are dated by the commit that closed the
milestone. `v0.1.0` was tagged after the three milestones below had already
landed, so 0.2.0 is the first tag whose notes match its contents.

## 0.2.2 (2026-09-11)

First release on PyPI; the repository goes public with it.

### Packaging

- PyPI metadata: long description from the README, author, classifiers,
  project URLs. README images point at absolute GitHub URLs so PyPI renders
  them; install lines are `pip install figcite`.
- The pre-push hook and README no longer explain themselves in terms of a
  private repo's Actions quota; the hook stays because it is the right shape
  for a one-developer project, not because minutes cost money.

## 0.2.1 (2026-09-10)

### Fixed

- **The deck screen showed "(image unavailable)" for every figure matched by
  its embedded metadata** -- the most common match a tagged deck produces.
  `embed` serialises the record into the PNG and only then hashes the finished
  file, so the copy inside the image has `sha256 == ""`; `read_embedded`
  returned it verbatim and the UI built a thumbnail ref of `sha:` with nothing
  after the colon. The reader now stamps the blob's own hashes on the record,
  as the JPEG branch always did. Found while screenshotting the UI for the
  README; the same demo deck audited "(image unavailable)" on its one sourced
  slide.

### Docs

- README rebuilt around the user's path, with a command reference checked
  against `--help`, two diagrams, six screenshots shot against an isolated demo
  home, and a CC BY credit for the one borrowed figure.

## 0.2.0 (2026-09-10)

Suite 1203 non-live + 30 live.

### Added

- **`figcite dismiss`** — a persistent terminal state for a capture that is
  NOT attributable, with a required reason; `--undo` restores it. Every other
  verb attaches provenance, so a stray snip of a blank region had no way out
  of `pending`. Dismissal sits beside `confirmed`, never inside it: a dismissed
  record carries no citation and can never read as sourced.
- `figcite --version`, read from the installed package metadata so the
  version is declared once, in `pyproject.toml`.
- `LICENSE` (MIT) and the matching `license` field; a repo with no license file
  is not permissive by default.

### Fixed

- **A featureless image was a confident false dhash match.** A flat fill or a
  monotonic gradient hashes to all zeros, which sits close to many corpus
  hashes. `corpus.can_compare_dhash` existed and was correct but was never
  called at two of the three sites where a distance becomes a verdict
  (`match.by_dhash`, `store.find_similar`). Both now decline instead of naming
  a figure at random. The test puts a blank row in the CORPUS as well as the
  query, because an all-textured corpus made the first version pass against
  the broken code.
- 15 pyright and 7 ruff findings; both now run in the pre-push gate. One was
  real: `register --this-work` typed its detail dict as `str`-valued, so the
  deliberate `git_commit: None` for "git did not answer" was a type error.

### Not a bug, checked

- A snip from the Microsoft Teams web app produced the CrossRef candidate
  "Introduction: Microsoft Teams" (score 37.3). Audited as junk; it is not.
  That is a real Apress book chapter whose title exactly matches the tab
  title, offered as a candidate the user must pick explicitly, which is the
  contract. The number beside it is CrossRef's unnormalised relevance score
  and says nothing about whether the page was a paper. Left as is: the
  discriminator that would help is the tab's URL, not its title, and a
  domain list is the open-set guard this file already refuses to carry.

### Milestones folded into 0.2.0

### `whereis` in the browser (2026-08-20)

Branch `feat/whereis-web`. Suite 383 -> 404 non-live + 28 live.

#### Added

- **A "Where is" tab in `figcite ui`.** `service.whereis()` was built as the
  one entry point both front ends call and only the CLI ever called it, while
  its sibling `service.duplicates()` did reach the browser. The asymmetry was
  a stop, not a policy.
- `POST /api/whereis`, allowlisted to `{"ref"}` through `_fields` like every
  other route. Read-only, so it does not take `_LOCK`.
- `tests/test_webui_whereis_browser.py` — the screen driven by a real
  chromium. The node tests execute the renderer and can say nothing about
  whether the page ever calls it: a tab that never reveals its section, or a
  button never wired to a listener, passes all of them. Skipped when
  playwright is absent, matching how the node tests skip without node.

#### Fixed

- **The CLI dropped its leads on a no-match, and mixed them in on a match.**
  `service.whereis` returns open tabs on EVERY verdict — it ranks them last,
  it does not withhold them — but `cmd_whereis` only read `matches` inside its
  `match` branch. So a no-match discarded leads the service had just produced,
  while a match listed them as numbered items beside the pixel hit, under a
  heading inviting the user to cite any of them by DOI. Both front ends now
  separate evidence from leads, print leads on all three verdicts, and offer
  `confirm` only when something was actually found.

  An unlabelled candidate falls to the lead side: promoting an unknown thing
  to evidence is the error that costs something.
- **A score with no unit, and half the time the wrong one.** `by_dhash`
  scores a hamming distance (0 is perfect); `by_orb` scores inlier count
  (higher is better). Both front ends printed the bare number, so the same
  digit meant opposite things depending on the matcher. `Match` now carries a
  `score_label` written by the matcher that produced it — "hamming 0",
  "71 inliers" — and the guard is an AST check that every `Match(...)` built
  in `match.py` supplies one, so a matcher added later cannot inherit
  whichever unit a front end hard-coded.
- **A dhash score of 0 rendered blank.** `esc(m.score || "")` — and a dhash
  score is a hamming distance, so 0 is a perfect match, the strongest result
  the screen can report. Found by putting the real corpus through the real
  route; every unit test passed because they all asserted on the DOI.

#### Notes

- `service.whereis()` now marks each candidate `evidence: true|false`. The
  browser must not derive that from `source in {"dhash","orb"}` — a list of
  names standing in for an open set, where adding a third matcher silently
  demotes its every hit to a lead.
- Open tabs render in their own block, below the pixel matches and labelled
  as leads. They are returned even when the corpus search found nothing, so a
  screen that listed them together would dress an absence as an answer.
- Eight mutants, all killed: collapsing evidence and leads into one list,
  no-match wearing the could-not-decide wording, a reason-less
  could-not-decide, a tab claiming to be evidence, an unwired button, a
  `show()` that does not know the section, a blanked failure box, and the
  score fix reverted.

### Figure corpus and reverse sourcing (2026-08-20)

Branch `feat/figure-corpus`, 17 commits. Suite 258 -> 383 non-live + 28 live.

#### Added

- `figcite corpus build` / `corpus status` — indexes the open-access figures
  of the papers already in your library via Europe PMC and the PMC Open Data
  bucket, with captions and licences. Every DOI produces an outcome
  (`indexed`, `not-in-europe-pmc`, `no-pmc-copy`, `not-open-access`,
  `failed`), because a coverage count without reasons hides the bug. Measured
  on a 535-DOI library: 226 indexable (42%).
- `figcite whereis <image>` — which paper is this figure from? dhash first,
  ORB second for crops, open browser tabs last and always ranked below any
  pixel match. Answers `match` / `no-match` / `could-not-decide`, never two of
  the three, and a `could-not-decide` always carries its reason.

  Measured against figures fetched from an INDEPENDENT source (the publisher's
  own render, not the bytes already in the index): **whole figures 8/8
  correct, 4/4 correctly declined; cropped panels 2/12.** Crop retrieval does
  not work and is not claimed to.

  An earlier draft of this entry read "11 matched, 1 declined, 0 wrong" for
  crops. That measurement was circular: it cropped the corpus's own bytes, so
  query and index sat at a scale ratio of 1.0 — the one condition under which
  the failure cannot appear. Retracted rather than quietly deleted, because
  the number was published here and why it was wrong is the useful part.

- `figcite/corpus.py`, `figcite/match.py`, `figcite/pmc.py`.
- ORB descriptors _and keypoints_ cached at build time — 4.6x faster queries.
  Keypoints matter: RANSAC cannot fit a homography without the coordinates,
  and they are not recoverable from descriptors.
- Duplicate detection on the deck audit — a figure credited to one paper that
  also appears under another DOI is flagged on its row. Reports only.
- `tests/test_pmc_live.py` — the real Europe PMC and S3 boundary, the only
  test that would notice PMC changing its published layout.

#### Fixed

- **One failing request no longer erases every other answer.** A single
  transient 504 on batch 3 of 67 made all 535 DOIs report `failed`, because
  the error boundary enclosed the whole lookup loop rather than one request.
  `lookup_dois` now survives a bad batch and names the DOIs it could not
  reach, and those are reported `failed`-with-reason, never
  `not-in-europe-pmc` — an outage must never read as an absence.
- Bounded retries on transient HTTP codes (429/500/502/503/504) only.
- `python -m figcite.cli` printed nothing and exited 0.

#### Notes

- `opencv` is optional (`pip install 'figcite[match]'`). Without it, crops
  cannot be matched; everything else works.
- Mutation testing found that the two featureless-image guards each masked the
  other, so removing either left the suite green. Their real coverage is the
  mirrored near-flat cases, now pinned.

### Web UI and service layer (2026-08-18)

Branch `feat/web-ui`, 35 commits. Suite 130 -> 258 passing.

#### Added

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

#### Changed

- `cmd_pending` and `cmd_confirm` are printers over `service.py`. Every other
  `cmd_*` is untouched and the pre-existing CLI tests passed unmodified.
- `slug`, `library_dest`, `finalize`, `record_for` moved to `figcite/_actions.py`
  with printing stripped out.
- The push gate (`.githooks/pre-push`) now covers the web server: 210 -> 239
  non-live tests.

#### Fixed

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

#### Deferred

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
