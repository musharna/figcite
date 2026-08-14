# figcite

Keep DOI/citation provenance attached to an image from the moment you capture it
through to the slide it lands on — and out the other side as a credits slide and
a manifest.

## Why this exists

Provenance can ride along in three places, and they fail differently:

| Layer | Survives | Dies when |
|---|---|---|
| 1. Embedded in the image bytes (PNG `tEXt`/XMP, JPEG EXIF) | *Insert → Picture* | clipboard paste, "Compress Pictures" — anything that re-encodes |
| 2. Shape alt-text in the `.pptx` | edits, save/reopen, export to tagged PDF | someone deletes and re-inserts the picture |
| 3. Central manifest keyed by sha256 **and** perceptual dhash | everything above failing | the image is heavily cropped or redrawn |

`figcite` writes all three. Matching a slide image back to its source tries them
in that order; a dhash match is reported as fuzzy and treated as unconfirmed.

## The rule that shapes the design

**A guessed citation never reaches a slide.** DOIs read out of the PDF you
cropped from are grounded and get marked confirmed. DOIs inferred from a window
title are not, and stay behind `figcite pending` until you pick one.

This isn't hypothetical caution. Asking CrossRef for the exact title
*"Array programming with NumPy"* returns a **review of** that paper as the top
hit, not the paper — score 37.2, ahead of everything else. A tool that
auto-accepted the top hit would have put the wrong citation on a slide with full
confidence. `tests/test_live.py::test_crossref_title_search_is_untrustworthy_by_design`
pins that behaviour so the policy can be revisited if CrossRef ever improves.

## The four ways an image arrives

```bash
# 1. Snip / screenshot to clipboard  (Win+Shift+S)
figcite watch                     # leave running; catches every image you copy
                                  # snips from a browser or a local PDF are
                                  # GROUNDED and filed automatically -- no step
figcite pending                   # only what could NOT be grounded lands here
figcite confirm 0 --doi 10.3390/horticulturae6040087
figcite confirm 0 --pick 1        # or accept a listed candidate

# 2. A figure inside a paper PDF  -- DOI is read from the PDF itself
figcite images paper.pdf --page 3                    # list embedded figures + bboxes
figcite grab paper.pdf --page 3 --image-index 0 -o fig.png
figcite grab paper.pdf --page 3 --rect 84,126,505,730 --dpi 300 -o fig.png
figcite grab paper.pdf --page 3 --rect 0.1,0.1,0.9,0.5 --frac   # fractions of the page

# 3. Your own generated plots
python -c "
import figcite.mplhook as fc
fc.savefig(fig, 'out.png', cite='This work', dataset='rnaseq_v3')  # stamps git commit too
"

# 4. A file you downloaded
figcite tag downloaded.png --doi 10.1111/mec.12953
figcite tag screenshot.png --url https://example.org/page --cite "Example Org, 2026"
```

Insert the **tagged** file into your deck (not the original), then:

```bash
figcite audit  deck.pptx                     # coverage report, changes nothing
figcite apply  deck.pptx -o deck.cited.pptx  # alt-text + captions + credits + manifest
```

`apply` is idempotent — re-running replaces its own captions and credits slide
rather than stacking a second copy.

## Browser snips are grounded automatically

The watcher records the foreground window title at snip time. For a browser that
title joins exactly onto a row in the browser's own history, giving the URL of
the page that was on screen; the DOI then comes from that URL. Because the URL
is the address of the document rather than an inference about which paper was
meant, it can be auto-confirmed and filed with no interaction.

Four resolution paths, tried in order, each verified against CrossRef:

1. **The DOI is in the URL** (`/doi/10.1111/nph.71477`) -- offline, instant.
   Publisher tails like `/full`, `.pdf`, `/abstract` and `v2` are stripped.
2. **A known publisher URL pattern** (`nature.com/articles/s41598-…` → `10.1038/…`).
3. **A publisher article ID**: Elsevier/Cell PII, resolved through CrossRef's
   `alternative-id` filter. This matters because ScienceDirect, OUP and Wiley
   return **403** to an automated page fetch -- the CrossRef route works anyway.
   Cell Press punctuates the PII (`S1674-2052(18)30156-4`) and Elsevier does not;
   both normalise to the same identifier.
4. **PubMed/PMC identifiers**, via NCBI (`esummary` for a PMID, the ID converter
   for a PMCID).
5. Failing all of those, the page's own `<meta name="citation_doi">` -- which
   only works on publishers that serve bots (Nature and PLOS do; OUP, Wiley and
   bioRxiv do not).

Measured against a real 82-page reading history: **61% auto-grounded without any
publisher page fetch** (34% from the URL alone, 27% via publisher/PubMed IDs).
Several of the remainder are journal homepages and GEO accession pages that
legitimately have no DOI.

**Only an exact, unambiguous title match grounds a capture.** If the title is not
in history, figcite falls back to the visit nearest the capture time -- and that
is a guess about which tab was showing, so it stays unconfirmed and goes to
`figcite pending`. Private-browsing windows leave no history and always land
there too.

**A failed lookup is never reported as an absence of provenance.** CrossRef
allows one request per second; a loop that trips that limit used to return
"no DOI" for perfectly resolvable papers. Lookups are now throttled, and a
failure surfaces as `LOOKUP FAILED … retry` rather than silently filing the
image as unsourced.

## What `apply` writes

- **Alt-text** on every picture: full citation, DOI, license, reuse verdict.
- **A small grey caption** under each picture: `[1] Shiragaki et al. 2020 · doi:…`
  (`--no-captions` to skip).
- **A numbered "Image credits" slide** at the end, paginated at 8 entries per
  slide (`--no-credits` to skip).
- **`deck.cited.csv` / `.json`** — one row per picture, including the ones with
  no source.

Images with no recorded provenance are **named on the credits slide**, not
silently dropped: `⚠ 1 image(s) on slide(s) 2 have no recorded source.` Pictures
under 1 inch in both dimensions are treated as decorative and exempt
(`--min-inches`).

## PDF output: Affinity, Illustrator, InDesign, Slides, LaTeX

`audit` and `apply` take a `.pdf` as well as a `.pptx`, so anything that exports
PDF is covered without parsing a proprietary document format:

```bash
figcite audit  board.pdf                     # coverage report, changes nothing
figcite apply  board.pdf -o board.cited.pdf  # captions + credits page + manifest
```

Measured behaviour of a PDF export: it **strips embedded image metadata** and
**re-encodes the pixels**, so layers 1 and 2 are both gone. The perceptual hash
survives -- on two real figures from one paper, the exported copy matched the
correct figure at hamming 0 and the other at 23. Recovery therefore runs entirely
through layer 3, which is why the manifest matters more here than anywhere else.

In Affinity specifically, keep placed images **linked** rather than embedded. The
Resource Manager then shows every image's path, the files keep their own metadata
and sidecars, and provenance never depends on hashing at all.

Not verified against a real Affinity export -- the PDF round-trip above was
produced with PyMuPDF. Affinity's own exporter has its own DPI and compression
settings, and aggressive downsampling could in principle push the hash past
threshold.

## Nothing is ever lost

Every clipboard capture is filed, whether or not a citation could be established:

- **Grounded** (a DOI from the page's URL, or from the PDF the snip came from) ->
  filed confirmed, with the full citation and license.
- **Everything else** -> filed *unconfirmed*, carrying what was actually observed:
  which app was in front, what the window title said, what URL was open, and when.

That second case is the point of the design. A screenshot with no resolvable DOI
still knows it came from Firefox showing a particular page at a particular minute,
and an audit reports that instead of "no source recorded". It is a trail, not a
citation, and it is stored as unconfirmed so nothing downstream can print it as
one -- credits show the capture context and withhold the guess.

`figcite pending` lists those filed-but-unresolved captures so you can attach a
DOI later:

```bash
figcite pending
figcite confirm m0 --doi 10.1111/nph.71477
```

## Licensing

Every record carries the publisher's license URL from CrossRef and a
conservative reuse verdict: `public-domain`, `reuse-ok-attribution-required`,
`reuse-ok-share-alike-attribution-required`, `noncommercial-only`,
`restricted-no-derivatives`, `publisher-terms-check-required`, or
`unknown-ask-publisher`. Nothing is assumed reusable by default. CrossRef's
retraction flag is checked too, and a retracted source is labelled as such.

Use `--adapted-from <DOI>` when the figure you cropped was itself reproduced
from an earlier paper — the PDF's own DOI cannot tell you that.

## Tests

```bash
python3 -m pytest tests/ -q -m "not live"   # 11 tests, no network
python3 -m pytest tests/ -q -m live         # real CrossRef, real PDF, real clipboard
```

The live tests drive the actual system boundaries. The clipboard test overwrites
your clipboard with a small test bitmap while it runs.

## Known limits

- **Clipboard paste strips layer 1.** If you paste rather than insert, the image
  bytes are re-encoded and only the dhash fallback can recover the source.
  Insert the tagged file from disk when you can.
- **JPEG can't hold the structured record** — only the human-readable citation
  goes into EXIF; the rest lives in the sidecar and manifest.
- **Verified against python-pptx, not Microsoft PowerPoint.** The `.pptx` written
  here round-trips correctly through python-pptx and unzips as expected; driving
  real PowerPoint via COM was not run.
- The watcher deliberately ignores whatever is already on the clipboard when it
  starts, because the focused window at that moment is not where the image came
  from. `-CaptureExisting` opts in.
