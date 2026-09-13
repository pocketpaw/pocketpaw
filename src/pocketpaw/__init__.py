"""PocketPaw - The AI agent that runs on your laptop, not a datacenter."""

# 2026-09-13: ``__version__`` was a hardcoded "0.4.15" that had drifted three
# releases behind ``pyproject.toml``. ``/api/v1/version`` serves it, so a
# perfectly current deployment reported a five-month-old version, and an
# afternoon went into hunting a deploy bug that did not exist. Read the number
# the build actually installed rather than keeping a second copy in sync by hand.

from importlib.metadata import PackageNotFoundError, version as _installed_version

try:
    __version__ = _installed_version("pocketpaw")
except PackageNotFoundError:  # a source tree with no install (rare; not how we ship)
    __version__ = "0.0.0.dev0"
