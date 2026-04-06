"""
Go test runner.
"""

from pathlib import Path
from typing import List, Optional, Tuple

from .base import TestRunner, TestResult, TestTimeoutError
from .parsers import parse_go_test_json


class GoTestRunner(TestRunner):
    """Test runner for Go."""

    name = "go test"
    language = "Go"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses Go."""
        score = 0

        # Check for go.mod
        if (repo_path / "go.mod").exists():
            score += 50

        # Check for go.sum
        if (repo_path / "go.sum").exists():
            score += 20

        # Check for _test.go files
        test_files = list(repo_path.rglob("*_test.go"))
        if test_files:
            score += 30
            if len(test_files) > 5:
                score += 10

        return min(score, 100)

    def check_runtime(self, repo_path: Optional[Path] = None) -> Tuple[bool, str]:
        """Check if Go is available."""
        if not self._check_command_exists("go"):
            return False, "Go not found"

        try:
            cwd = repo_path if repo_path and repo_path.exists() else Path.cwd()
            returncode, stdout, stderr = self._run_command(
                ["go", "version"], cwd, timeout=10
            )
            return True, stdout.strip()
        except Exception as e:
            return False, str(e)

    def get_current_version(self) -> Optional[str]:
        """Return current Go version as major.minor."""
        import re
        available, version = self.check_runtime()
        if not available:
            return None
        match = re.search(r'go(\d+\.\d+)', version)
        return match.group(1) if match else None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required Go version from go.mod."""
        import re
        go_mod = repo_path / "go.mod"
        if go_mod.exists():
            try:
                content = go_mod.read_text()
                match = re.search(r'^go\s+(\d+\.\d+)', content, re.MULTILINE)
                if match:
                    return match.group(1)
            except Exception:
                pass
        return None

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Download Go dependencies."""
        if not (repo_path / "go.mod").exists():
            # Pre-module Go project -- no deps to download via go mod.
            # Tests can still run in GOPATH/module-unaware mode.
            return True, ""

        try:
            returncode, stdout, stderr = self._run_command(
                ["go", "mod", "download"], repo_path, timeout=timeout
            )
            if returncode != 0:
                returncode2, _, stderr2 = self._run_command(
                    ["go", "mod", "tidy"], repo_path, timeout=timeout
                )
                if returncode2 != 0:
                    return False, f"go mod download failed: {stderr}; go mod tidy failed: {stderr2}"

            return True, ""
        except TestTimeoutError:
            return False, "go mod download timed out"
        except Exception as e:
            return False, str(e)

    def get_install_command(self, repo_path: Path) -> List[str]:
        """Return install command."""
        return ["go", "mod", "download"]

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        return ["go", "test", "-json", "./..."]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run go test and return results."""
        try:
            cmd = ["go", "test", "-json", "-v", "./..."]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            output = stdout + "\n" + stderr

            # Parse JSON output
            result = parse_go_test_json(stdout)
            result.raw_output = output

            # Check for errors if no tests found
            if result.total_tests == 0:
                if "no test files" in output.lower() or "no tests to run" in output.lower():
                    result.error = "No tests found"
                elif returncode != 0:
                    result.error = f"go test failed with exit code {returncode}"

            return result

        except TestTimeoutError as e:
            return TestResult(error=str(e))
