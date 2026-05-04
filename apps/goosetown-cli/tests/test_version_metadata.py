"""Regression tests for ``goosetown.__version__`` — must reflect installed dist.

Surfaced 2026-05-04 by dry-run #3 (Vera): ``goosetown --version`` reported
``0.1.0`` after ``uv tool upgrade`` to 0.1.5. Root cause: ``__init__.py``
had a hardcoded ``__version__ = "0.1.0"`` that no release bumped. Five
versions had shipped with stale ``--version`` output.

These tests pin the corrected behavior — the version comes from the
installed package metadata so a future bump in ``pyproject.toml``
automatically propagates without anyone touching ``__init__.py``.
"""

from importlib.metadata import version as _pkg_version
from pathlib import Path

import goosetown


def test_version_matches_installed_package_metadata():
    """``__version__`` must match what ``pip show goosetown`` would
    report — i.e. the dist-info on disk, not a stale literal."""
    assert goosetown.__version__ == _pkg_version("goosetown"), (
        f"__version__ ({goosetown.__version__}) does not match the installed "
        f"package metadata version ({_pkg_version('goosetown')}). The version "
        f"string must be derived from importlib.metadata, not a hardcoded "
        f"literal in __init__.py."
    )


def test_version_matches_pyproject_toml():
    """Belt-and-suspenders: the installed metadata version must also match
    the pyproject.toml at the repo, otherwise the build packaged the
    wrong version."""
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    # Find the [project] version line — first version = "..." in the file.
    import re

    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert match, "pyproject.toml has no version line"
    pyproject_version = match.group(1)
    assert goosetown.__version__ == pyproject_version, (
        f"__version__ ({goosetown.__version__}) doesn't match "
        f"pyproject.toml version ({pyproject_version}). Either the build "
        f"didn't pick up the bump or the metadata-loading logic is broken."
    )


def test_version_is_not_the_legacy_hardcoded_value():
    """Specific regression guard: 0.1.0 is the value that was hardcoded
    in __init__.py from the project's birth through 0.1.5 — if it shows
    up here on a non-0.1.0 build it means the metadata-loading path
    silently failed and we're back to the bug Vera caught."""
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    import re

    pyproject_version = re.search(
        r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE
    ).group(1)
    if pyproject_version != "0.1.0":
        assert goosetown.__version__ != "0.1.0", (
            "__version__ is 0.1.0 but pyproject.toml is at "
            f"{pyproject_version}. The hardcoded-literal regression is back."
        )
