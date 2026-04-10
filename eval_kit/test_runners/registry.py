"""
Test runner registry and auto-detection.
"""

import logging
from pathlib import Path
from typing import List, Optional, Type

from .base import TestRunner

# Import all runners
from .python import PytestRunner, UnittestRunner
from .javascript import JestRunner, VitestRunner, MochaRunner, NodeTestRunner
from .go import GoTestRunner
from .rust import CargoRunner
from .jvm import MavenRunner, GradleRunner, SbtRunner
from .ruby import RSpecRunner, MinitestRunner
from .c_cpp import CMakeRunner, MakeRunner, GoogleTestRunner
from .dotnet import DotNetRunner
from .dotnet_framework import DotNetFrameworkRunner
from .php import PHPUnitRunner, PestRunner
from .cobol import CobolCheckRunner

logger = logging.getLogger(__name__)


# All available runners, in priority order within each language.
# NOTE: For JS/TS, prefer the tool most commonly wired to `npm test` in the wild.
# Many repos may have vitest config/deps present but still run Jest via `scripts.test`.
ALL_RUNNERS: List[Type[TestRunner]] = [
    # Python (pytest preferred over unittest)
    PytestRunner,
    UnittestRunner,
    # JavaScript/TypeScript (order: jest, vitest, mocha, node:test)
    JestRunner,
    VitestRunner,
    MochaRunner,
    NodeTestRunner,
    # Go
    GoTestRunner,
    # Rust
    CargoRunner,
    # JVM (gradle preferred over maven)
    GradleRunner,
    MavenRunner,
    SbtRunner,
    # Ruby (rspec preferred over minitest)
    RSpecRunner,
    MinitestRunner,
    # PHP (pest preferred over phpunit where both exist)
    PestRunner,
    PHPUnitRunner,
    # C/C++ (cmake preferred)
    GoogleTestRunner,
    CMakeRunner,
    MakeRunner,
    # .NET (Framework first, then Core/5+)
    DotNetFrameworkRunner,
    DotNetRunner,
    # COBOL
    CobolCheckRunner,
]


# Map language names to preferred runners
LANGUAGE_RUNNERS = {
    "Python": [PytestRunner, UnittestRunner],
    "JavaScript": [JestRunner, VitestRunner, MochaRunner, NodeTestRunner],
    "TypeScript": [JestRunner, VitestRunner, MochaRunner, NodeTestRunner],
    "Go": [GoTestRunner],
    "Rust": [CargoRunner],
    "Java": [GradleRunner, MavenRunner],
    "Scala": [SbtRunner, GradleRunner],
    "Kotlin": [GradleRunner, MavenRunner],
    "Ruby": [RSpecRunner, MinitestRunner],
    "PHP": [PestRunner, PHPUnitRunner],
    "C++": [GoogleTestRunner, CMakeRunner, MakeRunner],
    "C": [CMakeRunner, MakeRunner],
    "C#": [DotNetFrameworkRunner, DotNetRunner],
    "COBOL": [CobolCheckRunner],
}


def _get_package_json_test_script(repo_path: Path) -> str:
    """
    Best-effort read of package.json `scripts.test` for tie-breaking.
    We intentionally keep this lightweight (no shared import cycles).
    """
    try:
        import json

        pkg_path = repo_path / "package.json"
        if not pkg_path.exists():
            return ""
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
        scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
        test_script = scripts.get("test", "") if isinstance(scripts, dict) else ""
        return test_script or ""
    except Exception:
        return ""


def get_runner(
    repo_path: Path, language_hint: Optional[str] = None
) -> Optional[TestRunner]:
    """
    Auto-detect and return the best test runner for a repository.

    Args:
        repo_path: Path to the repository
        language_hint: Optional language hint to narrow down runner selection

    Returns:
        TestRunner instance or None if no suitable runner found
    """
    repo_path = Path(repo_path)

    if not repo_path.exists():
        logger.error(f"Repository path does not exist: {repo_path}")
        return None

    # If we have a language hint, check those runners first
    candidates = []
    if language_hint and language_hint in LANGUAGE_RUNNERS:
        candidates = LANGUAGE_RUNNERS[language_hint]

    # Then check all runners
    candidates = candidates + [r for r in ALL_RUNNERS if r not in candidates]

    best_runner: Optional[TestRunner] = None
    best_score = 0
    best_script_match = False

    # Runners whose language matches the hint get a score boost so that,
    # e.g., pytest beats jest when the primary language is Python.
    # Any matching runner with a non-zero raw score gets at least HINT_MIN_SCORE.
    hint_runners = set()
    if language_hint and language_hint in LANGUAGE_RUNNERS:
        hint_runners = {id(cls) for cls in LANGUAGE_RUNNERS[language_hint]}
    HINT_BOOST = 30
    HINT_MIN_SCORE = 70

    test_script = _get_package_json_test_script(repo_path).lower()
    best_hint_runner: Optional[TestRunner] = None
    best_hint_score = 0

    for runner_class in candidates:
        try:
            runner = runner_class()
            raw_score = runner.detect(repo_path)
            score = raw_score

            is_hint_match = raw_score > 0 and id(runner_class) in hint_runners
            if is_hint_match:
                score = max(raw_score + HINT_BOOST, HINT_MIN_SCORE)
                score = min(score, 100)
                if score > best_hint_score:
                    best_hint_score = score
                    best_hint_runner = runner

            logger.debug(f"{runner.name}: score={score}")

            runner_script_match = bool(
                test_script and runner.name.lower() in test_script
            )

            if score > best_score:
                best_score = score
                best_runner = runner
                best_script_match = runner_script_match
            elif score == best_score and best_runner is not None:
                if is_hint_match:
                    best_runner = runner
                elif runner_script_match and not best_script_match:
                    best_runner = runner
                    best_script_match = True
        except Exception as e:
            logger.debug(f"Error detecting {runner_class.name}: {e}")
            continue

    # If a language-hint-matching runner was detected and the overall winner is
    # a different language, prefer the hint-matching runner.  The repo_evaluator
    # already performed language analysis so the hint is a strong signal.
    if (
        best_hint_runner is not None
        and best_runner is not None
        and best_hint_runner.name != best_runner.name
    ):
        logger.info(
            f"Language hint '{language_hint}' overrides: "
            f"{best_hint_runner.name} (score: {best_hint_score}) over "
            f"{best_runner.name} (score: {best_score})"
        )
        best_runner = best_hint_runner
        best_score = best_hint_score

    if best_runner and best_score >= 30:  # Minimum confidence threshold
        logger.info(f"Selected runner: {best_runner.name} (score: {best_score})")
        return best_runner

    logger.warning(f"No suitable test runner found for {repo_path}")
    return None


def get_all_detected_runners(repo_path: Path) -> List[tuple]:
    """
    Get all runners that can potentially handle this repo, with their scores.

    Returns:
        List of (runner, score) tuples, sorted by score descending
    """
    repo_path = Path(repo_path)
    results = []

    for runner_class in ALL_RUNNERS:
        try:
            runner = runner_class()
            score = runner.detect(repo_path)
            if score > 0:
                results.append((runner, score))
        except Exception:
            continue

    return sorted(results, key=lambda x: x[1], reverse=True)


def get_runner_by_name(name: str) -> Optional[TestRunner]:
    """
    Get a specific runner by name.

    Args:
        name: Runner name (e.g., "pytest", "jest", "cargo test")

    Returns:
        TestRunner instance or None
    """
    name_lower = name.lower()

    for runner_class in ALL_RUNNERS:
        runner = runner_class()
        if runner.name.lower() == name_lower:
            return runner

    return None


def list_available_runners() -> List[dict]:
    """
    List all available runners with their metadata.

    Returns:
        List of dicts with runner info
    """
    runners = []
    for runner_class in ALL_RUNNERS:
        runner = runner_class()
        runners.append(
            {
                "name": runner.name,
                "language": runner.language,
                "class": runner_class.__name__,
            }
        )
    return runners
