"""Provenance embed/read + the perceptual fallback.

Each negative assertion here ships with a positive control in the same test, so
a broken harness reads as a failure rather than as a clean "nothing matched".
"""

from PIL import Image

from figcite.provenance import (
    Record,
    dhash_bytes,
    embed,
    hamming,
    read_embedded,
    read_sidecar,
    sha256_bytes,
)


def _png(tmp_path, name, color=(40, 90, 140), size=(160, 120)):
    p = tmp_path / name
    Image.new("RGB", size, color).save(p)
    return p


def test_png_roundtrip_carries_doi(tmp_path):
    src = _png(tmp_path, "src.png")
    rec = Record(
        doi="10.1038/s41586-020-2649-2",
        citation="Harris et al. (2020). Nature 585:357-362.",
        short_cite="Harris et al. 2020",
        confirmed=True,
    )
    out = tmp_path / "out.png"
    rec = embed(src, out, rec)

    back = read_embedded(out.read_bytes())
    assert back is not None, "no record came back out of the PNG"
    assert back.doi == "10.1038/s41586-020-2649-2"
    assert back.confirmed is True
    assert rec.sha256 == sha256_bytes(out.read_bytes())

    # negative control: an untouched image must NOT yield a record
    plain = _png(tmp_path, "plain.png", color=(10, 10, 10))
    assert read_embedded(plain.read_bytes()) is None, (
        "read_embedded invented a record for an image that never had one"
    )

    # sidecar is written alongside
    side = read_sidecar(out)
    assert side is not None and side.doi == back.doi


def test_xmp_packet_present(tmp_path):
    src = _png(tmp_path, "src.png")
    rec = Record(
        doi="10.1111/mec.12953",
        citation="Ho et al. (2014).",
        title="A title",
        license_url="https://creativecommons.org/licenses/by/4.0",
        confirmed=True,
    )
    out = tmp_path / "x.png"
    embed(src, out, rec)
    txt = Image.open(out).text
    assert "XML:com.adobe.xmp" in txt
    assert "10.1111/mec.12953" in txt["XML:com.adobe.xmp"]
    assert "creativecommons" in txt["XML:com.adobe.xmp"]


def test_jpeg_carries_cite_but_is_honest_about_the_limit(tmp_path):
    src = tmp_path / "s.jpg"
    Image.new("RGB", (120, 90), (200, 120, 40)).save(src, "JPEG")
    rec = Record(
        doi="10.1038/x", citation="Someone et al. (2001). A journal 1:2-3.", confirmed=True
    )
    out = tmp_path / "o.jpg"
    embed(src, out, rec)
    back = read_embedded(out.read_bytes())
    assert back is not None
    assert "Someone" in back.citation
    # JPEG cannot carry the structured record, so it must not claim confirmation
    assert back.confirmed is False
    assert "JPEG" in back.note


def test_dhash_survives_recompression_but_discriminates(tmp_path):
    """Positive and negative control in one test.

    Positive: the same picture, JPEG-recompressed and resized, still matches.
    Negative: a visually different picture does not.
    """
    im = Image.new("RGB", (300, 200))
    for x in range(300):  # a gradient, so the hash is not degenerate
        for y in range(0, 200, 4):
            im.putpixel((x, y), (x % 256, (x * 2) % 256, (y * 3) % 256))
    orig = tmp_path / "a.png"
    im.save(orig)
    d_orig = dhash_bytes(orig.read_bytes())

    recompressed = tmp_path / "a.jpg"
    im.resize((280, 187)).save(recompressed, "JPEG", quality=55)
    d_re = dhash_bytes(recompressed.read_bytes())
    assert hamming(d_orig, d_re) <= 6, (
        f"recompressed copy drifted too far (hamming={hamming(d_orig, d_re)})"
    )

    other = Image.new("RGB", (300, 200), (255, 255, 255))
    for x in range(0, 300, 6):
        for y in range(200):
            other.putpixel((x, y), (0, 0, 0))
    d_other = dhash_bytes(_save(other, tmp_path / "b.png"))
    assert hamming(d_orig, d_other) > 6, (
        "dhash matched a clearly different image -- the discriminator is dead"
    )


def _save(im, p):
    im.save(p)
    return p.read_bytes()
