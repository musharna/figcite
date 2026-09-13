"""Hand a deck's figure sources to ghostcite at manuscript time.

**Why BibTeX and not a DOI list.** ghostcite catches ghost citations by
comparing the byline you CLAIM against the byline CrossRef reports. A bare DOI
list claims nothing, so there is nothing to disagree with. Measured 2026-08-16
against ghostcite: a file of two real DOIs produced 0 findings, while the same
two as BibTeX entries -- one with a fabricated author -- produced exactly 1, on
the fabricated one. So the handoff has to emit author/year/title, not just the
identifier, or the check is inert while looking like it ran.

**Why unconfirmed records are excluded by default.** An unconfirmed record is a
machine's guess about which paper a figure came from. Emitting those into a
manuscript bibliography is precisely the ghost-citation failure this is meant to
prevent, and ghostcite would not catch it: the byline would match the DOI
perfectly, because both came from CrossRef. It is the DOI-to-FIGURE link that is
unverified, and no bibliography checker can see that. They are counted and
reported instead, and `include_unconfirmed=True` is available for someone who
knows what they are asking for.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from .provenance import Record

GHOSTCITE = "ghostcite"

# BibTeX special characters. A title containing a bare & or % silently breaks
# the .bib for whatever reads it next, which usually surfaces as a missing
# reference rather than an error.
_TEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def escape(s: str) -> str:
    out = []
    for ch in s or "":
        out.append(_TEX_ESCAPES.get(ch, ch))
    return "".join(out)


def cite_key(rec: Record, taken: Optional[set[str]] = None) -> str:
    """A stable, unique key: firstauthor + year, disambiguated by suffix.

    Keyed off the author and year rather than a hash so the .bib stays readable
    and diffable across runs; collisions get a, b, c the way BibTeX users
    already expect.
    """
    who = ""
    if rec.authors:
        who = rec.authors[0].split(",")[0]
    who = re.sub(r"[^A-Za-z]", "", who) or "anon"
    base = f"{who.lower()}{rec.year or 'nd'}"
    if taken is None:
        return base
    key, n = base, 0
    while key in taken:
        key = f"{base}{chr(ord('a') + n)}"
        n += 1
    taken.add(key)
    return key


def _authors_field(rec: Record) -> str:
    """BibTeX 'and'-joined authors, in the 'Family, Given' form CrossRef gave us.

    Passed through verbatim rather than reformatted: ghostcite compares the
    first author's surname, and every reformat is a chance to corrupt exactly
    the field being checked.
    """
    return " and ".join(escape(a) for a in rec.authors) if rec.authors else ""


def entry(rec: Record, key: str) -> str:
    """One @article entry. Only fields we actually have are emitted."""
    fields: list[tuple[str, str]] = []
    if rec.authors:
        fields.append(("author", _authors_field(rec)))
    if rec.title:
        fields.append(("title", escape(rec.title)))
    if rec.year:
        fields.append(("year", str(rec.year)))
    if rec.container:
        fields.append(("journal", escape(rec.container)))
    if rec.doi:
        fields.append(("doi", rec.doi))
    if rec.url:
        fields.append(("url", rec.url))
    # Carried so a human reading the .bib can see what the machine believed.
    notes = []
    if not rec.confirmed:
        notes.append("UNCONFIRMED figure-to-DOI link")
    if rec.retracted:
        notes.append("CrossRef flags this work as RETRACTED")
    if rec.reuse and rec.reuse != "unknown":
        notes.append(f"reuse: {rec.reuse}")
    if notes:
        fields.append(("note", escape("; ".join(notes))))
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields)
    return f"@article{{{key},\n{body}\n}}"


def records_to_bibtex(records: Iterable[Record], *, include_unconfirmed: bool = False) -> dict:
    """Render records as BibTeX. Returns {bibtex, included, skipped_*}.

    Skips are counted and returned rather than dropped quietly -- a bibliography
    that is short because entries vanished looks identical to one that is short
    because the deck was small.
    """
    taken: set[str] = set()
    entries: list[str] = []
    seen_doi: set[str] = set()
    no_doi = 0
    unconfirmed = 0
    own_work = 0

    for rec in records:
        if rec is None:
            continue
        if rec.source_kind == "generated":
            own_work += 1
            continue
        if not rec.doi:
            no_doi += 1
            continue
        if not rec.confirmed and not include_unconfirmed:
            unconfirmed += 1
            continue
        if rec.doi.lower() in seen_doi:
            continue  # one entry per work, however many figures came from it
        seen_doi.add(rec.doi.lower())
        entries.append(entry(rec, cite_key(rec, taken)))

    header = (
        "% Generated by `figcite bib`. Every entry is a work a figure in this\n"
        "% deck was taken from. Author/year/title come from CrossRef, not from\n"
        "% recall, so they can be cross-checked -- run `ghostcite <this file>`.\n"
    )
    return {
        "bibtex": header + "\n" + "\n\n".join(entries) + ("\n" if entries else ""),
        "included": len(entries),
        "skipped_unconfirmed": unconfirmed,
        "skipped_no_doi": no_doi,
        "skipped_own_work": own_work,
    }


# ------------------------------------------------------------------ ghostcite


def ghostcite_available() -> Optional[str]:
    return shutil.which(GHOSTCITE)


def run_ghostcite(bib_path: str | Path, timeout: int = 600) -> dict:
    """Run ghostcite over the emitted .bib and return its parsed JSON.

    A missing ghostcite raises rather than returning an empty result: "no
    findings" and "the checker never ran" must not look the same.
    """
    exe = ghostcite_available()
    if not exe:
        raise RuntimeError(
            f"{GHOSTCITE} is not on PATH; install it with `pipx install ghostcite` "
            f"(a missing checker is not a clean bibliography)"
        )
    r = subprocess.run(
        [exe, "--json", str(bib_path)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    import json

    try:
        return json.loads(r.stdout)
    except Exception as e:
        raise RuntimeError(
            f"could not parse {GHOSTCITE} output (exit {r.returncode}): "
            f"{r.stderr.strip()[:300] or r.stdout[:300]}"
        ) from e
