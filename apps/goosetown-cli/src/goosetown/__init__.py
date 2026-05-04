"""GooseTown CLI — a virtual town for AI agents."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("goosetown")
except PackageNotFoundError:
    # Running from a source checkout without an installed dist-info — best
    # effort: read pyproject.toml two levels up. Falls back to a sentinel so
    # `goosetown --version` doesn't crash in dev workflows.
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
