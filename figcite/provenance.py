"""Provenance records: the thing that travels with an image.

Storage strategy, most durable last:
  1. embedded in the image bytes (PNG tEXt/iTXt-XMP, JPEG EXIF)
  2. a sidecar <image>.figcite.json next to the file
  3. the central manifest, keyed by sha256 AND by a perceptual dhash

Layer 1 dies if anything re-encodes the image (clipboard paste, PowerPoint's
"Compress Pictures"). Layer 3 is what survives that, which is why the dhash
fallback exists.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from PIL import Image, PngImagePlugin

TEXT_KEY = "figcite"
XMP_KEY = "XML:com.adobe.xmp"
LOCAL_TZ = ZoneInfo("America/New_York")

# EXIF tags used for JPEG (JPEG cannot hold the full record; it gets the
# human-readable cite only, and the full record lives in the sidecar/manifest).
EXIF_IMAGE_DESCRIPTION = 0x010E
EXIF_COPYRIGHT = 0x8298


def now_stamps() -> tuple[str, str]:
    u = datetime.now(timezone.utc)
    return u.isoformat(timespec="seconds"), u.astimezone(LOCAL_TZ).isoformat(
        timespec="seconds"
    )


@dataclass
class Record:
    """Everything known about where one image came from."""

    sha256: str = ""
    dhash: str = ""
    doi: Optional[str] = None
    url: Optional[str] = None
    citation: str = ""  # full formatted citation
    short_cite: str = ""  # "Harris et al. 2020"
    authors: list[str] = field(default_factory=list)
    year: Optional[int] = None
    title: str = ""
    container: str = ""
    license_url: Optional[str] = None
    reuse: str = "unknown"  # see crossref.classify_reuse
    retracted: bool = False
    adapted_from: Optional[str] = None  # DOI of an upstream figure source
    source_kind: str = "unknown"  # clipboard|pdf-crop|generated|download|manual
    source_detail: dict[str, Any] = field(default_factory=dict)
    captured_utc: str = ""
    captured_local: str = ""
    confirmed: bool = False  # False => a machine guessed this; never auto-cite it
    # Why this capture is resolved as NOT attributable; "" means it is not
    # dismissed. A reason rather than a bool, because a dismissal with no
    # stated reason is indistinguishable from a mistake six months later.
    # Deliberately NOT a form of `confirmed`: a dismissed record carries no
    # citation and must never read as sourced.
    dismissed: str = ""
    note: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "Record":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def doi_url(self) -> Optional[str]:
        return f"https://doi.org/{self.doi}" if self.doi else None

    def context_line(self) -> str:
        """Where this image was captured from, regardless of whether a DOI resolved.

        This is an observation -- what app was in front, what the window said,
        when -- so it is safe to show even when no citation could be established.
        "At least some track" is the point: a capture with no DOI still knows it
        came from Firefox showing a particular page at a particular minute.
        """
        d = self.source_detail or {}
        cap = d.get("clipboard_capture") or {}
        bits = []
        app = cap.get("process") or d.get("app")
        if app:
            bits.append(f"captured from {app}")
        title = (cap.get("title") or "").strip()
        if title:
            bits.append(f'window: "{title[:70]}"')
        url = d.get("url") or self.url
        if url and not (self.doi and url.endswith(self.doi)):
            bits.append(url[:90])
        if d.get("pdf"):
            bits.append(f"pdf: {d['pdf']}")
        when = cap.get("captured_local") or self.captured_local
        if when:
            bits.append(when[:19].replace("T", " "))
        return " — ".join(bits)

    def display(self) -> str:
        """One-line human form, honest about unconfirmed guesses."""
        base = self.citation or self.short_cite or self.url or ""
        if not base:
            ctx = self.context_line()
            base = f"no citation resolved ({ctx})" if ctx else "(no citation)"
        if self.doi and self.doi not in base:
            base = f"{base} https://doi.org/{self.doi}"
        if self.adapted_from:
            base += f" [figure adapted from https://doi.org/{self.adapted_from}]"
        if self.retracted:
            base = "RETRACTED — " + base
        if not self.confirmed:
            base += "  [UNCONFIRMED]"
        return base


# ---------------------------------------------------------------- hashing


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str | os.PathLike) -> str:
    with open(path, "rb") as fh:
        return sha256_bytes(fh.read())


def dhash_bytes(b: bytes, size: int = 8) -> str:
    """Difference hash: survives re-encoding and mild rescaling.

    Pure-PIL so there is no imagehash dependency. Returns hex.
    """
    im = (
        Image.open(io.BytesIO(b))
        .convert("L")
        .resize((size + 1, size), Image.Resampling.LANCZOS)
    )
    px = im.tobytes()  # mode "L" => one byte per pixel, row-major
    bits = []
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits.append("1" if left > right else "0")
    return f"{int(''.join(bits), 2):0{size * size // 4}x}"


def hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 999
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# ---------------------------------------------------------------- embedding


def _xmp_packet(rec: Record) -> str:
    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:xmpRights="http://ns.adobe.com/xap/1.0/rights/">'
        f"<dc:source>{esc(rec.doi_url or rec.url or '')}</dc:source>"
        f"<dc:rights><rdf:Alt><rdf:li xml:lang='x-default'>{esc(rec.citation)}</rdf:li></rdf:Alt></dc:rights>"
        f"<dc:title><rdf:Alt><rdf:li xml:lang='x-default'>{esc(rec.title)}</rdf:li></rdf:Alt></dc:title>"
        f"<xmpRights:WebStatement>{esc(rec.license_url or '')}</xmpRights:WebStatement>"
        '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    )


def embed(src: str | os.PathLike, dst: str | os.PathLike, rec: Record) -> Record:
    """Write the record into the image file. Returns rec with sha/dhash of the OUTPUT."""
    im = Image.open(src)
    fmt = (im.format or "").upper()
    dst = str(dst)

    if fmt == "PNG" or dst.lower().endswith(".png"):
        meta = PngImagePlugin.PngInfo()
        meta.add_text(TEXT_KEY, rec.to_json())
        if rec.doi_url or rec.url:
            meta.add_text("Source", rec.doi_url or rec.url or "")
        if rec.citation:
            meta.add_text("Copyright", rec.citation)
        if rec.title:
            meta.add_text("Title", rec.title)
        meta.add_itxt(XMP_KEY, _xmp_packet(rec))
        im.save(dst, "PNG", pnginfo=meta)
    elif fmt in ("JPEG", "JPG") or dst.lower().endswith((".jpg", ".jpeg")):
        exif = im.getexif()
        exif[EXIF_IMAGE_DESCRIPTION] = (rec.citation or rec.short_cite)[:900]
        exif[EXIF_COPYRIGHT] = (rec.citation or "")[:900]
        im.save(dst, "JPEG", exif=exif.tobytes(), quality=95, subsampling=0)
    else:
        # Anything else gets converted to PNG so it can carry metadata at all.
        dst = os.path.splitext(dst)[0] + ".png"
        return embed_image(im, dst, rec)

    blob = open(dst, "rb").read()
    rec.sha256 = sha256_bytes(blob)
    rec.dhash = dhash_bytes(blob)
    write_sidecar(dst, rec)
    return rec


def embed_image(im: Image.Image, dst: str, rec: Record) -> Record:
    tmp = dst + ".tmp.png"
    im.convert("RGB").save(tmp, "PNG")
    try:
        return embed(tmp, dst, rec)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def read_embedded(blob: bytes) -> Optional[Record]:
    """Pull a record back out of raw image bytes, if one is in there."""
    try:
        im = Image.open(io.BytesIO(blob))
    except Exception:
        return None
    txt = getattr(im, "text", None) or {}
    if TEXT_KEY in txt:
        try:
            return Record.from_dict(json.loads(txt[TEXT_KEY]))
        except Exception:
            pass
    # JPEG fallback: only the human-readable cite is recoverable from EXIF.
    try:
        exif = im.getexif()
    except Exception:
        exif = {}
    desc = exif.get(EXIF_IMAGE_DESCRIPTION) if exif else None
    if desc:
        r = Record(
            citation=str(desc),
            confirmed=False,
            note="recovered from JPEG EXIF; full record not embeddable in JPEG",
        )
        r.sha256 = sha256_bytes(blob)
        r.dhash = dhash_bytes(blob)
        return r
    return None


def sidecar_path(image_path: str | os.PathLike) -> str:
    return str(image_path) + ".figcite.json"


def write_sidecar(image_path: str | os.PathLike, rec: Record) -> str:
    p = sidecar_path(image_path)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(rec.to_json())
    return p


def read_sidecar(image_path: str | os.PathLike) -> Optional[Record]:
    p = sidecar_path(image_path)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return Record.from_dict(json.load(fh))
    except Exception:
        return None
