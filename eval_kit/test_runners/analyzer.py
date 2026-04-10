"""
F2P/P2P Analyzer - Core orchestrator for test verification.
F2P/P2P implementation with 3-run test approach.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set

from eval_kit.repo_evaluator_helpers import get_language_config, is_test_file_path

from .base import F2PP2PResult, TestResult, TestRunner, TestTimeoutError
from .registry import get_runner

logger = logging.getLogger(__name__)

PASSED_STATUSES = {"PASSED", "XFAIL"}
FAILED_STATUSES = {"FAILED", "ERROR"}

INSTALL_INSTRUCTIONS = {
    "pytest": "Install Python: https://python.org/downloads/ or 'sudo apt install python3' / 'brew install python'",
    "unittest": "Install Python: https://python.org/downloads/ or 'sudo apt install python3' / 'brew install python'",
    "jest": "Install Node.js: https://nodejs.org/ or 'sudo apt install nodejs' / 'brew install node'",
    "vitest": "Install Node.js: https://nodejs.org/ or 'sudo apt install nodejs' / 'brew install node'",
    "mocha": "Install Node.js: https://nodejs.org/ or 'sudo apt install nodejs' / 'brew install node'",
    "go test": "Install Go: https://go.dev/dl/ or 'sudo apt install golang' / 'brew install go'",
    "cargo test": "Install Rust: https://rustup.rs/ or 'curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh'",
    "maven": "Install Maven & Java: 'sudo apt install maven' / 'brew install maven'",
    "gradle": "Install Gradle & Java: 'sudo apt install gradle' / 'brew install gradle'",
    "sbt": "Install sbt: https://www.scala-sbt.org/download.html or 'brew install sbt'",
    "rspec": "Install Ruby: https://www.ruby-lang.org/en/downloads/ or 'sudo apt install ruby' / 'brew install ruby'",
    "minitest": "Install Ruby: https://www.ruby-lang.org/en/downloads/ or 'sudo apt install ruby' / 'brew install ruby'",
    "cmake": "Install CMake: https://cmake.org/download/ or 'sudo apt install cmake' / 'brew install cmake'",
    "make": "Install Make: 'sudo apt install build-essential' / 'xcode-select --install' (macOS)",
    "googletest": "Install CMake: https://cmake.org/download/ or 'sudo apt install cmake' / 'brew install cmake'",
    "dotnet test": "Install .NET SDK: https://dotnet.microsoft.com/download or 'sudo apt install dotnet-sdk-8.0'",
    "cobol-check": "Install GnuCOBOL: 'sudo apt install gnucobol' and cobol-check from https://github.com/openmainframeproject/cobol-check",
}

PROJECT_MARKERS = [
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "Gemfile",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
]


def _is_project_dir(path: Path) -> bool:
    return any((path / marker).exists() for marker in PROJECT_MARKERS)


def _extract_package_from_path(file_path: str, repo_path: Path) -> Optional[Path]:
    parts = file_path.split("/")
    if len(parts) < 2:
        return None
    candidate = repo_path / parts[0]
    if candidate.is_dir() and _is_project_dir(candidate):
        return candidate
    return None


def _get_affected_packages(changed_files: List[str], repo_path: Path) -> List[Path]:
    packages: Set[Path] = set()
    for f in changed_files:
        pkg = _extract_package_from_path(f, repo_path)
        if pkg:
            packages.add(pkg)
    if not packages:
        if _is_project_dir(repo_path):
            return [repo_path]
        for sub in repo_path.iterdir():
            if sub.is_dir() and _is_project_dir(sub):
                packages.add(sub)
    return sorted(packages)


def _test_passed(test: str, status_map: Dict[str, str]) -> bool:
    return test in status_map and status_map[test] in PASSED_STATUSES


def _test_failed(test: str, status_map: Dict[str, str]) -> bool:
    return test not in status_map or status_map[test] in FAILED_STATUSES


def generate_test_report(
    tests_base: Dict[str, str],
    tests_before: Dict[str, str],
    tests_after: Dict[str, str],
    has_new_test_file: bool = False,
) -> Dict[str, List[str]]:
    """
    Generate F2P/P2P/F2F/P2F report using 3-run logic.

    Args:
        tests_base: Test results at pristine base commit
        tests_before: Test results at base with test files from head applied
        tests_after: Test results at head commit (full solution)
        has_new_test_file: Whether any test file is newly added

    Returns:
        Dict with keys: FAIL_TO_PASS, PASS_TO_PASS, FAIL_TO_FAIL, PASS_TO_FAIL
    """
    result = {
        "FAIL_TO_PASS": [],
        "PASS_TO_PASS": [],
        "PASS_TO_FAIL": [],
        "FAIL_TO_FAIL": [],
    }

    has_mixed_before = any(s in PASSED_STATUSES for s in tests_before.values()) and any(
        s in FAILED_STATUSES for s in tests_before.values()
    )

    if has_new_test_file or not has_mixed_before:
        base_passing = {t for t, s in tests_base.items() if s in PASSED_STATUSES}
        after_passing = {t for t, s in tests_after.items() if s in PASSED_STATUSES}

        fail_to_pass = [t for t in after_passing if t not in base_passing]
        pass_to_pass = [t for t in after_passing if t in base_passing]

        before_passing = {t for t, s in tests_before.items() if s in PASSED_STATUSES}
        reclassify_to_p2p = [t for t in fail_to_pass if t in before_passing]
        if reclassify_to_p2p:
            fail_to_pass = [t for t in fail_to_pass if t not in reclassify_to_p2p]
            seen = set(pass_to_pass)
            for t in reclassify_to_p2p:
                if t not in seen:
                    pass_to_pass.append(t)
                    seen.add(t)

        before_failing = {t for t, s in tests_before.items() if s in FAILED_STATUSES}
        reclassify_to_f2p = [t for t in pass_to_pass if t in before_failing]
        if reclassify_to_f2p:
            pass_to_pass = [t for t in pass_to_pass if t not in reclassify_to_f2p]
            seen = set(fail_to_pass)
            for t in reclassify_to_f2p:
                if t not in seen:
                    fail_to_pass.append(t)
                    seen.add(t)

        result["FAIL_TO_PASS"] = fail_to_pass
        result["PASS_TO_PASS"] = pass_to_pass
    else:
        all_tests = set(tests_before.keys()) | set(tests_after.keys())
        for test in all_tests:
            status_before = tests_before.get(test)
            status_after = tests_after.get(test)

            if status_before in FAILED_STATUSES and status_after in PASSED_STATUSES:
                result["FAIL_TO_PASS"].append(test)
            elif status_before in PASSED_STATUSES and status_after in PASSED_STATUSES:
                result["PASS_TO_PASS"].append(test)
            elif status_before in PASSED_STATUSES and status_after in FAILED_STATUSES:
                result["PASS_TO_FAIL"].append(test)
            elif status_before in FAILED_STATUSES and status_after in FAILED_STATUSES:
                result["FAIL_TO_FAIL"].append(test)

    return result


def _result_to_status_map(result: TestResult) -> Dict[str, str]:
    status_map = {}
    for t in result.passed:
        status_map[t] = "PASSED"
    for t in result.failed:
        status_map[t] = "FAILED"
    for t in result.skipped:
        status_map[t] = "SKIPPED"
    return status_map


UNSTABLE_PATTERNS = [
    r"\d{10,13}",  # Unix timestamps (10-13 digits)
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",  # ISO dates
    r"built in \d+(\.\d+)?s",  # Build times
    r"in \d+(\.\d+)?\s*(ms|s|sec|seconds)",  # Duration patterns
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",  # UUIDs
    r"0x[a-f0-9]{8,}",  # Memory addresses
]


def _infer_exit_code(result: TestResult) -> Optional[int]:
    """Best-effort exit code inference from error/output when runners don't set it."""
    import re

    haystacks = [result.error or "", result.raw_output or ""]
    for text in haystacks:
        if not text:
            continue
        match = re.search(
            r"\b(?:exit|error)\s+code\b[:\s]*([0-9]+)", text, re.IGNORECASE
        )
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue
    return None


def _is_install_error(result: TestResult) -> bool:
    if not result.error:
        return False
    msg = result.error.lower()
    return (
        msg.startswith("install failed")
        or msg.startswith("install timeout")
        or msg.startswith("install error")
    )


def _has_unstable_pattern(test_name: str) -> bool:
    import re

    for pattern in UNSTABLE_PATTERNS:
        if re.search(pattern, test_name, re.IGNORECASE):
            return True
    return False


def validate_f2p_p2p_result(
    f2p_tests: List[str],
    p2p_tests: List[str],
    tests_base: Dict[str, str],
    tests_before: Dict[str, str],
    tests_after: Dict[str, str],
    language: Optional[str] = None,
) -> Optional[str]:
    """
    Validate F2P/P2P results and return rejection reason if invalid.
    Returns None if valid, otherwise returns the rejection reason string.
    """
    if not f2p_tests:
        return "empty_f2p"
    if not p2p_tests:
        return "empty_p2p"

    all_f2p_p2p = f2p_tests + p2p_tests

    # Unstable test names (JS/TS/C++ only)
    if language and language.lower() in ("javascript", "typescript", "c++", "cpp"):
        for test in all_f2p_p2p:
            if _has_unstable_pattern(test):
                return "unstable_test_name"

    # Duplicate test names
    if len(all_f2p_p2p) != len(set(all_f2p_p2p)):
        return "duplicate_test_names"

    # Failed test in base present in P2P
    base_failed = {t for t, s in tests_base.items() if s in FAILED_STATUSES}
    for test in p2p_tests:
        if test in base_failed:
            return "failed_base_in_p2p"

    # Failed or missing test in after present in F2P/P2P
    after_failed = {t for t, s in tests_after.items() if s in FAILED_STATUSES}
    after_all = set(tests_after.keys())
    for test in all_f2p_p2p:
        if test in after_failed or test not in after_all:
            return "failed_after_in_f2p_p2p"

    # P2P missing in base, not passing in before
    base_all = set(tests_base.keys())
    before_passed = {t for t, s in tests_before.items() if s in PASSED_STATUSES}
    for test in p2p_tests:
        if test not in base_all:
            if test not in before_passed:
                return "p2p_missing_base_not_passing_before"

    # Test didn't run in all 3 stages
    for test in all_f2p_p2p:
        if (
            test not in tests_base
            and test not in tests_before
            and test not in tests_after
        ):
            return "test_not_in_all_stages"
        ran_count = sum([test in tests_base, test in tests_before, test in tests_after])
        if ran_count < 3:
            in_new_test_file = test not in tests_base
            if not in_new_test_file:
                return "test_not_in_all_stages"

    return None


class F2PP2PAnalyzer:
    def __init__(
        self,
        repo_path: Path,
        runner: Optional[TestRunner] = None,
        install_timeout: int = 300,
        test_timeout: int = 600,
        language_hint: Optional[str] = None,
    ):
        self.repo_path = Path(repo_path)
        self._provided_runner = runner
        self.install_timeout = install_timeout
        self.test_timeout = test_timeout
        self.language_hint = language_hint
        self.language_config = (
            get_language_config(language_hint) if language_hint else {}
        )

    def analyze(
        self,
        pr_number: int,
        pr_title: str,
        base_sha: str,
        head_sha: str,
        pr_files: Optional[List[str]] = None,
        single_pr_mode: bool = False,
    ) -> F2PP2PResult:
        result = F2PP2PResult(
            pr_number=pr_number,
            pr_title=pr_title,
            base_sha=base_sha,
            head_sha=head_sha,
        )

        # Reset to default branch to ensure clean state for runner detection
        try:
            # Get default branch name
            branch_result = subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            default_branch = (
                branch_result.stdout.strip().replace("origin/", "")
                if branch_result.returncode == 0
                else "main"
            )
            subprocess.run(
                ["git", "checkout", default_branch, "--force"],
                cwd=self.repo_path,
                capture_output=True,
                timeout=30,
            )
        except Exception:
            pass

        changed_files = (
            pr_files if pr_files else self._get_all_changed_files(base_sha, head_sha)
        )
        if not changed_files:
            result.error = "Could not get changed files from PR"
            result.error_code = "NO_CHANGED_FILES"
            return result

        test_files = self._filter_test_files(changed_files)
        if not test_files:
            result.error = "No test files changed in PR"
            result.error_code = "NO_TEST_FILES"
            return result

        new_test_files = self._get_new_files(base_sha, head_sha, test_files)
        has_new_test_file = len(new_test_files) > 0
        result.has_new_test_file = has_new_test_file

        affected_packages = _get_affected_packages(test_files, self.repo_path)
        if not affected_packages:
            affected_packages = [self.repo_path]

        logger.info(
            f"Affected packages: {[str(p.relative_to(self.repo_path)) if p != self.repo_path else '.' for p in affected_packages]}"
        )
        logger.info(
            f"Found {len(test_files)} changed test files ({len(new_test_files)} new)"
        )

        all_tests_base: Dict[str, str] = {}
        all_tests_before: Dict[str, str] = {}
        all_tests_after: Dict[str, str] = {}
        stage_exit_codes = {"base": [], "before": [], "after": []}
        stage_errors = {"base": [], "before": [], "after": []}
        errors = []
        packages_tested = 0
        packages_no_runner = []

        for pkg_path in affected_packages:
            pkg_name = (
                str(pkg_path.relative_to(self.repo_path))
                if pkg_path != self.repo_path
                else "."
            )
            logger.info(f"Testing package: {pkg_name}")

            runner = self._get_runner_for_package(pkg_path)
            if runner is None:
                logger.debug(f"No test runner for {pkg_name}, skipping")
                packages_no_runner.append(pkg_name)
                continue

            runtime_ok, runtime_msg = runner.check_runtime()
            if not runtime_ok:
                install_hint = INSTALL_INSTRUCTIONS.get(
                    runner.name, f"Please install {runner.language} runtime"
                )
                logger.warning(
                    f"  ⚠️  {runner.name} runtime not available. {install_hint}"
                )
                errors.append(f"{pkg_name}: Runtime not available - {runtime_msg}")
                continue

            version_ok, version_msg = runner.check_version_compatible(pkg_path)
            if not version_ok:
                logger.warning(f"  ⚠️  {version_msg}")
                result.error = version_msg
                result.error_code = "RUNTIME_VERSION_MISMATCH"
                return result

            logger.info(f"  Using runner: {runner.name}")
            # Record runner used (first package wins; typically there's only one)
            if result.runner_name is None:
                result.runner_name = runner.name
            pkg_test_files = [
                f
                for f in test_files
                if f.startswith(pkg_name + "/") or pkg_path == self.repo_path
            ]
            prefix = f"[{pkg_name}] " if pkg_path != self.repo_path else ""

            # Run 1: tests_base (pristine base)
            logger.info(f"  [1/3] Checking out base (pristine): {base_sha[:8]}")
            base_result = self._run_at_commit(base_sha, "base", runner, pkg_path)
            if base_result.error and "checkout" in base_result.error.lower():
                errors.append(f"{pkg_name} base: {base_result.error}")
                continue
            if base_result.exit_code is not None:
                stage_exit_codes["base"].append(base_result.exit_code)
            if base_result.error:
                stage_errors["base"].append(base_result.error)
            if _is_install_error(base_result):
                result.tests_base = base_result
                result.error = f"{pkg_name} base: {base_result.error}"
                result.error_code = "INSTALL_FAILED"
                return result
            for t in base_result.passed:
                all_tests_base[prefix + t] = "PASSED"
            for t in base_result.failed:
                all_tests_base[prefix + t] = "FAILED"
            for t in base_result.skipped:
                all_tests_base[prefix + t] = "SKIPPED"
            if (
                base_result.exit_code is not None
                and base_result.exit_code != 0
                and not base_result.passed
                and not base_result.failed
            ):
                result.tests_base = base_result
                result.error = (
                    f"{pkg_name} base: tests exited with code {base_result.exit_code}"
                )
                result.error_code = "TEST_EXIT_NONZERO"
                return result

            # Run 2: tests_before (base + test files from head)
            logger.info("  [2/3] Applying test patch from head to base")
            before_result = self._run_at_commit(
                base_sha,
                "before",
                runner,
                pkg_path,
                apply_test_files=pkg_test_files,
                head_sha=head_sha,
            )
            if before_result.error and "checkout" in before_result.error.lower():
                errors.append(f"{pkg_name} before: {before_result.error}")
                continue
            if before_result.exit_code is not None:
                stage_exit_codes["before"].append(before_result.exit_code)
            if before_result.error:
                stage_errors["before"].append(before_result.error)
            if _is_install_error(before_result):
                result.tests_base = base_result
                result.tests_before = before_result
                result.error = f"{pkg_name} before: {before_result.error}"
                result.error_code = "INSTALL_FAILED"
                return result
            for t in before_result.passed:
                all_tests_before[prefix + t] = "PASSED"
            for t in before_result.failed:
                all_tests_before[prefix + t] = "FAILED"
            for t in before_result.skipped:
                all_tests_before[prefix + t] = "SKIPPED"
            if (
                before_result.exit_code is not None
                and before_result.exit_code != 0
                and not before_result.passed
                and not before_result.failed
            ):
                result.tests_base = base_result
                result.tests_before = before_result
                result.error = f"{pkg_name} before: tests exited with code {before_result.exit_code}"
                result.error_code = "TEST_EXIT_NONZERO"
                return result

            # Run 3: tests_after (full head commit)
            after_use_apply = os.environ.get(
                "REPO_EVAL_AFTER_USE_GIT_APPLY", "1"
            ).strip() in ("1", "true", "True")
            if after_use_apply:
                logger.info(
                    "  [3/3] Applying full patch from head to base for after stage"
                )
                after_result = self._run_at_commit(
                    base_sha,
                    "after",
                    runner,
                    pkg_path,
                    head_sha=head_sha,
                    apply_full_patch=True,
                )
            # NOTE: Disabled non-patch AFTER path. We always want base checkout + patch apply.
            # else:
            #     logger.info(f"  [3/3] Checking out head: {head_sha[:8]}")
            #     after_result = self._run_at_commit(head_sha, "after", runner, pkg_path)
            if after_result.error and "checkout" in after_result.error.lower():
                errors.append(f"{pkg_name} after: {after_result.error}")
                continue
            if after_result.exit_code is not None:
                stage_exit_codes["after"].append(after_result.exit_code)
            if after_result.error:
                stage_errors["after"].append(after_result.error)
            if _is_install_error(after_result) and single_pr_mode:
                result.tests_base = base_result
                result.tests_before = before_result
                result.tests_after = after_result
                result.error = f"{pkg_name} after: {after_result.error}"
                result.error_code = "INSTALL_FAILED"
                return result
            for t in after_result.passed:
                all_tests_after[prefix + t] = "PASSED"
            for t in after_result.failed:
                all_tests_after[prefix + t] = "FAILED"
            for t in after_result.skipped:
                all_tests_after[prefix + t] = "SKIPPED"
            if (
                after_result.exit_code is not None
                and after_result.exit_code != 0
                and single_pr_mode
            ):
                result.tests_base = base_result
                result.tests_before = before_result
                result.tests_after = after_result
                result.error = (
                    f"{pkg_name} after: tests exited with code {after_result.exit_code}"
                )
                result.error_code = "TEST_EXIT_NONZERO"
                return result

            packages_tested += 1

            if base_result.error and before_result.error and after_result.error:
                errors.append(f"{pkg_name}: Tests failed at all commits")

        if packages_tested == 0:
            supported = "JavaScript/TypeScript, Python, Go, Rust, Ruby, Java, .NET, C/C++, Scala, PHP, COBOL"
            if packages_no_runner:
                result.error = f"No supported test runner found for: {', '.join(packages_no_runner)}. Supported: {supported}"
                result.error_code = "NO_TEST_RUNNER"
            elif errors:
                result.error = "; ".join(errors)
                result.error_code = "BUILD_FAILED"
            else:
                result.error = (
                    f"No test runner detected. Supported languages: {supported}"
                )
                result.error_code = "NO_TEST_RUNNER"
            return result

        if not all_tests_after and errors:
            result.error = "; ".join(errors)
            result.error_code = "BUILD_FAILED"
            return result

        result.tests_base = TestResult(
            passed=[t for t, s in all_tests_base.items() if s == "PASSED"],
            failed=[t for t, s in all_tests_base.items() if s == "FAILED"],
            skipped=[t for t, s in all_tests_base.items() if s == "SKIPPED"],
        )
        result.tests_before = TestResult(
            passed=[t for t, s in all_tests_before.items() if s == "PASSED"],
            failed=[t for t, s in all_tests_before.items() if s == "FAILED"],
            skipped=[t for t, s in all_tests_before.items() if s == "SKIPPED"],
        )
        result.tests_after = TestResult(
            passed=[t for t, s in all_tests_after.items() if s == "PASSED"],
            failed=[t for t, s in all_tests_after.items() if s == "FAILED"],
            skipped=[t for t, s in all_tests_after.items() if s == "SKIPPED"],
        )

        # Preserve stage-level exit codes/errors (otherwise stage summaries can look "healthy"
        # even when most suites fail to load and produce 0 assertions).
        for stage, tr in [
            ("base", result.tests_base),
            ("before", result.tests_before),
            ("after", result.tests_after),
        ]:
            ecs = stage_exit_codes.get(stage) or []
            tr.exit_code = max(ecs) if ecs else None
            errs = stage_errors.get(stage) or []
            tr.error = errs[0] if errs else None

        report = generate_test_report(
            all_tests_base, all_tests_before, all_tests_after, has_new_test_file
        )

        result.f2p_tests = sorted(report["FAIL_TO_PASS"])
        result.p2p_tests = sorted(report["PASS_TO_PASS"])
        result.f2f_tests = sorted(report["FAIL_TO_FAIL"])
        result.p2f_tests = sorted(report["PASS_TO_FAIL"])
        result.test_file_count = len(test_files)
        result.changed_file_count = len(changed_files)

        # Debug signal: distinguish "true" F2P (failed in BEFORE) from tests that weren't collected in BEFORE.
        # This helps diagnose cases where new tests only exist at AFTER or fail to load at BEFORE.
        before_all = set(all_tests_before.keys())
        f2p_not_collected = [t for t in result.f2p_tests if t not in before_all]
        f2p_failed = [
            t
            for t in result.f2p_tests
            if (t in before_all and all_tests_before.get(t) in FAILED_STATUSES)
        ]
        if f2p_not_collected or f2p_failed:
            logger.info(
                f"  F2P breakdown: {len(f2p_failed)} failed in before, {len(f2p_not_collected)} not collected in before"
            )

        language = None
        if affected_packages:
            pkg_runner = self._get_runner_for_package(affected_packages[0])
            if pkg_runner:
                language = pkg_runner.language

        rejection = validate_f2p_p2p_result(
            result.f2p_tests,
            result.p2p_tests,
            all_tests_base,
            all_tests_before,
            all_tests_after,
            language,
        )

        if rejection:
            result.rejection_reason = rejection
            result.success = False
            logger.info(f"PR #{pr_number} rejected: {rejection}")
        else:
            result.success = True
            logger.info(f"Analysis complete for PR #{pr_number}")

        logger.info(f"  F2P tests: {len(result.f2p_tests)}")
        logger.info(f"  P2P tests: {len(result.p2p_tests)}")
        logger.info(f"  Verdict: {result.verdict}")

        return result

    def _get_runner_for_package(self, pkg_path: Path) -> Optional[TestRunner]:
        # If a runner was explicitly provided (e.g. by repo_evaluator.py), honor it.
        if self._provided_runner is not None:
            return self._provided_runner
        return get_runner(pkg_path, self.language_hint)

    def _get_all_changed_files(self, base_sha: str, head_sha: str) -> List[str]:
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return []
            return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        except Exception as e:
            logger.debug(f"Error getting changed files: {e}")
            return []

    def _get_new_files(
        self, base_sha: str, head_sha: str, test_files: List[str]
    ) -> List[str]:
        new_files = []
        try:
            result = subprocess.run(
                [
                    "git",
                    "diff",
                    "--name-only",
                    "--diff-filter=A",
                    f"{base_sha}...{head_sha}",
                ],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                added = set(
                    f.strip() for f in result.stdout.strip().split("\n") if f.strip()
                )
                new_files = [f for f in test_files if f in added]
        except Exception as e:
            logger.debug(f"Error getting new files: {e}")
        return new_files

    def _filter_test_files(self, changed_files: List[str]) -> List[str]:
        test_files = []
        for f in changed_files:
            if self.language_config:
                if is_test_file_path(f, self.language_config):
                    test_files.append(f)
            else:
                if any(p in f.lower() for p in ["test", "spec", "__tests__"]):
                    test_files.append(f)
        return test_files

    def _get_deleted_files(self, base_sha: str, head_sha: str) -> Set[str]:
        """Get files that were deleted between base and head.

        Note: We use `base...head` to match the rest of the analyzer diff logic.
        """
        deleted: Set[str] = set()
        try:
            result = subprocess.run(
                [
                    "git",
                    "diff",
                    "--name-only",
                    "--diff-filter=D",
                    f"{base_sha}...{head_sha}",
                ],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                deleted = {
                    f.strip() for f in (result.stdout or "").splitlines() if f.strip()
                }
        except Exception as e:
            logger.debug(f"Error getting deleted files: {e}")
        return deleted

    def _generate_test_patch(
        self, base_sha: str, head_sha: str, test_files: List[str]
    ) -> str:
        """Generate a patch for selected test file paths.

        We include `--binary` so `git apply` has full metadata for binary diffs.
        We use base..head (two-dot) because we want the patch that transforms base -> head.
        """
        if not test_files:
            return ""

        try:
            result = subprocess.run(
                [
                    "git",
                    "diff",
                    "--binary",
                    f"{base_sha}..{head_sha}",
                    "--",
                    *test_files,
                ],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=60,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                err = (result.stderr or "").strip()
                raise RuntimeError(err or f"git diff failed (exit {result.returncode})")
            return result.stdout or ""
        except Exception as e:
            raise RuntimeError(f"Failed to generate test patch: {e}")

    def _apply_test_patch_from_head(
        self, test_files: List[str], base_sha: str, head_sha: str
    ):
        """Apply a test patch onto the current working tree (expected to be at base_sha)."""
        if not test_files:
            return

        patch = self._generate_test_patch(base_sha, head_sha, test_files)
        if not patch.strip():
            logger.info(
                "    No test patch to apply (diff for selected test files is empty)"
            )
            return

        result = subprocess.run(
            ["git", "apply", "--verbose", "--reject", "--whitespace=nowarn", "-"],
            cwd=self.repo_path,
            input=patch,
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            details = "\n".join([s for s in [stdout, stderr] if s])
            raise RuntimeError(
                details or f"git apply failed (exit {result.returncode})"
            )

        logger.info(f"    Applied test patch for {len(test_files)} test file path(s)")

    def _generate_full_patch(self, base_sha: str, head_sha: str) -> str:
        """Generate a full patch (tests + source) transforming base -> head."""
        try:
            result = subprocess.run(
                ["git", "diff", "--binary", f"{base_sha}..{head_sha}"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=120,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                err = (result.stderr or "").strip()
                raise RuntimeError(err or f"git diff failed (exit {result.returncode})")
            return result.stdout or ""
        except Exception as e:
            raise RuntimeError(f"Failed to generate full patch: {e}")

    def _apply_full_patch_from_head(self, base_sha: str, head_sha: str):
        """Apply a full patch onto the current working tree (expected to be at base_sha)."""
        patch = self._generate_full_patch(base_sha, head_sha)
        if not patch.strip():
            logger.info("    No full patch to apply (base..head diff is empty)")
            return

        result = subprocess.run(
            ["git", "apply", "--verbose", "--reject", "--whitespace=nowarn", "-"],
            cwd=self.repo_path,
            input=patch,
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            details = "\n".join([s for s in [stdout, stderr] if s])
            raise RuntimeError(
                details or f"git apply failed (exit {result.returncode})"
            )

        logger.info("    Applied full patch from head")

    def _apply_test_files_from_head(
        self, test_files: List[str], head_sha: str, base_sha: Optional[str] = None
    ):
        if not test_files:
            return

        # Filter out deleted files (can appear in changed_files via diff, but cannot be checked out from head).
        files_to_apply = list(test_files)
        if base_sha:
            deleted_files = self._get_deleted_files(base_sha, head_sha)
            if deleted_files:
                before = len(files_to_apply)
                files_to_apply = [f for f in files_to_apply if f not in deleted_files]
                skipped = before - len(files_to_apply)
                if skipped:
                    logger.debug(
                        f"Skipping {skipped} deleted test file(s) when applying from head"
                    )

        if not files_to_apply:
            logger.info(
                "    No test files to apply (all candidate test files were deleted)"
            )
            return

        applied = 0
        failed: List[str] = []

        try:
            # Apply one-by-one so a single missing/bad pathspec doesn't block all test files.
            for f in files_to_apply:
                cmd = ["git", "checkout", head_sha, "--", f]
                result = subprocess.run(
                    cmd,
                    cwd=self.repo_path,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    encoding="utf-8",
                    errors="replace",
                )
                if result.returncode == 0:
                    applied += 1
                else:
                    failed.append(f)
                    err = (result.stderr or "").strip().splitlines()
                    msg = err[-1] if err else "no error output"
                    logger.warning(
                        f"    Failed to apply test file from head: {f} (exit {result.returncode}): {msg}"
                    )

            if applied:
                logger.info(
                    f"    Applied {applied}/{len(files_to_apply)} test files from head"
                )
            if failed and applied == 0:
                logger.warning(
                    "    No test files could be applied from head; BEFORE stage may match BASE stage (F2P/P2P may be unreliable)"
                )
        except Exception as e:
            logger.debug(f"Error applying test files: {e}")

    def _run_at_commit(
        self,
        sha: str,
        label: str,
        runner: TestRunner,
        pkg_path: Path,
        apply_test_files: Optional[List[str]] = None,
        head_sha: Optional[str] = None,
        apply_full_patch: bool = False,
    ) -> TestResult:
        try:
            self._git_checkout(sha)
        except Exception as e:
            return TestResult(error=f"Checkout failed: {e}")

        if apply_full_patch:
            if not head_sha:
                return TestResult(
                    error="Patch Apply Failed:\nMissing head_sha for full patch apply"
                )
            try:
                self._apply_full_patch_from_head(base_sha=sha, head_sha=head_sha)
            except Exception as e:
                return TestResult(error=f"Patch Apply Failed:\n{e}")

        elif apply_test_files and head_sha:
            # BEFORE semantics: checkout base, then apply a patch of test changes.
            # Allow opting out for troubleshooting/legacy behavior.
            use_git_apply = os.environ.get(
                "REPO_EVAL_BEFORE_USE_GIT_APPLY", "1"
            ).strip() not in ("0", "false", "False")
            if use_git_apply:
                try:
                    self._apply_test_patch_from_head(
                        apply_test_files, base_sha=sha, head_sha=head_sha
                    )
                except Exception as e:
                    return TestResult(error=f"Patch Apply Failed:\n{e}")
            else:
                self._apply_test_files_from_head(
                    apply_test_files, head_sha, base_sha=sha
                )

        logger.info(f"Installing dependencies at {label} ({sha[:8]}) in {pkg_path}...")
        try:
            success, error_msg = runner.install_deps(
                pkg_path, timeout=self.install_timeout
            )
            if not success:
                logger.error(f"Install failed: {error_msg}")
                return TestResult(error=f"Install failed: {error_msg}")
        except TestTimeoutError as e:
            logger.error(f"Install timeout: {e}")
            return TestResult(error=f"Install timeout: {e}")
        except Exception as e:
            logger.error(f"Install error: {e}")
            return TestResult(error=f"Install error: {e}")

        logger.info(f"Running tests at {label} ({sha[:8]}) in {pkg_path}...")
        try:
            result = runner.run_tests(pkg_path, timeout=self.test_timeout)
            if result.exit_code is None:
                inferred = _infer_exit_code(result)
                print(f"inferred exit code: {inferred}")
                if inferred is not None:
                    result.exit_code = inferred
            logger.info(
                f"    {label}: {len(result.passed)} passed, "
                f"{len(result.failed)} failed, "
                f"{len(result.skipped)} skipped"
            )
            return result
        except TestTimeoutError as e:
            logger.error(f"Test timeout: {e}")
            return TestResult(error=f"Test timeout: {e}", exit_code=1)
        except Exception as e:
            logger.error(f"Test error: {e}")
            return TestResult(error=f"Test error: {e}", exit_code=1)

    def _git_checkout(self, sha: str):
        result = subprocess.run(
            ["git", "cat-file", "-t", sha],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            logger.debug(f"Commit {sha[:8]} not found locally, fetching...")
            subprocess.run(
                ["git", "fetch", "origin", sha],
                cwd=self.repo_path,
                capture_output=True,
                timeout=120,
            )

        subprocess.run(
            ["git", "reset", "--hard"],
            cwd=self.repo_path,
            capture_output=True,
            timeout=30,
        )

        subprocess.run(
            ["git", "clean", "-fd"], cwd=self.repo_path, capture_output=True, timeout=30
        )

        result = subprocess.run(
            ["git", "checkout", sha],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            raise RuntimeError(f"git checkout failed: {result.stderr}")


def analyze_f2p_p2p(
    repo_path: str,
    base_sha: str,
    head_sha: str,
    pr_number: int = 0,
    pr_title: str = "",
    timeout: int = 600,
    language_hint: Optional[str] = None,
) -> F2PP2PResult:
    analyzer = F2PP2PAnalyzer(
        repo_path=Path(repo_path),
        test_timeout=timeout,
        language_hint=language_hint,
    )
    return analyzer.analyze(
        pr_number=pr_number,
        pr_title=pr_title,
        base_sha=base_sha,
        head_sha=head_sha,
    )


def preflight_check(repo_path: str, language_hint: Optional[str] = None) -> dict:
    from .registry import LANGUAGE_RUNNERS, get_all_detected_runners

    repo_path = Path(repo_path)
    blockers = []
    warnings = []
    detected = {}

    if not repo_path.exists():
        return {
            "can_run": False,
            "blockers": [
                {
                    "code": "REPO_NOT_FOUND",
                    "message": f"Repository not found: {repo_path}",
                }
            ],
            "warnings": [],
            "detected": {},
        }

    runners = get_all_detected_runners(repo_path)

    normalized_hint = language_hint
    if language_hint and language_hint not in LANGUAGE_RUNNERS:
        for known_language in LANGUAGE_RUNNERS:
            if known_language.lower() == language_hint.lower():
                normalized_hint = known_language
                break

    if normalized_hint and normalized_hint in LANGUAGE_RUNNERS:
        allowed = tuple(LANGUAGE_RUNNERS[normalized_hint])
        filtered = [
            (runner, score) for runner, score in runners if isinstance(runner, allowed)
        ]
        runners = filtered
        if not filtered:
            warnings.append(
                {
                    "code": "LANGUAGE_HINT_NO_MATCH",
                    "message": f"No runners matched language hint: {normalized_hint}",
                }
            )

    if not runners:
        blockers.append(
            {"code": "NO_TEST_FRAMEWORK", "message": "No test framework detected"}
        )
    else:
        best_runner, best_score = runners[0]
        detected["framework"] = best_runner.name
        detected["language"] = best_runner.language
        detected["confidence"] = best_score

        runtime_ok, runtime_msg = best_runner.check_runtime()
        if not runtime_ok:
            install_hint = INSTALL_INSTRUCTIONS.get(
                best_runner.name, f"Please install {best_runner.language} runtime"
            )
            blockers.append(
                {
                    "code": "MISSING_RUNTIME",
                    "message": f"{best_runner.language} runtime not found: {runtime_msg}",
                    "install_hint": install_hint,
                }
            )
        else:
            detected["runtime"] = runtime_msg

    lock_files = [
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Pipfile.lock",
        "poetry.lock",
        "Cargo.lock",
        "Gemfile.lock",
    ]
    if not any((repo_path / lf).exists() for lf in lock_files):
        warnings.append({"code": "NO_LOCK_FILE", "message": "No lock file found"})

    if (repo_path / "docker-compose.yml").exists() or (
        repo_path / "docker-compose.yaml"
    ).exists():
        warnings.append(
            {"code": "DOCKER_REQUIRED", "message": "docker-compose.yml found"}
        )

    if (repo_path / ".env.example").exists() or (repo_path / ".env.sample").exists():
        warnings.append(
            {
                "code": "ENV_VARS_NEEDED",
                "message": "Environment variables may be required",
            }
        )

    return {
        "can_run": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
        "detected": detected,
    }
