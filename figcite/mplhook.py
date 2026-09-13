"""Provenance for figures you generate yourself.

matplotlib already embeds PNG text chunks natively; this just makes sure the
right things go in and that the figure lands in the manifest, so `figcite apply`
can caption your own plots the same way it captions a cropped paper figure.
"""

from __future__ import annotations

import os
import subprocess
import threading
from contextlib import contextmanager
from typing import Any, Optional

from .provenance import Record, dhash_bytes, now_stamps, sha256_bytes, write_sidecar
from . import store


def _git_head(cwd: str) -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def savefig(
    fig,
    path: str,
    *,
    cite: str = "",
    doi: Optional[str] = None,
    dataset: Optional[str] = None,
    note: str = "",
    script: Optional[str] = None,
    register: bool = True,
    **savefig_kw: Any,
) -> Record:
    """Drop-in for fig.savefig that stamps provenance into the PNG.

    >>> figcite.mplhook.savefig(fig, "out.png", cite="This work", dataset="arabidopsis_rnaseq_v3")
    """
    cwd = os.getcwd()
    commit = _git_head(cwd)
    u, loc = now_stamps()
    src = {
        "generated_by": script
        or os.path.abspath(getattr(__import__("__main__"), "__file__", "") or "interactive"),
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
    # This helper does its own registration below. If install() has patched
    # Figure.savefig, the call on the next line would ALSO register, filing the
    # same figure twice -- measured, +2 records for one call. Claiming the
    # re-entrancy flag here is what makes "figcite is already handling this"
    # true at the moment the patched savefig runs.
    with _handling():
        fig.savefig(path, metadata=meta, **savefig_kw)

    blob = open(path, "rb").read()
    rec = Record(
        sha256=sha256_bytes(blob),
        dhash=dhash_bytes(blob),
        doi=doi,
        citation=cite or "This work",
        short_cite=cite or "This work",
        source_kind="generated",
        source_detail=src,
        captured_utc=u,
        captured_local=loc,
        confirmed=True,
        note=note,
    )
    write_sidecar(path, rec)
    if register:
        store.put(rec)
    return rec


# --------------------------------------------------------------- auto-register
#
# `savefig()` above only helps if you remember to call it. The retro-match is
# blunt about which half of that matters: figures saved from a script recovered
# 15/15 after the fact, because the script still existed. The ones that go
# missing are the ones nobody thought about at the time. So the patch below
# makes the ordinary `fig.savefig("x.png")` self-register with no call-site
# change at all.
#
# `plt.savefig` is not patched separately: it is a thin wrapper that calls
# `fig.savefig` (verified against matplotlib 3.11), so patching Figure.savefig
# covers both. Patching both would double-register every pyplot call.

_ORIGINAL = None  # the unpatched Figure.savefig, kept so uninstall is exact
_DEFAULTS: dict[str, Any] = {}

# Thread-local rather than a plain global: a bare global set by one thread would
# be observed by another, and the direction of that race is silent -- the second
# thread's figure is passed through UNREGISTERED, which looks exactly like a
# figure nobody saved. Registration gaps must not be a race outcome.
_state = threading.local()


def _busy() -> bool:
    return getattr(_state, "in_hook", False)


@contextmanager
def _handling():
    """Mark this thread as already handling registration for a savefig call."""
    prev = _busy()
    _state.in_hook = True
    try:
        yield
    finally:
        _state.in_hook = prev


# Formats we can hash and therefore file. A PDF or SVG has no pixels to
# perceptually hash, so it cannot be matched back to a slide later.
RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}


class RegistrationError(RuntimeError):
    """The figure was written but its provenance was not recorded.

    Deliberately loud, and deliberately raised AFTER the file is on disk: a
    provenance tool that silently stops recording is the exact failure this
    project exists to prevent, so it must not be possible to lose a figure's
    origin without seeing an error. `install(strict=False)` downgrades this to
    a warning for people who would rather keep plotting.
    """


def installed() -> bool:
    return _ORIGINAL is not None


def install(
    *,
    cite: str = "This work",
    dataset: Optional[str] = None,
    note: str = "",
    strict: bool = True,
) -> None:
    """Make every `fig.savefig(...)` register itself in the manifest.

    Idempotent: installing twice does not stack wrappers, which would otherwise
    double-write the manifest and make `uninstall()` restore a wrapper rather
    than the real function.

        import figcite.mplhook as fc; fc.install(dataset="rnaseq_v3")
        fig.savefig("panel_a.png")     # now filed, with cwd + git commit
    """
    global _ORIGINAL, _DEFAULTS
    from matplotlib.figure import Figure

    _DEFAULTS = {"cite": cite, "dataset": dataset, "note": note, "strict": strict}
    if _ORIGINAL is not None:
        return
    _ORIGINAL = Figure.savefig
    Figure.savefig = _wrapped  # type: ignore[method-assign]


def uninstall() -> None:
    """Restore matplotlib's own savefig."""
    global _ORIGINAL
    from matplotlib.figure import Figure

    if _ORIGINAL is not None:
        Figure.savefig = _ORIGINAL  # type: ignore[method-assign]
        _ORIGINAL = None


def _target_path(fname) -> Optional[str]:
    """The on-disk path savefig is writing to, or None if it is not writing one.

    savefig accepts a file-like object as well as a path. A BytesIO has no path
    to hash later and no sidecar location, so those are passed straight through
    rather than half-registered.
    """
    if isinstance(fname, (str, os.PathLike)):
        return os.fspath(fname)
    return None


def _wrapped(self, fname, *args, **kwargs):
    """Figure.savefig, plus registration. Never changes what savefig returns."""
    assert _ORIGINAL is not None

    # mplhook.savefig() calls fig.savefig() internally and does its own
    # registration. Without this guard, using the explicit helper while the
    # patch is installed files the same figure twice.
    if _busy():
        return _ORIGINAL(self, fname, *args, **kwargs)

    result = _ORIGINAL(self, fname, *args, **kwargs)

    path = _target_path(fname)
    if path is None:
        return result
    if os.path.splitext(path)[1].lower() not in RASTER_SUFFIXES:
        return result

    try:
        with _handling():
            _register_written_file(path)
    except Exception as e:
        msg = f"figcite: wrote {path} but could NOT record its provenance: {type(e).__name__}: {e}"
        if _DEFAULTS.get("strict", True):
            raise RegistrationError(msg) from e
        import warnings

        warnings.warn(msg, stacklevel=2)
    return result


def _register_written_file(path: str) -> Record:
    """Hash a figure already on disk and file it as generated work."""
    cwd = os.getcwd()
    commit = _git_head(cwd)
    u, loc = now_stamps()
    cite = _DEFAULTS.get("cite") or "This work"
    rec = Record(
        citation=cite,
        short_cite=cite,
        source_kind="generated",
        source_detail={
            "generated_by": os.path.abspath(
                getattr(__import__("__main__"), "__file__", "") or "interactive"
            ),
            "cwd": cwd,
            "git_commit": commit,
            "dataset": _DEFAULTS.get("dataset"),
            "auto_registered": True,
            "path": os.path.abspath(path),
        },
        captured_utc=u,
        captured_local=loc,
        confirmed=True,
        note=_DEFAULTS.get("note") or "",
    )
    # register_existing hashes the bytes as written and stores under those
    # hashes, so the figure is matched by sha256 in a deck and perceptually
    # after PowerPoint re-encodes it.
    return store.register_existing(path, rec)
