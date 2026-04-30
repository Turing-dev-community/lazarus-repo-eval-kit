"""Session-scoped fixtures for characterization tests."""

import subprocess
import sys
from pathlib import Path

import pytest

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_REPOS_DIR = _FIXTURES_DIR / "repos"
_BUILD_SCRIPT = _FIXTURES_DIR / "build_fixture_repos.py"

_FIXTURE_NAMES = ("tiny_python", "no_prs", "multi_lang_ci")


def _repos_need_build() -> bool:
    for name in _FIXTURE_NAMES:
        if not (_REPOS_DIR / name / ".git").exists():
            return True
    return False


@pytest.fixture(scope="session")
def fixture_repos():
    """Ensure fixture git repos exist, building them if necessary.

    Running build_fixture_repos.py is idempotent: it rebuilds repos from
    scratch each time so the SHA-to-cassette mapping stays consistent.
    """
    if _repos_need_build():
        result = subprocess.run(
            [sys.executable, str(_BUILD_SCRIPT)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"build_fixture_repos.py failed:\n{result.stdout}\n{result.stderr}"
            )
    return _REPOS_DIR
