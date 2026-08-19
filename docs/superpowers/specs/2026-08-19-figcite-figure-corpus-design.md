# figcite figure corpus (SP2): reverse sourcing and duplication

Date: 2026-08-19
Status: approved, not yet implemented

Answers two questions off one index:

- **Reverse sourcing** — "this figure has no source; where did it come from?"
- **Duplication** — "has this figure appeared somewhere other than the paper
  I credited?"

SP1 (service layer + web UI) shipped and is the consumer: matches surface as
candidates in the pending card's existing radio list.

## What was measured first

Every load-bearing choice below was decided by running something, and two of
them reversed the intuitive answer. The measurements are recorded because a
future reader will otherwise re-propose the rejected designs.

**Figure retrieval works; page retrieval does not.** Panel-crop queries against
a 16-figure corpus with decoys from three unrelated papers: **7/7 correct,
median margin 38.9x** (range 3.5x-283x). The same technique against rendered
PDF _page_ rasters put the correct page outside the top 6 on raw match counts,
and first by only **1.5x** after RANSAC — with a page from an unrelated CRISPR
paper scoring 14 against the truth's 21. A page crop is mostly text, text
corners look alike everywhere, and they swamp the figure's own features.

The unit of indexing is therefore the **figure**, not the page or the article.
That is the whole reason to use PMC: not corpus size, but that PMC has already
done the segmentation, and each figure arrives with a caption, a DOI, and a
licence.

**dhash cannot survive a crop.** Against figcite's own `fuzzy_distance = 6`
(`deck.py:93`), measured on a real figure: re-save 0, rescale to 50% 0,
**crop 5% off each edge 7**, crop 10% 16, top half 13, top-left quadrant 24.
dhash is perfectly scale-invariant and dead at the mildest crop, so it cannot
carry reverse sourcing alone — but it is free and catches whole-figure snips,
so it stays as the first pass.

**Some figures cannot be decided at all.** One figure in the retrieval test
returned no usable keypoints: a smooth, low-texture panel, 100% non-white but
with only 2 ORB features against 592-1057 for its neighbours. The matcher must
report this as _could not decide_, never as _no match_.

**Corpus size.** 4,824 Zotero items → 535 with a DOI → sampled 60 → 73% found
in Europe PMC, **45% with a PMCID, 38% open access**. Extrapolated: **~241
papers with a PMCID, ~205 open access**, roughly 1,200 figures, ~100 MB, a
build of about ten minutes at polite request rates.

**Local PDFs are not a substitute.** Zotero stores exactly 1 PDF attachment on
this machine. There are ~357 PDFs in Downloads/Documents, but 12 sampled files
yielded 21 embedded images total (9 substantive) — most journal figures are
vector, so `page.get_images()` finds nothing. Extracting them would mean
building and validating a figure-region detector, which is the risky part PMC
removes.

## Non-goals

- No full-PMC index. The corpus is bounded by the user's own Zotero DOIs.
- No OCR.
- No auto-confirmation. A match is a candidate a human accepts, as everywhere
  else in figcite.
- Duplication reports; it never rewrites a record.
- No figure-region detector for local PDFs. Revisit only if the OA ceiling
  proves too low in practice.

## Architecture

Three new modules and one service entry point.

- **`figcite/pmc.py`** — fetching only. DOI → PMCID (Europe PMC REST search,
  batched), PMCID → figure list with captions (`fullTextXML`), PMCID → CDN
  image URLs (article page), image download. Throttled, no index knowledge.
- **`figcite/corpus.py`** — the index. `build()`, `status()`, `find(image)`,
  `duplicates_of(doi, image)`. Owns storage; knows nothing about HTTP.
- **`figcite/match.py`** — the two-stage matcher over image bytes. No I/O.
- **`service.whereis(ref_or_path)`** — the one entry point both front ends
  call, following the SP1 rule that neither front end reimplements an action.

### Where the images come from (revised during implementation)

The design said to scrape the article page for a CDN URL whose path contains an
opaque segment (`/blobs/9779/5383700/ffcd625bb91d/…`) that appears in no API
response. **That route is dead**: PMC answers a non-browser HTTP client with a
21 KB *"Checking your browser - reCAPTCHA"* interstitial. Neither a descriptive
User-Agent nor a full browser header set clears it; `curl` gets the real page
and `requests` does not, so the gate is below the header layer. Scraping the
human-facing site was the wrong instinct anyway.

**The route in use is the AWS Open Data programme**, which is what NCBI
publishes *for* programmatic access. One prefix per article version holds the
figure images beside the XML:

    PMC5383700.1/fpls-08-00491-g0001.jpg

Those names are exactly fullTextXML's `xlink:href` values, so the two line up
with no mapping; the URLs are constructible; and a plain `requests` GET returns
the same bytes as the CDN copy (verified: identical length, identical dhash
`8113132323031b07`). Measured end to end on PMC5383700: 6 figures enumerated,
6 URLs resolved, 6/6 fetchable, licence `CC BY`.

Two consequences worth keeping:

- **Article versions matter.** The prefix carries `.1`, `.2`, …; `s3_prefix`
  takes the highest, because indexing a superseded version's figures would be
  quietly wrong in exactly the way this project exists to avoid.
- **The fragile joint is gone.** There is no HTML parsing left in the fetch
  path, so PMC redesigning its site can no longer break the build.

The OA package tarball (`oa.fcgi` → `ftp://…/oa_package/….tar.gz`) was tried
before both of these: it 404s over https for every article tested, and over
FTP the advertised directory returns 550. Do not re-propose it.

### Storage

`FIGCITE_HOME/corpus/`:

- `figures.sqlite` — one row per figure: `pmcid`, `doi`, `label`, `caption`,
  `licence`, `source_url`, `dhash`, `width`, `height`, `image_path`,
  `fetched_at`.
- `images/<pmcid>/<figure>.jpg` — the bytes. Kept so queries are instant and
  work offline; ~100 MB at the measured corpus size.
- `descriptors/<pmcid>/<figure>.npy` — ORB descriptors, written only when
  opencv is importable. Absent is a valid state, not an error.

SQLite because a dhash scan and a "same figure, different DOI" join are natural
queries and it is stdlib. `licence` is stored because the OA record carries it
(`license="CC BY-NC-ND"` on a probed article), so a reverse-sourced figure
arrives with its reuse verdict already attached and feeds the existing badges.

## Build

`figcite corpus build [--limit N]`

1. Read the Zotero library, collect DOIs.
2. Batch Europe PMC search by DOI (8 per request) → PMCID, `isOpenAccess`,
   title, year.
3. For each open-access PMCID not already complete: `fullTextXML` → figure
   labels and captions; article page → href-to-CDN-URL map; download each
   image; compute dhash; compute ORB descriptors if opencv is present.
4. Record per-article outcome. A re-run retries only failures and new DOIs.

Politeness: single-threaded, <= 3 requests/second to NCBI (their documented
no-key limit), exponential backoff on 429 and 503. The build is resumable
because it is the only thing standing between the user and a ten-minute wait
they may interrupt.

## Matching

Two stages, always reported explicitly.

1. **dhash** — hamming <= 6 against the corpus. Catches whole-figure and
   rescaled snips at effectively zero cost.
2. **ORB + RANSAC** — Lowe ratio test at 0.75, then `findHomography` with
   RANSAC; the score is the **inlier count**. Raw match counts do not
   discriminate (they are what produced the 1.5x page-raster result); inliers
   do. Runs only when opencv is importable.

A third source of candidates, ranked strictly below any pixel match: the
**currently open browser tabs and recent history**, which `session_tabs.py`
already reads for the capture-tool fix. A tab whose URL yields a DOI is a lead
about where a figure came from, not evidence that it did, so it is offered with
`method="open-tab"` and never outranks a real pixel hit. It costs one function
call and covers the case the corpus cannot: a paper that is not open access.

`match.py` returns one of:

- `Match(figure, method, score, margin)` — ranked, never auto-accepted.
- `NoMatch()` — searched, nothing above threshold.
- `CouldNotDecide(reason)` — below the keypoint floor, or opencv absent and
  dhash found nothing (a crop is invisible to dhash, so "dhash says no" is not
  evidence of absence).

**These three must never collapse into one another**, in any layer, including
the UI. This is the defect class this codebase keeps re-finding.

When opencv is absent the tool says so on every query that dhash alone could
not settle. It must not silently degrade to "no match": a silent degrade is
indistinguishable from a real negative, which is the exact failure the
capture-tool bug had.

## Surfaces

Both front ends call `service.whereis()`; neither reimplements it.

- `figcite corpus build [--limit N]` / `figcite corpus status`
- `figcite whereis <image-path-or-ref>` — prints ranked candidates, each with
  its method, score, margin, and licence, plus the `figcite confirm` line that
  would accept one. Prints the `CouldNotDecide` reason when there is one.
- **Pending card**: matches appear in the candidate radio list already rendered
  there, tagged `open-tab`, `dhash`, or `orb` by the existing `source` badge —
  no new UI control.
- **Deck screen**: a figure whose match carries a different DOI than the one
  credited is flagged in the row as a possible duplicate. It reports; it never
  rewrites.

## Errors

- **DNS.** `cdn.ncbi.nlm.nih.gov` fails to resolve on this machine — SERVFAIL
  from both WSL and Windows resolvers, while Cloudflare DoH answers it
  (`34.110.206.50`). Every other NCBI host resolves. `pmc.py` must detect
  resolution failure and raise a named error saying which host failed and that
  the corpus is incomplete. Reporting "no figures found" would be a lie.
- Network, 429, and malformed-page failures are recorded per article and
  surfaced by `corpus status`, never swallowed.
- `corpus status` reports, for every Zotero DOI: indexed, not in PMC, not open
  access, or failed (with the reason). "Coverage" without the reason is the
  number that hides the bug.

## Testing

- **No PMC figures in the repository.** They are copyrighted, and at least one
  probed article is CC BY-NC-ND. Deterministic tests use synthetic images.
- **Every retrieval test ships decoys.** A two-candidate comparison measured
  283x separation and a proper retrieval test of the same technique on page
  rasters measured 1.5x — the first could not fail. Retrieval tests assert both
  the correct hit _and_ the margin over the runner-up.
- **One `live` test** fetches a known article end to end: the real boundary,
  including the scraped CDN URL, which no fixture can exercise.
- Each of `NoMatch` and `CouldNotDecide` is asserted with a positive control in
  the same test, so a broken harness cannot read as a clean negative.
- A test asserts opencv-absent behaviour by simulating the import failure, and
  that the resulting verdict is `CouldNotDecide`, not `NoMatch`.

## Success criteria

1. `figcite corpus build` indexes the open-access subset of the user's Zotero
   DOIs, is resumable, and reports per-DOI coverage with reasons.
2. A figure snipped from an indexed paper is retrieved by `figcite whereis`,
   as a candidate requiring confirmation.
3. A low-texture figure returns `CouldNotDecide`, not `NoMatch`.
4. With opencv uninstalled, a cropped query returns `CouldNotDecide` and says
   why; a whole-figure query still resolves via dhash.
5. A figure appearing in two indexed papers is reported as a duplicate, naming
   both DOIs.
6. A reverse-sourced match carries the licence from its OA record, so the
   existing reuse badge renders without a second lookup.
7. The full suite passes; SP1's tests are unmodified.

## Risks

- **The image route depends on an external bulk-distribution layout.** Much
  safer than the scrape it replaced -- no HTML parsing at all -- but the S3 key
  layout is still someone else's convention. Isolated to `s3_prefix` and
  `image_urls`, covered by the live test, and failures surface in
  `corpus status` rather than as an empty index.
- **The OA ceiling.** ~205 of ~535 DOIs are reachable. A figure from a
  paywalled paper can never be matched, and `whereis` must say that rather
  than implying the figure is unknown.
- **Open-tab candidates could be mistaken for evidence.** They are specified
  under Matching and rank below any pixel match, but a card showing one beside
  a real hit invites exactly that confusion. The `method` field must be visible
  wherever a candidate is rendered, not only in the CLI.
