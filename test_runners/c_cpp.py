"""
C/C++ test runners: CMake/CTest, Make, GoogleTest.
"""

import re
from pathlib import Path
from typing import List, Tuple

from .base import TestRunner, TestResult, TestTimeoutError
from .parsers import parse_junit_xml


class CMakeRunner(TestRunner):
    """Test runner for CMake/CTest."""

    name = "cmake"
    language = "C++"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses CMake."""
        score = 0

        # Check for CMakeLists.txt
        if (repo_path / "CMakeLists.txt").exists():
            score += 60

        # Check for build directory with cmake cache
        build_dir = repo_path / "build"
        if build_dir.exists():
            if (build_dir / "CMakeCache.txt").exists():
                score += 20

        # Check CMakeLists.txt for testing setup
        cmake_file = repo_path / "CMakeLists.txt"
        if cmake_file.exists():
            try:
                content = cmake_file.read_text()
                if "enable_testing" in content.lower() or "add_test" in content.lower():
                    score += 30
                if "gtest" in content.lower() or "googletest" in content.lower():
                    score += 10
            except Exception:
                pass

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if CMake is available."""
        if not self._check_command_exists("cmake"):
            return False, "CMake not found"

        try:
            returncode, stdout, stderr = self._run_command(
                ["cmake", "--version"], Path.cwd(), timeout=10
            )
            return True, stdout.strip().split('\n')[0]
        except Exception as e:
            return False, str(e)

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Build project with CMake."""
        build_dir = repo_path / "build"

        try:
            # Create build directory
            build_dir.mkdir(exist_ok=True)

            # Configure with CMake
            cmd = ["cmake", "-B", "build", "-S", "."]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            if returncode != 0:
                return False, f"cmake configure failed: {stderr}"

            # Build
            cmd = ["cmake", "--build", "build"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            if returncode != 0:
                return False, f"cmake build failed: {stderr}"

            return True, ""
        except TestTimeoutError:
            return False, "cmake build timed out"
        except Exception as e:
            return False, str(e)

    def get_install_command(self, repo_path: Path) -> List[str]:
        """Return install command."""
        return ["cmake", "-B", "build", "&&", "cmake", "--build", "build"]

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        return ["ctest", "--test-dir", "build", "--output-on-failure"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run CTest and return results."""
        build_dir = repo_path / "build"

        try:
            cmd = ["ctest", "--test-dir", str(build_dir), "--output-on-failure", "-V"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            output = stdout + "\n" + stderr

            # Try to find JUnit XML if CTest was configured to output it
            for xml_file in build_dir.glob("**/Testing/**/*.xml"):
                try:
                    result = parse_junit_xml(xml_file)
                    if result.total_tests > 0:
                        result.raw_output = output
                        return result
                except Exception:
                    continue

            # Parse CTest output
            result = self._parse_ctest_output(output, returncode)
            return result

        except TestTimeoutError as e:
            return TestResult(error=str(e))

    def _parse_ctest_output(self, output: str, returncode: int) -> TestResult:
        """Parse CTest output."""
        passed = []
        failed = []

        # Look for test results in CTest output
        # Pattern: 1/5 Test #1: test_name .......   Passed    0.01 sec
        # Pattern: 2/5 Test #2: test_name .......***Failed    0.01 sec
        pattern = r'Test\s+#\d+:\s+(\S+)\s+\.+\s*(Passed|Failed|\*+Failed)'

        for match in re.finditer(pattern, output, re.IGNORECASE):
            test_name = match.group(1)
            status = match.group(2).lower()

            if "passed" in status:
                passed.append(test_name)
            elif "failed" in status:
                failed.append(test_name)

        # Also look for summary
        # Pattern: 100% tests passed, 0 tests failed out of 5
        summary_match = re.search(r'(\d+)%\s+tests\s+passed,\s+(\d+)\s+tests\s+failed\s+out\s+of\s+(\d+)', output)
        if summary_match and not passed and not failed:
            total = int(summary_match.group(3))
            fail_count = int(summary_match.group(2))
            pass_count = total - fail_count
            passed = [f"test_{i}" for i in range(pass_count)]
            failed = [f"failed_test_{i}" for i in range(fail_count)]

        result = TestResult(
            passed=passed,
            failed=failed,
            raw_output=output
        )

        if result.total_tests == 0:
            if "no tests were found" in output.lower():
                result.error = "No tests found"
            elif returncode != 0:
                result.error = f"ctest failed with exit code {returncode}"

        return result


class MakeRunner(TestRunner):
    """Test runner for Make-based projects."""

    name = "make"
    language = "C++"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses Make for testing."""
        score = 0

        # Skip if this is clearly a non-C/C++ project
        non_c_markers = [
            "package.json", "pyproject.toml", "setup.py", "requirements.txt",
            "Gemfile", "Cargo.toml", "go.mod", "pom.xml", "build.gradle"
        ]
        for marker in non_c_markers:
            if (repo_path / marker).exists():
                return 0

        # Must have C/C++ source files
        has_c_files = any(repo_path.rglob("*.c")) or any(repo_path.rglob("*.cpp")) or any(repo_path.rglob("*.cc"))
        has_h_files = any(repo_path.rglob("*.h")) or any(repo_path.rglob("*.hpp"))
        if not (has_c_files or has_h_files):
            return 0

        # Check for Makefile
        makefile = repo_path / "Makefile"
        if makefile.exists():
            score += 40

            try:
                content = makefile.read_text()
                # Check for test target
                if re.search(r'^test\s*:', content, re.MULTILINE):
                    score += 40
                if re.search(r'^check\s*:', content, re.MULTILINE):
                    score += 30
            except Exception:
                pass

        # Reduce score if CMake is detected (prefer CMake)
        cmake_runner = CMakeRunner()
        if cmake_runner.detect(repo_path) > 50:
            score = max(0, score - 40)

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if Make is available."""
        if not self._check_command_exists("make"):
            return False, "Make not found"

        try:
            returncode, stdout, stderr = self._run_command(
                ["make", "--version"], Path.cwd(), timeout=10
            )
            return True, stdout.strip().split('\n')[0]
        except Exception as e:
            return False, str(e)

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Build project with Make."""
        try:
            # First check if there's a configure script
            if (repo_path / "configure").exists():
                cmd = ["./configure"]
                returncode, _, stderr = self._run_command(cmd, repo_path, timeout=timeout)
                if returncode != 0:
                    return False, f"configure failed: {stderr}"

            # Run make
            cmd = ["make"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            if returncode != 0:
                return False, f"make failed: {stderr}"

            return True, ""
        except TestTimeoutError:
            return False, "make timed out"
        except Exception as e:
            return False, str(e)

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        return ["make", "test"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run make test and return results."""
        try:
            # Try 'make test' first, then 'make check'
            for target in ["test", "check"]:
                cmd = ["make", target]
                returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
                output = stdout + "\n" + stderr

                # If successful or we found tests, parse the output
                if returncode == 0 or "test" in output.lower():
                    result = self._parse_make_output(output, returncode)
                    if result.total_tests > 0 or returncode == 0:
                        return result

            # No tests found
            return TestResult(
                error="No test target found in Makefile",
                raw_output=output
            )

        except TestTimeoutError as e:
            return TestResult(error=str(e))

    def _parse_make_output(self, output: str, returncode: int) -> TestResult:
        """Parse make test output."""
        passed = []
        failed = []

        # Look for common test output patterns
        # PASS: test_name or test_name ... ok
        for match in re.finditer(r'(?:PASS|ok|passed):\s*(\S+)', output, re.IGNORECASE):
            passed.append(match.group(1))

        for match in re.finditer(r'(?:FAIL|failed|error):\s*(\S+)', output, re.IGNORECASE):
            failed.append(match.group(1))

        result = TestResult(
            passed=passed,
            failed=failed,
            raw_output=output
        )

        if result.total_tests == 0 and returncode != 0:
            result.error = f"make test failed with exit code {returncode}"

        return result


class GoogleTestRunner(TestRunner):
    """Test runner for GoogleTest."""

    name = "gtest"
    language = "C++"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses GoogleTest."""
        score = 0

        # Check CMakeLists.txt for gtest
        cmake_file = repo_path / "CMakeLists.txt"
        if cmake_file.exists():
            try:
                content = cmake_file.read_text()
                if "gtest" in content.lower() or "googletest" in content.lower():
                    score += 60
                if "gtest_discover_tests" in content or "gtest_add_tests" in content:
                    score += 30
            except Exception:
                pass

        # Check for googletest directory
        if (repo_path / "googletest").exists() or (repo_path / "third_party" / "googletest").exists():
            score += 20

        # Check for test files with gtest includes
        for test_file in repo_path.rglob("*test*.cpp"):
            try:
                content = test_file.read_text()
                if "gtest/gtest.h" in content or "TEST(" in content or "TEST_F(" in content:
                    score += 20
                    break
            except Exception:
                pass

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if CMake is available (needed for GoogleTest)."""
        cmake_runner = CMakeRunner()
        return cmake_runner.check_runtime()

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Build project with CMake (same as CMake runner)."""
        cmake_runner = CMakeRunner()
        return cmake_runner.install_deps(repo_path, timeout)

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        return ["ctest", "--test-dir", "build", "--output-on-failure"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run GoogleTest via CTest."""
        # Use CMake runner for actual test execution
        cmake_runner = CMakeRunner()
        return cmake_runner.run_tests(repo_path, timeout)
