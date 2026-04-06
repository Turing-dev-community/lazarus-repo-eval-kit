"""
Base classes and data structures for test runners.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple
import subprocess
import shutil


@dataclass
class TestResult:
    """Result of running tests at a single commit."""
    passed: List[str] = field(default_factory=list)  # Test names that passed
    failed: List[str] = field(default_factory=list)  # Test names that failed
    skipped: List[str] = field(default_factory=list)  # Test names that were skipped
    duration_seconds: float = 0.0  # Total test duration
    error: Optional[str] = None  # Build/runtime error message
    raw_output: Optional[str] = None  # Raw test output for debugging
    exit_code: Optional[int] = None  # Process exit code (if available)

    @property
    def total_tests(self) -> int:
        return len(self.passed) + len(self.failed) + len(self.skipped)

    @property
    def all_passed(self) -> bool:
        return len(self.failed) == 0 and len(self.passed) > 0


@dataclass
class F2PP2PResult:
    """Result of F2P/P2P analysis for a PR."""
    pr_number: int
    pr_title: str
    base_sha: str
    head_sha: str

    # Core results
    f2p_tests: List[str] = field(default_factory=list)  # Fail → Pass (verifies fix)
    p2p_tests: List[str] = field(default_factory=list)  # Pass → Pass (regression tests)
    f2f_tests: List[str] = field(default_factory=list)  # Fail → Fail
    p2f_tests: List[str] = field(default_factory=list)  # Pass → Fail (regressions)

    # 3-run test results
    tests_base: Optional[TestResult] = None    # Pristine base
    tests_before: Optional[TestResult] = None  # Base + test files from head
    tests_after: Optional[TestResult] = None   # Full head commit

    # Status
    success: bool = False
    error: Optional[str] = None
    error_code: Optional[str] = None
    rejection_reason: Optional[str] = None

    # Metadata
    has_new_test_file: bool = False
    runner_name: Optional[str] = None
    test_file_count: Optional[int] = None
    changed_file_count: Optional[int] = None

    @property
    def has_valid_f2p(self) -> bool:
        return len(self.f2p_tests) > 0

    @property
    def has_valid_p2p(self) -> bool:
        return len(self.p2p_tests) > 0

    @property
    def verdict(self) -> str:
        if self.rejection_reason:
            return f"REJECTED:{self.rejection_reason}"
        if not self.success:
            if self.error_code == "BUILD_FAILED":
                return "BUILD_FAILED"
            elif self.error_code == "TIMEOUT":
                return "TIMEOUT"
            elif self.error_code == "NO_TESTS":
                return "NO_TESTS"
            return "UNKNOWN"
        if self.has_valid_f2p and self.has_valid_p2p:
            return "VALID"
        if not self.has_valid_f2p:
            return "NO_F2P"
        return "NO_P2P"

    def to_dict(self) -> dict:
        def stage_summary(tr: Optional[TestResult]) -> Optional[dict]:
            if tr is None:
                return None
            return {
                "passed": len(tr.passed),
                "failed": len(tr.failed),
                "skipped": len(tr.skipped),
                "total": tr.total_tests,
                "error": tr.error,
                "exit_code": tr.exit_code,
            }

        return {
            "pr_number": self.pr_number,
            "pr_title": self.pr_title,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "f2p_count": len(self.f2p_tests),
            "p2p_count": len(self.p2p_tests),
            "f2f_count": len(self.f2f_tests),
            "p2f_count": len(self.p2f_tests),
            "f2p_tests": self.f2p_tests,
            "p2p_tests": self.p2p_tests,
            "f2f_tests": self.f2f_tests,
            "p2f_tests": self.p2f_tests,
            "success": self.success,
            "verdict": self.verdict,
            "error": self.error,
            "error_code": self.error_code,
            "rejection_reason": self.rejection_reason,
            "has_new_test_file": self.has_new_test_file,
            "runner": self.runner_name,
            "test_file_count": self.test_file_count,
            "changed_file_count": self.changed_file_count,
            "stages": {
                "base": stage_summary(self.tests_base),
                "before": stage_summary(self.tests_before),
                "after": stage_summary(self.tests_after),
            },
        }


class TestRunnerError(Exception):
    """Base exception for test runner errors."""
    pass


class RuntimeNotFoundError(TestRunnerError):
    """Required runtime (python, node, etc.) not installed."""
    pass


class DependencyInstallError(TestRunnerError):
    """Failed to install dependencies."""
    pass


class BuildError(TestRunnerError):
    """Failed to build/compile the project."""
    pass


class TestTimeoutError(TestRunnerError):
    """Tests exceeded timeout."""
    pass


class OutputParseError(TestRunnerError):
    """Failed to parse test output."""
    pass


class TestRunner(ABC):
    """Abstract base class for language-specific test runners."""

    name: str = "base"
    language: str = "unknown"

    @abstractmethod
    def detect(self, repo_path: Path) -> int:
        """
        Return confidence score (0-100) that this runner can handle the repo.
        0 = cannot handle, 100 = definitely can handle.
        """
        pass

    @abstractmethod
    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """
        Install dependencies. Returns (success, error_message).
        """
        pass

    @abstractmethod
    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """
        Run tests and return structured results.
        """
        pass

    def get_install_command(self, repo_path: Path) -> List[str]:
        """Return install command for logging/debugging."""
        return []

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command for logging/debugging."""
        return []

    def check_runtime(self) -> Tuple[bool, str]:
        """
        Check if the required runtime is available.
        Returns (available, version_string or error_message).
        """
        return True, "unknown"

    def get_install_instructions(self) -> str:
        """Return instructions for installing the required runtime."""
        return f"Please install {self.language} runtime."

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Return required runtime version from repo config, or None if not specified."""
        return None

    def get_current_version(self) -> Optional[str]:
        """Return currently installed runtime version (major.minor)."""
        available, version = self.check_runtime()
        if not available:
            return None
        return version

    def check_version_compatible(self, repo_path: Path) -> Tuple[bool, Optional[str]]:
        """
        Check if installed runtime version is compatible with repo requirements.
        Returns (compatible, error_message). error_message is None if compatible.
        """
        required = self.get_required_version(repo_path)
        if not required:
            return True, None

        current = self.get_current_version()
        if not current:
            return False, f"{self.language} runtime not installed"

        if not self._versions_compatible(required, current):
            return False, f"Repo requires {self.language} {required}, but {current} is installed"

        return True, None

    def _versions_compatible(self, required: str, current: str) -> bool:
        """Check if current version satisfies required version (current >= required)."""
        try:
            req_parts = [int(x) for x in required.split('.')]
            cur_parts = [int(x) for x in current.split('.')]
            return (cur_parts[0], cur_parts[1]) >= (req_parts[0], req_parts[1])
        except (IndexError, ValueError):
            return True

    def _run_command(
        self,
        cmd: List[str],
        cwd: Path,
        timeout: int = 300,
        env: Optional[dict] = None
    ) -> Tuple[int, str, str]:
        """
        Run a shell command and return (return_code, stdout, stderr).
        """
        import os
        full_env = os.environ.copy()
        if env:
            full_env.update(env)

        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=full_env
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            raise TestTimeoutError(f"Command timed out after {timeout}s: {' '.join(cmd)}")
        except FileNotFoundError as e:
            raise RuntimeNotFoundError(f"Command not found: {cmd[0]}")

    def _check_command_exists(self, cmd: str) -> bool:
        """Check if a command exists in PATH."""
        return shutil.which(cmd) is not None
