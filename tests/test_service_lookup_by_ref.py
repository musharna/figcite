"""Three lookups that scan a list for one item, and none was ever asked to miss.

    service._item_for(ref)     `it.ref == ref`
    service._raw_staged(ref)   `Path(raw["png"]).name == name`
    service.apply(...)         `out_path.suffix != in_path.suffix`

Each walks a collection and stops at the first element that matches a NAME.
Narrowed to an order, "the first element that equals X" becomes "the first
element that sorts on one side of X" -- and since these lists are short and the
scan stops at the first hit, that is almost always element ZERO. So the lookup
does not fail; it succeeds, with the wrong thing.

They survived because every existing test looks up something that IS there, in
a collection where it happens to be first, or with one item in it. Two probes
fix that: a ref that sorts BELOW everything present and one that sorts ABOVE.
Both must miss, and a miss must stay a miss.

`apply`'s suffix check is the same shape one level up. It refuses an output
whose extension differs from the input's -- writing PDF bytes into a .pptx is
how a deck gets silently corrupted -- and `!=` narrowed to `>` only refuses
half the pairs. `.pptx` in, `.pdf` out sorts the wrong way and sails through.
"""

from __future__ import annotations

import json

import pytest
from PIL import Image

from figcite import service, store
from figcite.provenance import Record, now_stamps

FILED_SHA = "b" * 64


@pytest.fixture
def two_of_each(tmp_path, monkeypatch):
    """Two staged captures and one filed one, so a scan can stop early."""
    staging = tmp_path / "staging"
    staging.mkdir()
    for name in ("clip-aaa.png", "clip-zzz.png"):
        png = staging / name
        Image.new("RGB", (10, 8), (30, 30, 90)).save(png)
        (staging / f"{name[:-4]}.pending.json").write_text(
            json.dumps(
                {
                    "png": str(png),
                    "capture": {"process": "firefox", "title": name},
                    "inference": {"doi": None, "doi_evidence": "nothing matched"},
                }
            )
        )
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))

    u, loc = now_stamps()
    store.put(
        Record(
            sha256=FILED_SHA,
            dhash="0" * 16,
            source_kind="clipboard",
            confirmed=False,
            captured_utc=u,
            captured_local=loc,
            source_detail={
                "clipboard_capture": {"process": "powerpnt", "title": "filed one"},
                "doi_evidence": "nothing matched",
            },
        )
    )
    return {
        "filed": f"filed:{FILED_SHA}",
        "first_staged": "staged:clip-aaa.png",
        "last_staged": "staged:clip-zzz.png",
    }


def test_the_fixture_gives_the_scan_something_to_stop_early_on(two_of_each):
    """The premise. A one-item list cannot show a scan returning the wrong
    item, and a lookup of the FIRST item cannot either."""
    refs = [i.ref for i in service.pending_items()]
    assert len(refs) == 3, refs
    assert refs[0] != two_of_each["last_staged"], (
        "the ref these tests look up is already first; the scan cannot be observed to stop early"
    )
    assert two_of_each["filed"] < two_of_each["last_staged"], refs


def test_item_for_returns_the_item_that_was_asked_for(two_of_each):
    for key in ("filed", "first_staged", "last_staged"):
        got = service._item_for(two_of_each[key])
        assert got is not None, f"{key} was not found"
        assert got.ref == two_of_each[key], (
            f"asked for {two_of_each[key]}, got {got.ref} -- the scan stopped "
            f"at an element that merely sorts near it"
        )


@pytest.mark.parametrize(
    "missing,where",
    [
        ("filed:" + "0" * 64, "below every ref present"),
        ("zzzz:nothing-like-this", "above every ref present"),
    ],
)
def test_item_for_misses_a_ref_that_is_not_there(two_of_each, missing, where):
    """Both directions, because `==` narrowed to `<=` and to `>=` each fail on
    only one side. A ref below everything is caught by `>=`; a ref above
    everything is caught by `<=`."""
    assert service._item_for(missing) is None, f"a ref {where} was matched to a real item"


def test_raw_staged_returns_the_capture_that_was_asked_for(two_of_each):
    raw = service._raw_staged(two_of_each["last_staged"])

    assert raw["png"].endswith("clip-zzz.png"), (
        f"asked for clip-zzz.png, got {raw['png']} -- the scan stopped at the "
        f"first name that sorts below it"
    )


def test_raw_staged_raises_for_a_capture_that_is_not_there(two_of_each):
    with pytest.raises(KeyError):
        service._raw_staged("staged:clip-nothing.png")


# --- the suffix guard, both ways --------------------------------------------


@pytest.mark.parametrize(
    "in_suffix,out_suffix",
    [
        (".pptx", ".pdf"),  # out sorts BELOW in -- the half `>` lets through
        (".pdf", ".pptx"),  # out sorts above in
    ],
)
def test_apply_refuses_an_output_whose_suffix_differs(tmp_path, in_suffix, out_suffix):
    """`out.suffix != in.suffix` narrowed to `>` refuses only the pairs that
    happen to sort one way. `.pdf` out of a `.pptx` in is exactly the pair it
    lets through, and writing one format's bytes under the other's extension
    is how a deck becomes unopenable."""
    src = tmp_path / f"deck{in_suffix}"
    src.write_bytes(b"not really a deck, and never read")
    out = tmp_path / f"deck-cited{out_suffix}"

    with pytest.raises(ValueError) as e:
        service.apply(str(src), out=str(out))

    assert "suffix" in str(e.value), str(e.value)


def test_the_suffix_pair_sorts_both_ways():
    """The premise for the parametrisation above."""
    assert ".pdf" < ".pptx"
