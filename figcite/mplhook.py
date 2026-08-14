"""Provenance for figures you generate yourself.

matplotlib already embeds PNG text chunks natively; this just makes sure the
right things go in and that the figure lands in the manifest, so `figcite apply`
can caption your own plots the same way it captions a cropped paper figure.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Optional

from .provenance import Record, dhash_bytes, now_stamps, sha256_bytes, write_sidecar
from . import store


def _git_head(cwd: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def savefig(fig, path: str, *, cite: str = "", doi: Optional[str] = None,
            dataset: Optional[str] = None, note: str = "",
            script: Optional[str] = None, register: bool = True,
            **savefig_kw: Any) -> Record:
    """Drop-in for fig.savefig that stamps provenance into the PNG.

    >>> figcite.mplhook.savefig(fig, "out.png", cite="This work", dataset="arabidopsis_rnaseq_v3")
    """
    cwd = os.getcwd()
    commit = _git_head(cwd)
    u, loc = now_stamps()
    src = {
        "generated_by": script or os.path.abspath(getattr(__import__("__main__"), "__file__", "") or "interactive"),
        "cwd": cwd,
        "git_commit": commit,
        "dataset": dataset,
    }
    meta = {
        "Software": "figcite",
        "Copyright": cite or "This work",
        "Comment": note,
        "Creation Time": loc,
    }
    if doi:
        meta["Source"] = f"https://doi.org/{doi}"
    if commit:
        meta["Title"] = f"git:{commit[:12]}"
    savefig_kw.setdefault("dpi", 300)
    savefig_kw.setdefault("bbox_inches", "tight")
    fig.savefig(path, metadata=meta, **savefig_kw)

    blob = open(path, "rb").read()
    rec = Record(
        sha256=sha256_bytes(blob), dhash=dhash_bytes(blob),
        doi=doi, citation=cite or "This work", short_cite=cite or "This work",
        source_kind="generated", source_detail=src,
        captured_utc=u, captured_local=loc, confirmed=True, note=note,
    )
    write_sidecar(path, rec)
    if register:
        store.put(rec)
    return rec
