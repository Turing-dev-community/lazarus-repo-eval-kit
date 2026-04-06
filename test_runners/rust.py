"""
Rust test runner (cargo test).
"""

from pathlib import Path
from typing import List, Optional, Tuple

from .base import TestRunner, TestResult, TestTimeoutError
from .parsers import parse_cargo_test_output


class CargoRunner(TestRunner):
    """Test runner for Rust using cargo."""

    name = "cargo test"
    language = "Rust"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses Cargo."""
        score = 0

        # Check for Cargo.toml
        if (repo_path / "Cargo.toml").exists():
            score += 60

        # Check for Cargo.lock
        if (repo_path / "Cargo.lock").exists():
            score += 20

        # Check for src directory with .rs files
        src_dir = repo_path / "src"
        if src_dir.exists():
            rs_files = list(src_dir.rglob("*.rs"))
            if rs_files:
                score += 20

        # Check for tests directory
        tests_dir = repo_path / "tests"
        if tests_dir.exists():
            test_files = list(tests_dir.rglob("*.rs"))
            if test_files:
                score += 10

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if Rust/Cargo is available."""
        if not self._check_command_exists("cargo"):
            return False, "Cargo not found"

        try:
            returncode, stdout, stderr = self._run_command(
                ["cargo", "--version"], Path.cwd(), timeout=10
            )
            return True, stdout.strip()
        except Exception as e:
            return False, str(e)

    def get_current_version(self) -> Optional[str]:
        """Return current Rust version as major.minor."""
        import re
        if not self._check_command_exists("rustc"):
            return None
        try:
            returncode, stdout, _ = self._run_command(
                ["rustc", "--version"], Path.cwd(), timeout=10
            )
            match = re.search(r'(\d+\.\d+)', stdout)
            return match.group(1) if match else None
        except Exception:
            return None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required Rust version from config files."""
        import re

        # Check rust-toolchain.toml
        for toolchain_file in ["rust-toolchain.toml", "rust-toolchain"]:
            tc_path = repo_path / toolchain_file
            if tc_path.exists():
                try:
                    content = tc_path.read_text()
                    match = re.search(r'channel\s*=\s*["\']?(\d+\.\d+)', content)
                    if match:
                        return match.group(1)
                except Exception:
                    pass

        # Check Cargo.toml for rust-version
        cargo_toml = repo_path / "Cargo.toml"
        if cargo_toml.exists():
            try:
                content = cargo_toml.read_text()
                match = re.search(r'rust-version\s*=\s*["\'](\d+\.\d+)', content)
                if match:
                    return match.group(1)
            except Exception:
                pass

        return None

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Build Rust dependencies (cargo handles this automatically)."""
        try:
            # cargo build will fetch and compile dependencies
            returncode, stdout, stderr = self._run_command(
                ["cargo", "build"], repo_path, timeout=timeout
            )
            if returncode != 0:
                # Try cargo fetch as fallback
                returncode2, _, stderr2 = self._run_command(
                    ["cargo", "fetch"], repo_path, timeout=timeout
                )
                if returncode2 != 0:
                    return False, f"cargo build failed: {stderr}"

            return True, ""
        except TestTimeoutError:
            return False, "cargo build timed out"
        except Exception as e:
            return False, str(e)

    def get_install_command(self, repo_path: Path) -> List[str]:
        """Return install command."""
        return ["cargo", "build"]

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        return ["cargo", "test"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run cargo test and return results."""
        try:
            # Run cargo test with verbose output
            cmd = ["cargo", "test", "--", "--format=pretty"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            output = stdout + "\n" + stderr

            # Parse output
            result = parse_cargo_test_output(output)
            result.raw_output = output

            # Check for errors if no tests found
            if result.total_tests == 0:
                if "0 passed" in output and "0 failed" in output:
                    result.error = "No tests found"
                elif returncode != 0:
                    result.error = f"cargo test failed with exit code {returncode}"

            return result

        except TestTimeoutError as e:
            return TestResult(error=str(e))
