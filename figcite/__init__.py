"""figcite -- keep DOI/citation provenance attached to images you put in slides."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("figcite")
except PackageNotFoundError:  # running from a checkout that was never installed
    __version__ = "0+unknown"

from .provenance import Record  # noqa: F401
