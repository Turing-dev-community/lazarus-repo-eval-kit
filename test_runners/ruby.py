"""
Ruby test runners: RSpec, Minitest.
"""

import re
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from .base import TestRunner, TestResult, TestTimeoutError
from .parsers import parse_rspec_json


def get_required_ruby_version(repo_path: Path) -> Optional[str]:
    """Extract required Ruby version from repo config files."""
    # Check .ruby-version
    ruby_version = repo_path / ".ruby-version"
    if ruby_version.exists():
        try:
            content = ruby_version.read_text().strip()
            match = re.search(r'(\d+\.\d+)', content)
            if match:
                return match.group(1)
        except Exception:
            pass

    # Check Gemfile for ruby directive
    gemfile = repo_path / "Gemfile"
    if gemfile.exists():
        try:
            content = gemfile.read_text()
            match = re.search(r'^ruby\s+["\'](\d+\.\d+)', content, re.MULTILINE)
            if match:
                return match.group(1)
        except Exception:
            pass

    return None


class RSpecRunner(TestRunner):
    """Test runner for RSpec."""

    name = "rspec"
    language = "Ruby"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses RSpec."""
        score = 0

        # Check for .rspec file
        if (repo_path / ".rspec").exists():
            score += 50

        # Check for spec directory
        spec_dir = repo_path / "spec"
        if spec_dir.exists() and spec_dir.is_dir():
            score += 30
            # Check for spec_helper.rb
            if (spec_dir / "spec_helper.rb").exists():
                score += 20

        # Check Gemfile for rspec
        gemfile = repo_path / "Gemfile"
        if gemfile.exists():
            try:
                content = gemfile.read_text()
                if "rspec" in content.lower():
                    score += 30
            except Exception:
                pass

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if Ruby is available."""
        if not self._check_command_exists("ruby"):
            return False, "Ruby not found"

        try:
            returncode, stdout, stderr = self._run_command(
                ["ruby", "--version"], Path.cwd(), timeout=10
            )
            return True, stdout.strip()
        except Exception as e:
            return False, str(e)

    def get_current_version(self) -> Optional[str]:
        """Return current Ruby version as major.minor."""
        available, version = self.check_runtime()
        if not available:
            return None
        match = re.search(r'(\d+\.\d+)', version)
        return match.group(1) if match else None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required Ruby version from repo config."""
        return get_required_ruby_version(repo_path)

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Install Ruby dependencies using bundler."""
        try:
            # Check if bundler is available
            if not self._check_command_exists("bundle"):
                # Try to install bundler
                returncode, _, stderr = self._run_command(
                    ["gem", "install", "bundler"], repo_path, timeout=60
                )
                if returncode != 0:
                    return False, f"Failed to install bundler: {stderr}"

            # Run bundle install
            cmd = ["bundle", "install"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            if returncode != 0:
                return False, f"bundle install failed: {stderr}"
            return True, ""
        except TestTimeoutError:
            return False, "bundle install timed out"
        except Exception as e:
            return False, str(e)

    def get_install_command(self, repo_path: Path) -> List[str]:
        """Return install command."""
        return ["bundle", "install"]

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        return ["bundle", "exec", "rspec", "--format", "json"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run RSpec and return results."""
        # Create temp file for JSON output
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode='w') as f:
            json_path = Path(f.name)

        try:
            cmd = [
                "bundle", "exec", "rspec",
                "--format", "json",
                "--out", str(json_path),
                "--format", "progress"  # Also show progress to stdout
            ]

            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            output = stdout + "\n" + stderr

            # Try to parse JSON output
            if json_path.exists() and json_path.stat().st_size > 0:
                try:
                    result = parse_rspec_json(json_path)
                    result.raw_output = output
                    return result
                except Exception:
                    pass

            # Fall back to parsing stdout
            result = self._parse_rspec_output(output, returncode)
            return result

        except TestTimeoutError as e:
            return TestResult(error=str(e))
        finally:
            try:
                if json_path.exists():
                    json_path.unlink()
            except Exception:
                pass

    def _parse_rspec_output(self, output: str, returncode: int) -> TestResult:
        """Parse RSpec text output as fallback."""
        import re

        passed = []
        failed = []

        # Look for summary line: X examples, Y failures
        match = re.search(r'(\d+)\s+examples?,\s+(\d+)\s+failures?', output)
        if match:
            total = int(match.group(1))
            fail_count = int(match.group(2))
            pass_count = total - fail_count
            passed = [f"example_{i}" for i in range(pass_count)]
            failed = [f"failed_example_{i}" for i in range(fail_count)]

        result = TestResult(
            passed=passed,
            failed=failed,
            raw_output=output
        )

        if result.total_tests == 0 and returncode != 0:
            result.error = f"rspec failed with exit code {returncode}"

        return result


class MinitestRunner(TestRunner):
    """Test runner for Minitest."""

    name = "minitest"
    language = "Ruby"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses Minitest."""
        score = 0

        gemfile = repo_path / "Gemfile"
        has_gemfile = gemfile.exists()
        if not has_gemfile:
            return 0

        test_dir = repo_path / "test"
        if test_dir.exists() and test_dir.is_dir():
            has_ruby_tests = any(test_dir.glob("**/*_test.rb")) or any(test_dir.glob("**/test_*.rb"))
            if has_ruby_tests:
                score += 40
            if (test_dir / "test_helper.rb").exists():
                score += 20

        rakefile = repo_path / "Rakefile"
        if rakefile.exists():
            try:
                content = rakefile.read_text()
                if "minitest" in content.lower() or "Rake::TestTask" in content:
                    score += 20
            except Exception:
                pass

        try:
            content = gemfile.read_text()
            if "minitest" in content.lower():
                score += 30
        except Exception:
            pass

        rspec_runner = RSpecRunner()
        if rspec_runner.detect(repo_path) > 50:
            score = max(0, score - 30)

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if Ruby is available."""
        if not self._check_command_exists("ruby"):
            return False, "Ruby not found"

        try:
            returncode, stdout, stderr = self._run_command(
                ["ruby", "--version"], Path.cwd(), timeout=10
            )
            return True, stdout.strip()
        except Exception as e:
            return False, str(e)

    def get_current_version(self) -> Optional[str]:
        """Return current Ruby version as major.minor."""
        available, version = self.check_runtime()
        if not available:
            return None
        match = re.search(r'(\d+\.\d+)', version)
        return match.group(1) if match else None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required Ruby version from repo config."""
        return get_required_ruby_version(repo_path)

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Install Ruby dependencies using bundler."""
        try:
            if not self._check_command_exists("bundle"):
                returncode, _, stderr = self._run_command(
                    ["gem", "install", "bundler"], repo_path, timeout=60
                )
                if returncode != 0:
                    return False, f"Failed to install bundler: {stderr}"

            cmd = ["bundle", "install"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            if returncode != 0:
                return False, f"bundle install failed: {stderr}"
            return True, ""
        except TestTimeoutError:
            return False, "bundle install timed out"
        except Exception as e:
            return False, str(e)

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        return ["bundle", "exec", "rake", "test"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run Minitest and return results."""
        try:
            cmd = ["bundle", "exec", "rake", "test"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            output = stdout + "\n" + stderr

            result = self._parse_minitest_output(output, returncode)
            return result

        except TestTimeoutError as e:
            return TestResult(error=str(e))

    def _parse_minitest_output(self, output: str, returncode: int) -> TestResult:
        """Parse Minitest output."""
        import re

        passed = []
        failed = []
        skipped = []

        # Look for summary: X runs, Y assertions, Z failures, W errors, V skips
        match = re.search(
            r'(\d+)\s+runs?,\s+(\d+)\s+assertions?,\s+(\d+)\s+failures?,\s+(\d+)\s+errors?,?\s*(\d+)?\s*skips?',
            output
        )

        if match:
            runs = int(match.group(1))
            failures = int(match.group(3))
            errors = int(match.group(4))
            skips = int(match.group(5)) if match.group(5) else 0

            pass_count = runs - failures - errors - skips
            passed = [f"test_{i}" for i in range(pass_count)]
            failed = [f"failed_test_{i}" for i in range(failures + errors)]
            skipped = [f"skipped_test_{i}" for i in range(skips)]

        result = TestResult(
            passed=passed,
            failed=failed,
            skipped=skipped,
            raw_output=output
        )

        if result.total_tests == 0 and returncode != 0:
            result.error = f"minitest failed with exit code {returncode}"

        return result
