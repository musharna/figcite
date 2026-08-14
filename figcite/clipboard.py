"""Windows clipboard capture + source inference.

Everything this module infers is a GUESS and is recorded as unconfirmed. The
deck writer refuses to print an unconfirmed citation onto a slide unless
explicitly overridden, because a confident-looking wrong citation is worse than
a visible gap.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from .browser import resolve_from_capture as browser_resolve
from .crossref import search_bibliographic
from .pdfgrab import discover_doi

PS1 = Path(__file__).with_name("watch_clipboard.ps1")
PS_EXE = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

PDF_APPS = {
    "acrobat",
    "acrord32",
    "sumatrapdf",
    "foxitpdfreader",
    "foxitreader",
    "pdfxedit",
    "zotero",
    "okular",
    "mupdf",
}
BROWSERS = {"msedge", "chrome", "firefox", "brave", "opera", "vivaldi"}

# Where a PDF named in a window title might actually live.
SEARCH_ROOTS = [
    Path.home() / "Downloads",
    Path.home() / "wiki",
    Path.home() / ".cache" / "pdf-parse-docling",
]


def _win_userprofile() -> Optional[str]:
    try:
        r = subprocess.run(
            [PS_EXE, "-NoProfile", "-Command", "$env:USERPROFILE"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def win_to_wsl(p: str) -> str:
    try:
        r = subprocess.run(
            ["wslpath", "-u", p], capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return p


def wsl_to_win(p: str | os.PathLike) -> str:
    try:
        r = subprocess.run(
            ["wslpath", "-w", str(p)], capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return str(p)


def staging_dirs() -> tuple[str, Path]:
    """(windows staging path, wsl view of it)."""
    up = _win_userprofile()
    if not up:
        raise RuntimeError(
            "could not read $env:USERPROFILE via powershell.exe -- is this WSL with "
            "Windows interop enabled? (tried %s)" % PS_EXE
        )
    win = up.rstrip("\\") + r"\.figcite\staging"
    return win, Path(win_to_wsl(win))


# ------------------------------------------------------------- inference


def pdf_name_from_title(title: str) -> Optional[str]:
    """Pull a *.pdf filename out of a window title."""
    m = re.search(r"([^\\/:*?\"<>|]+\.pdf)", title or "", re.I)
    return m.group(1).strip() if m else None


def clean_browser_title(title: str) -> str:
    """Strip the browser chrome off a tab title so it can be searched."""
    t = title or ""
    t = re.sub(
        r"\s+[-—|]\s+(Google Chrome|Microsoft\s*Edge|Mozilla Firefox|Brave|Opera|Vivaldi)\s*$",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(r"\s+and \d+ more pages?.*$", "", t, flags=re.I)
    t = re.sub(r"^\(\d+\)\s*", "", t)  # unread-count prefix
    t = re.sub(
        r"\s*[-—|]\s*(ScienceDirect|PubMed|Nature|bioRxiv|PMC|Wiley Online Library|SpringerLink)\s*$",
        "",
        t,
        flags=re.I,
    )
    return t.strip()


def find_pdf_on_disk(
    name: str,
    roots: Optional[Iterable[Path]] = None,
    extra: Optional[Iterable[Path]] = None,
) -> Optional[str]:
    roots = list(roots or SEARCH_ROOTS)
    up = _win_userprofile()
    if up:
        wsl_home = Path(win_to_wsl(up))
        roots += [
            wsl_home / "Downloads",
            wsl_home / "Documents",
            wsl_home / "Zotero" / "storage",
            wsl_home / "Desktop",
        ]
    roots += list(extra or [])
    target = name.lower()
    for root in roots:
        if not root.exists():
            continue
        try:
            r = subprocess.run(
                ["find", str(root), "-maxdepth", "4", "-type", "f", "-iname", name],
                capture_output=True,
                text=True,
                timeout=45,
            )
            for line in r.stdout.splitlines():
                if line.strip() and Path(line).name.lower() == target:
                    return line.strip()
        except Exception:
            continue
    return None


def infer_source(capture: dict) -> dict:
    """Best-effort provenance guess for one clipboard capture.

    Returns {'kind', 'doi', 'doi_evidence', 'candidates', 'pdf'} where any DOI
    is unconfirmed unless it came out of a PDF we actually located and read.
    """
    title = capture.get("title", "") or ""
    proc = (capture.get("process") or "").lower()
    out: dict = {
        "kind": "unknown",
        "doi": None,
        "doi_evidence": "",
        "candidates": [],
        "pdf": None,
        "url": None,
        "grounded": False,
    }

    # Browser first. A web-hosted PDF puts "<accession>.pdf" in the window title,
    # which would otherwise be sent down the find-it-on-disk path and dead-end
    # there -- the file only ever existed in the browser's cache.
    if proc in BROWSERS:
        try:
            b = browser_resolve(capture)
        except Exception as e:
            b = {
                "doi": None,
                "url": None,
                "grounded": False,
                "evidence": f"browser grounding failed: {e}",
            }
        out["url"] = b.get("url")
        if b.get("doi"):
            out.update(
                kind="clipboard-from-web",
                doi=b["doi"],
                doi_evidence=b.get("evidence", ""),
                grounded=bool(b.get("grounded")),
            )
            return out
        browser_note = b.get("evidence", "")
    else:
        browser_note = ""

    pdf_name = pdf_name_from_title(title)
    if pdf_name and (proc in PDF_APPS or proc in BROWSERS or True):
        path = find_pdf_on_disk(pdf_name)
        if path:
            out["pdf"] = path
            try:
                doi, where = discover_doi(path)
            except Exception as e:
                doi, where = None, f"could not read {path}: {e}"
            out.update(
                kind="clipboard-from-pdf",
                doi=doi,
                doi_evidence=where,
                grounded=bool(doi),
            )
            return out
        out.update(
            kind="clipboard-from-pdf",
            doi_evidence=f"window title named '{pdf_name}' but no such file was found on disk",
        )
        return out

    if proc in BROWSERS:
        q = clean_browser_title(title)
        out["kind"] = "clipboard-from-web"
        if len(q) > 12:
            try:
                out["candidates"] = search_bibliographic(q, rows=5)
            except Exception as e:
                out["doi_evidence"] = f"CrossRef search failed: {e}"
        out["doi_evidence"] = out["doi_evidence"] or (
            "title-derived candidates only -- CrossRef title search is unreliable "
            "(it ranks reviews and commentaries above the paper itself), so pick one explicitly"
        )
        if browser_note:
            out["doi_evidence"] = f"{browser_note}; fell back to {out['doi_evidence']}"
        return out

    out["doi_evidence"] = (
        f"no rule for process '{proc or '?'}' with title '{title[:80]}'"
    )
    return out


# ------------------------------------------------------------- watching


def watch(max_hours: float = 8.0, poll_ms: int = 800, resolve: bool = True,
          auto_confirm: bool = True) -> int:
    """Run the watcher until it hits its own wall-clock deadline."""
    if not Path(PS_EXE).exists():
        raise RuntimeError(f"powershell.exe not found at {PS_EXE}")
    win_dir, wsl_dir = staging_dirs()
    wsl_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        PS_EXE,
        "-sta",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        wsl_to_win(PS1),
        "-StagingDir",
        win_dir,
        "-PollMs",
        str(poll_ms),
        "-MaxHours",
        str(max_hours),
    ]
    print(
        f"watching clipboard -> {wsl_dir}  (deadline {max_hours}h; Ctrl-C to stop)",
        flush=True,
    )
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            if line.startswith("CAPTURED "):
                png_win = line.split(" ", 1)[1]
                png = win_to_wsl(png_win)
                print(f"  captured {Path(png).name}", flush=True)
                if resolve:
                    try:
                        pending = enrich(png)
                        if auto_confirm:
                            auto_finalize(png, pending)
                    except Exception as e:
                        print(f"    (inference failed: {e})", flush=True)
            else:
                print(f"  {line}", flush=True)
    except KeyboardInterrupt:
        print("stopping watcher", flush=True)
    finally:
        proc.terminate()
    return 0


def enrich(png: str | os.PathLike) -> dict:
    """Attach an inference to a staged capture, writing <base>.pending.json."""
    png = Path(png)
    cap_file = png.with_suffix("").with_suffix(".capture.json")
    if not cap_file.exists():
        cap_file = Path(str(png)[:-4] + ".capture.json")
    cap = (
        json.loads(cap_file.read_text(encoding="utf-8-sig"))
        if cap_file.exists()
        else {}
    )
    inf = infer_source(cap)
    pending = {"png": str(png), "capture": cap, "inference": inf}
    Path(str(png)[:-4] + ".pending.json").write_text(
        json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if inf.get("doi"):
        print(f"    source: {inf['doi']}  (from {inf['doi_evidence']})", flush=True)
    elif inf.get("candidates"):
        print(
            f"    {len(inf['candidates'])} unconfirmed candidates -- run `figcite pending`",
            flush=True,
        )
    else:
        print(f"    no source inferred: {inf.get('doi_evidence', '')}", flush=True)
    return pending


def list_pending() -> list[dict]:
    _, wsl_dir = staging_dirs()
    if not wsl_dir.exists():
        return []
    out = []
    for p in sorted(wsl_dir.glob("*.pending.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    # Captures the watcher saw but never enriched (e.g. watcher died mid-run).
    known = {Path(d["png"]).name for d in out}
    for p in sorted(wsl_dir.glob("clip-*.png")):
        if p.name not in known:
            out.append(
                {"png": str(p), "capture": {}, "inference": {"kind": "unenriched"}}
            )
    return out


def auto_finalize(png: str | os.PathLike, pending: dict) -> Optional[Path]:
    """File EVERY capture. Grounded ones get a citation; the rest get a trail.

    The point of filing an ungrounded capture is that a screenshot with no DOI
    still knows which app was in front, what the window said, what URL was open
    and when. That is a real record -- it just is not a citation, and it is
    stored as unconfirmed so nothing downstream can print it as one.

    Leaving these loose in the staging folder meant that pasting one into a deck
    produced "no source recorded", discarding context that had already been
    collected.
    """
    from . import store
    from .crossref import record_from_doi
    from .provenance import Record, now_stamps

    inf = pending.get("inference", {})
    cap = pending.get("capture", {})
    png = Path(png)
    detail = {
        "clipboard_capture": cap,
        "inference_kind": inf.get("kind", ""),
        "url": inf.get("url"),
        "pdf": inf.get("pdf"),
        "doi_evidence": inf.get("doi_evidence", ""),
    }

    if inf.get("grounded") and inf.get("doi"):
        try:
            rec = record_from_doi(inf["doi"], confirmed=True, source_kind="clipboard",
                                  source_detail=detail)
        except Exception as e:
            print(f"    (citation lookup failed, filing with context only: {e})", flush=True)
            rec = _context_record(detail, inf, note=f"citation lookup failed: {e}")
    else:
        rec = _context_record(detail, inf)

    dest = store.finalize_into_library(png, rec)
    if rec.confirmed:
        print(f"    AUTO-TAGGED -> {dest.name}", flush=True)
        print(f"      {rec.short_cite or rec.doi} | {rec.reuse}", flush=True)
    else:
        print(f"    filed unconfirmed -> {dest.name}", flush=True)
        print(f"      {rec.context_line()[:110]}", flush=True)
        print("      (resolve later with `figcite pending`)", flush=True)
    for suffix in (".pending.json", ".capture.json"):
        q = Path(str(png)[:-4] + suffix)
        if q.exists():
            q.unlink()
    if png.exists() and png != dest:
        png.unlink()
    return dest


def _context_record(detail: dict, inf: dict, note: str = ""):
    """A record that carries the capture context but claims no citation."""
    from .provenance import Record, now_stamps
    u, loc = now_stamps()
    cap = detail.get("clipboard_capture") or {}
    return Record(
        url=inf.get("url"),
        source_kind="clipboard",
        source_detail=detail,
        captured_utc=u,
        captured_local=cap.get("captured_local") or loc,
        confirmed=False,
        note=(note or inf.get("doi_evidence", ""))[:400],
    )
