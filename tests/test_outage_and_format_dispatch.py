"""Two guards at the centre of the tool, neither of which any test drove.

From the GUARD tier of the full survivor triage (199 of 1,807 survivors sit
in a condition rather than in a message).

1. `crossref.throttled_get` -- `if r.status_code == 429`. BOTH the operator
   (`Eq -> NotEq`) and the literal (`429 -> 430`) survived.

   This is the exact defect class that has already shipped in this project
   once: a CrossRef 429 folded into an absence. Inverted, every **200**
   triggers the back-off-and-retry path while a real 429 passes straight
   through unretried -- so the tool hammers the API when it is healthy and
   gives up the moment it is throttled. Nothing noticed.

2. `provenance.embed` -- `if fmt == "PNG" or dst.lower().endswith(".png")`
   and the JPEG arm below it. Both `Or -> And` mutants survived, as did the
   `.png` / `.jpg` / `.jpeg` literals.

   This chooses which provenance MECHANISM is used: PNG tEXt+XMP, or JPEG
   EXIF. Under `and`, a JPEG source written to a `.png` destination stops
   taking the PNG arm and falls into the JPEG arm -- producing a file named
   `.png` that contains JPEG bytes with EXIF. The layer this whole tool rests
   on, picked by an untested condition.

Every test asserts the mechanism actually used (the bytes on disk, the call
made), not merely that the call returned -- a record that round-trips through
`read_embedded` proves nothing about which arm wrote it if both arms happen
to write something readable.
"""

from __future__ import annotations


import pytest
import requests
from PIL import Image

from figcite import crossref
from figcite.provenance import Record, embed, read_embedded


# --- crossref: a 429 is an outage, and only a 429 ------------------------


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return {"message": {"DOI": "10.1/x"}}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")


@pytest.fixture
def no_sleeping(monkeypatch):
    """The retry path sleeps for real. Record the naps instead of taking
    them, so the test is fast AND can assert that a back-off happened."""
    naps: list[float] = []
    monkeypatch.setattr(crossref.time, "sleep", lambda s: naps.append(s))
    monkeypatch.setattr(crossref, "_last_call", 0.0, raising=False)
    return naps


def _responses(monkeypatch, *seq):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return seq[min(len(calls) - 1, len(seq) - 1)]

    monkeypatch.setattr(crossref.requests, "get", fake_get)
    return calls


def test_a_429_backs_off_and_retries_once(monkeypatch, no_sleeping):
    """THE finding. Nothing in the suite drove a 429 through this function."""
    calls = _responses(
        monkeypatch,
        FakeResponse(429, {"retry-after": "3"}),
        FakeResponse(200),
    )

    r = crossref.throttled_get("https://api.crossref.org/works/10.1/x")

    assert len(calls) == 2, f"a 429 was not retried: {len(calls)} request(s)"
    assert r.status_code == 200, r.status_code
    assert 3.0 in no_sleeping, f"retry-after was not honoured; slept {no_sleeping}"


def test_a_200_is_not_retried(monkeypatch, no_sleeping):
    """The asymmetric half, and the one the mutation actually breaks.

    Inverted (`!= 429`), a healthy 200 takes the back-off path: the tool
    sleeps and re-requests every single successful call. That is invisible
    to a test that only ever checks the returned object.
    """
    calls = _responses(monkeypatch, FakeResponse(200))

    r = crossref.throttled_get("https://api.crossref.org/works/10.1/x")

    assert len(calls) == 1, (
        f"a healthy response was retried {len(calls)} times -- the rate-limit "
        "branch is firing on success"
    )
    assert r.status_code == 200
    assert not no_sleeping, f"the tool slept on a 200: {no_sleeping}"


def test_a_404_is_not_treated_as_a_rate_limit(monkeypatch, no_sleeping):
    """A miss is not an outage. 404 must not enter the back-off path."""
    calls = _responses(monkeypatch, FakeResponse(404))

    r = crossref.throttled_get("https://api.crossref.org/works/10.1/nope")

    assert len(calls) == 1, f"a 404 was retried as though throttled: {len(calls)}"
    assert r.status_code == 404
    assert not no_sleeping, no_sleeping


def test_a_persistent_429_is_still_a_429_and_not_a_miss(monkeypatch, no_sleeping):
    """After the single retry the response is returned as-is.

    It must stay a 429 -- the caller raises on it -- rather than being
    smoothed into something a caller could mistake for "no such work". An
    outage that reads as an absence is the one error this tool cannot make.
    """
    _responses(monkeypatch, FakeResponse(429, {"retry-after": "1"}))

    r = crossref.throttled_get("https://api.crossref.org/works/10.1/x")

    assert r.status_code == 429, (
        f"a still-throttled response came back as {r.status_code}"
    )
    with pytest.raises(requests.exceptions.HTTPError):
        r.raise_for_status()


def test_an_unparseable_retry_after_still_backs_off(monkeypatch, no_sleeping):
    """`float(...)` is wrapped in try/except for this. A malformed header
    must not skip the back-off entirely."""
    _responses(
        monkeypatch,
        FakeResponse(429, {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}),
        FakeResponse(200),
    )

    crossref.throttled_get("https://api.crossref.org/works/10.1/x")

    assert no_sleeping, "a malformed retry-after skipped the back-off"


# --- provenance: which mechanism writes the record -----------------------


REC = Record(
    doi="10.3390/horticulturae6040087",
    citation="Shiragaki et al. (2020). Horticulturae 6: 87.",
    short_cite="Shiragaki et al. 2020",
    confirmed=True,
    source_kind="pdf-crop",
)


def _make(path, fmt):
    im = Image.new("RGB", (64, 48), (120, 90, 200))
    im.save(path, fmt)
    return path


def _format_on_disk(path):
    with Image.open(path) as im:
        return (im.format or "").upper()


def test_a_png_written_to_a_png_keeps_the_png_mechanism(tmp_path):
    src = _make(tmp_path / "in.png", "PNG")
    dst = tmp_path / "out.png"

    embed(src, dst, REC)

    assert _format_on_disk(dst) == "PNG", _format_on_disk(dst)
    got = read_embedded(dst.read_bytes())
    assert got and got.doi == REC.doi, got


def test_a_jpeg_written_to_a_jpg_keeps_the_jpeg_mechanism(tmp_path):
    src = _make(tmp_path / "in.jpg", "JPEG")
    dst = tmp_path / "out.jpg"

    embed(src, dst, REC)

    assert _format_on_disk(dst) == "JPEG", _format_on_disk(dst)
    got = read_embedded(dst.read_bytes())
    assert got is not None and REC.citation in (got.citation or ""), got


def test_a_jpeg_written_to_a_png_destination_is_actually_a_png(tmp_path):
    """THE finding, and the case only the `or` half reaches.

    Source format and destination extension DISAGREE. The `or` says the
    destination wins: a `.png` name gets PNG bytes and the tEXt mechanism.
    Under `and` this falls through to the JPEG arm and writes EXIF-carrying
    JPEG bytes into a file called `.png` -- a file whose name lies about its
    contents, which every downstream reader then has to cope with.
    """
    src = _make(tmp_path / "in.jpg", "JPEG")
    dst = tmp_path / "out.png"

    embed(src, dst, REC)

    assert _format_on_disk(dst) == "PNG", (
        f"a .png destination was written as {_format_on_disk(dst)} bytes"
    )
    got = read_embedded(dst.read_bytes())
    assert got and got.doi == REC.doi, got


def test_a_png_source_stays_png_whatever_the_destination_is_called(tmp_path):
    """The mirror case is NOT symmetric, and this pins the real rule.

    I first wrote this asserting a `.jpg` destination gets JPEG bytes, and it
    failed on unmutated code -- because I had invented a spec rather than read
    one. The condition is

        if fmt == "PNG" or dst.lower().endswith(".png"):

    and `or` SHORT-CIRCUITS: a PNG source takes the PNG arm before the
    destination name is even looked at. So source-format wins for PNG, while
    destination-extension only gets a say when the source is something else.

    The consequence is real and is recorded here rather than changed, because
    it is shipped behaviour and no part of this triage asked for a redesign:
    `embed("x.png", "out.jpg", rec)` writes PNG bytes into a file called
    `.jpg`. Every reader in this codebase sniffs content rather than trusting
    the name, so nothing downstream breaks -- but the name does lie.
    """
    src = _make(tmp_path / "in2.png", "PNG")
    dst = tmp_path / "out2.jpg"

    embed(src, dst, REC)

    assert _format_on_disk(dst) == "PNG", (
        f"the PNG arm stopped short-circuiting: got {_format_on_disk(dst)}"
    )
    got = read_embedded(dst.read_bytes())
    assert got and got.doi == REC.doi, (
        "the record did not survive into a PNG-in-a-.jpg file"
    )


def test_a_jpeg_written_under_any_other_name_is_still_a_jpeg(tmp_path):
    """The JPEG arm's `or`, which my first draft could not reach.

    `test_a_jpeg_written_to_a_jpg_keeps_the_jpeg_mechanism` has a JPEG source
    AND a .jpg destination, so both sides of the `or` are true and `and`
    satisfies it too -- that mutant survived. Only ONE side may hold for the
    two to be distinguishable.

    Here the source is JPEG and the destination extension is not. The `or`
    keeps it on the JPEG arm; under `and` it falls through to the convert-to-
    PNG branch and the file the caller asked for is never written at all.
    """
    src = _make(tmp_path / "in4.jpg", "JPEG")
    dst = tmp_path / "out4.dat"

    embed(src, dst, REC)

    assert dst.exists(), (
        "the destination the caller named was not written; the JPEG arm was "
        "skipped and the file was converted to .png instead"
    )
    assert _format_on_disk(dst) == "JPEG", _format_on_disk(dst)


def test_a_jpg_destination_wins_for_a_source_that_is_neither(tmp_path):
    """The other side of the same `or`: destination-only.

    A BMP carries no metadata of its own, so with a .jpg destination the JPEG
    arm is what gives the record somewhere to live. Under `and` (source is not
    JPEG) it converts to PNG instead and the requested .jpg never appears.
    """
    src = _make(tmp_path / "in5.bmp", "BMP")
    dst = tmp_path / "out5.jpg"

    embed(src, dst, REC)

    assert dst.exists(), "the .jpg the caller asked for was not written"
    assert _format_on_disk(dst) == "JPEG", _format_on_disk(dst)


def test_a_format_that_carries_no_metadata_is_converted_to_png(tmp_path):
    """The `else` arm. BMP can hold neither tEXt nor EXIF, so the record
    would simply be lost; the code converts instead. Asserted because both
    guards above having survived means nothing pinned which arm runs."""
    src = _make(tmp_path / "in.bmp", "BMP")
    dst = tmp_path / "out.bmp"

    out = embed(src, dst, REC)

    converted = tmp_path / "out.png"
    assert converted.exists(), (
        "a BMP was not converted, so its provenance had nowhere to live"
    )
    assert _format_on_disk(converted) == "PNG"
    assert out.sha256, "the returned record has no hash of what was written"


def test_the_written_record_hashes_the_output_not_the_input(tmp_path):
    """Positive control on all of the above.

    Every test here would pass if `embed` wrote the file and returned a
    record describing the SOURCE. The hash has to be of the bytes that
    actually landed, or the manifest points at something that was never
    written.
    """
    src = _make(tmp_path / "in3.png", "PNG")
    dst = tmp_path / "out3.png"

    out = embed(src, dst, REC)

    from figcite.provenance import sha256_bytes

    assert out.sha256 == sha256_bytes(dst.read_bytes()), (
        "the record hashes something other than the file on disk"
    )
    assert out.sha256 != sha256_bytes(src.read_bytes()), (
        "the record hashes the INPUT; embedding changed the bytes, so these must differ"
    )


def test_a_tiff_is_converted_too_and_not_written_under_its_own_name(tmp_path):
    """The `else` arm again, from ABOVE the comparison instead of below it.

    The BMP test above is the same rule and cannot see this. `embed`'s first
    branch is `fmt == "PNG" or dst...endswith(".png")`, and every source these
    tests feed it sorts at or below "PNG" -- "BMP" < "JPEG" < "PNG". Widen that
    comparison to `>=` and none of them enter it, so the mutant survives a file
    full of format-dispatch tests.

    "TIFF" sorts above. So do "WEBP" and "PPM". And TIFF is not an exotic
    choice for this tool: microscopy and gel images arrive as .tif routinely.

    Widened, a TIFF takes the PNG branch: PNG bytes are written to the .tif
    path that was asked for, the rename to .png never happens, and the sidecar
    ends up naming a file whose extension lies about its contents.
    """
    src = _make(tmp_path / "in.tif", "TIFF")
    assert _format_on_disk(src) == "TIFF", "the fixture is not a TIFF"
    assert "TIFF" > "PNG", "TIFF no longer sorts above PNG; this test is moot"

    dst = tmp_path / "out.tif"
    out = embed(src, dst, REC)

    converted = tmp_path / "out.png"
    assert converted.exists(), (
        "a TIFF was not converted; the PNG branch claimed it and wrote PNG "
        "bytes under the .tif name"
    )
    assert not dst.exists(), (
        f"{dst.name} was written as well as {converted.name}, so there are now "
        f"two files and only one of them is named honestly"
    )
    assert _format_on_disk(converted) == "PNG"
    assert out.sha256, "the returned record has no hash of what was written"
