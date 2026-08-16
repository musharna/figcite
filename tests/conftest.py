import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Every test run gets its own manifest so the user's real library is never touched.
_TMP = tempfile.mkdtemp(prefix="figcite-tests-")
os.environ["FIGCITE_HOME"] = _TMP
os.environ.setdefault("FIGCITE_CACHE", str(Path(_TMP) / "crossref-cache"))

# Unit tests must never reach the real Zotero library. Credentials live in the
# environment, so on a machine that exports them a test would silently start
# hitting the network and asserting against 4,824 real items -- passing or
# failing for reasons that have nothing to do with the code under test.
for _var in (
    "FIGCITE_ZOTERO_API_KEY",
    "ZOTERO_API_KEY",
    "FIGCITE_ZOTERO_LIBRARY_ID",
    "ZOTERO_LIBRARY_ID",
    "FIGCITE_ZOTERO_LIBRARY_TYPE",
    "ZOTERO_LIBRARY_TYPE",
):
    os.environ.pop(_var, None)
os.environ["FIGCITE_ZOTERO_CACHE"] = str(Path(_TMP) / "zotero-cache")
# ...including the on-disk credential file, which a real install has.
os.environ["FIGCITE_ZOTERO_CONFIG"] = str(Path(_TMP) / "zotero-config.json")
