import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Every test run gets its own manifest so the user's real library is never touched.
_TMP = tempfile.mkdtemp(prefix="figcite-tests-")
os.environ["FIGCITE_HOME"] = _TMP
os.environ.setdefault("FIGCITE_CACHE", str(Path(_TMP) / "crossref-cache"))
