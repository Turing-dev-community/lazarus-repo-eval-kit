"""
Python test runners: pytest, unittest.
"""

import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from .base import TestRunner, TestResult, TestTimeoutError
from .parsers import parse_junit_xml, parse_pytest_output


class PytestRunner(TestRunner):
    """Test runner for pytest."""

    name = "pytest"
    language = "Python"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses pytest."""
        score = 0

        # Check for pytest config files
        if (repo_path / "pytest.ini").exists():
            score += 50
        if (repo_path / "conftest.py").exists():
            score += 30

        # Check pyproject.toml for pytest config
        pyproject = repo_path / "pyproject.toml"
        if pyproject.exists():
            try:
                content = pyproject.read_text()
                if "[tool.pytest" in content:
                    score += 50  # Explicit pytest config section
                elif "pytest" in content:
                    score += 30
            except Exception:
                pass

        # Check setup.cfg for pytest config
        setup_cfg = repo_path / "setup.cfg"
        if setup_cfg.exists():
            try:
                content = setup_cfg.read_text()
                if "[tool:pytest]" in content:
                    score += 50  # Explicit pytest config section
            except Exception:
                pass

        # Check requirements for pytest
        for req_file in [
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-test.txt",
        ]:
            req_path = repo_path / req_file
            if req_path.exists():
                try:
                    content = req_path.read_text()
                    if "pytest" in content.lower():
                        score += 20
                except Exception:
                    pass

        # Check for test files with pytest patterns
        test_dirs = ["tests", "test", "t"]
        for test_dir in test_dirs:
            test_path = repo_path / test_dir
            if test_path.exists() and test_path.is_dir():
                has_python_tests = any(test_path.rglob("test_*.py")) or any(
                    test_path.rglob("*_test.py")
                )
                if has_python_tests:
                    score += 10
                if (test_path / "conftest.py").exists():
                    score += 20
                # Check for conftest in subdirs (like t/unit/)
                if any(test_path.rglob("conftest.py")):
                    score += 10

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if Python is available (uses the Python running this script)."""
        return True, f"Python {sys.version.split()[0]}"

    def get_current_version(self) -> Optional[str]:
        """Return current Python version as major.minor."""
        return f"{sys.version_info.major}.{sys.version_info.minor}"

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required Python version from repo config files."""
        import re

        # Check Dockerfile
        for dockerfile in ["Dockerfile", "Dockerfile.local", "docker/Dockerfile"]:
            df_path = repo_path / dockerfile
            if df_path.exists():
                try:
                    content = df_path.read_text()
                    match = re.search(
                        r"FROM\s+python:(\d+\.\d+)", content, re.IGNORECASE
                    )
                    if match:
                        return match.group(1)
                except Exception:
                    pass

        # Check pyproject.toml
        pyproject = repo_path / "pyproject.toml"
        if pyproject.exists():
            try:
                content = pyproject.read_text()
                match = re.search(r'requires-python\s*=\s*["\']>=?(\d+\.\d+)', content)
                if match:
                    return match.group(1)
                match = re.search(r'python_requires\s*=\s*["\']>=?(\d+\.\d+)', content)
                if match:
                    return match.group(1)
            except Exception:
                pass

        # Check setup.py
        setup_py = repo_path / "setup.py"
        if setup_py.exists():
            try:
                content = setup_py.read_text()
                match = re.search(r'python_requires\s*=\s*["\']>=?(\d+\.\d+)', content)
                if match:
                    return match.group(1)
            except Exception:
                pass

        # Check setup.cfg
        setup_cfg = repo_path / "setup.cfg"
        if setup_cfg.exists():
            try:
                content = setup_cfg.read_text()
                match = re.search(r"python_requires\s*=\s*>=?(\d+\.\d+)", content)
                if match:
                    return match.group(1)
            except Exception:
                pass

        return None

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Install Python dependencies."""
        python_cmd = sys.executable

        # Try different installation methods
        install_methods = []

        # Check for pyproject.toml (modern Python)
        if (repo_path / "pyproject.toml").exists():
            install_methods.append(
                [python_cmd, "-m", "pip", "install", "-e", ".[dev,test]"]
            )
            install_methods.append([python_cmd, "-m", "pip", "install", "-e", "."])

        # Check for setup.py
        if (repo_path / "setup.py").exists():
            install_methods.append(
                [python_cmd, "-m", "pip", "install", "-e", ".[dev,test]"]
            )
            install_methods.append([python_cmd, "-m", "pip", "install", "-e", "."])

        # Check for requirements files
        for req_file in [
            "requirements-dev.txt",
            "requirements-test.txt",
            "requirements.txt",
        ]:
            if (repo_path / req_file).exists():
                install_methods.append(
                    [python_cmd, "-m", "pip", "install", "-r", req_file]
                )

        # Always try to install pytest as fallback
        install_methods.append([python_cmd, "-m", "pip", "install", "pytest"])

        errors = []
        for cmd in install_methods:
            try:
                returncode, stdout, stderr = self._run_command(
                    cmd, repo_path, timeout=timeout
                )
                if returncode == 0:
                    # Continue with remaining installs (don't return early)
                    continue
                else:
                    errors.append(f"{' '.join(cmd)}: {stderr}")
            except TestTimeoutError:
                errors.append(f"{' '.join(cmd)}: Timeout")
            except Exception as e:
                errors.append(f"{' '.join(cmd)}: {str(e)}")

        # Check if pytest is now available
        try:
            returncode, _, _ = self._run_command(
                [python_cmd, "-m", "pytest", "--version"], repo_path, timeout=30
            )
            if returncode == 0:
                return True, ""
        except Exception:
            pass

        return False, "; ".join(errors)

    def get_install_command(self, repo_path: Path) -> List[str]:
        """Return typical install command."""
        python_cmd = sys.executable
        if (repo_path / "pyproject.toml").exists() or (repo_path / "setup.py").exists():
            return [python_cmd, "-m", "pip", "install", "-e", ".[dev,test]"]
        return [python_cmd, "-m", "pip", "install", "-r", "requirements.txt"]

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        python_cmd = sys.executable
        return [python_cmd, "-m", "pytest", "-v", "--tb=short"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run pytest and return results."""
        python_cmd = sys.executable

        # Create temp file for JUnit XML output
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
            xml_path = Path(f.name)

        try:
            cmd = [
                python_cmd,
                "-m",
                "pytest",
                "-v",
                "--tb=short",
                f"--junitxml={xml_path}",
                "--continue-on-collection-errors",
            ]

            returncode, stdout, stderr = self._run_command(
                cmd, repo_path, timeout=timeout
            )
            output = stdout + "\n" + stderr

            # Try to parse JUnit XML first
            if xml_path.exists() and xml_path.stat().st_size > 0:
                try:
                    result = parse_junit_xml(xml_path)
                    result.raw_output = output
                    return result
                except Exception:
                    pass

            # Fall back to parsing stdout
            result = parse_pytest_output(output)

            # If we couldn't parse any tests, check for errors
            if result.total_tests == 0:
                if (
                    "no tests ran" in output.lower()
                    or "collected 0 items" in output.lower()
                ):
                    result.error = "No tests found"
                elif returncode != 0:
                    result.error = f"pytest failed with exit code {returncode}"

            return result

        except TestTimeoutError as e:
            return TestResult(error=str(e))
        finally:
            # Cleanup
            try:
                if xml_path.exists():
                    xml_path.unlink()
            except Exception:
                pass


class UnittestRunner(TestRunner):
    """Test runner for Python unittest."""

    name = "unittest"
    language = "Python"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses unittest (without pytest)."""
        score = 0

        # Check for test files with unittest patterns
        test_dirs = ["tests", "test"]
        has_test_files = False

        for test_dir in test_dirs:
            test_path = repo_path / test_dir
            if test_path.exists() and test_path.is_dir():
                for test_file in test_path.rglob("test_*.py"):
                    has_test_files = True
                    try:
                        content = test_file.read_text()
                        if "import unittest" in content or "from unittest" in content:
                            score += 30
                        if "TestCase" in content:
                            score += 20
                    except Exception:
                        pass

        if has_test_files:
            score += 20

        # Django: manage.py plus app-level tests.py files
        if (repo_path / "manage.py").exists():
            score += 20
            django_tests = [
                p
                for p in repo_path.rglob("tests.py")
                if ".venv" not in p.parts and "venv" not in p.parts
            ]
            if django_tests:
                score += 30
            for req_file in [
                "requirements.txt",
                "requirements-dev.txt",
                "requirements-test.txt",
            ]:
                req_path = repo_path / req_file
                if req_path.exists():
                    try:
                        content = req_path.read_text().lower()
                        if "django" in content:
                            score += 20
                            break
                    except Exception:
                        pass

        if self._find_standalone_tests(repo_path):
            score += 20

        # Reduce score if pytest is detected (prefer pytest runner)
        pytest_runner = PytestRunner()
        if pytest_runner.detect(repo_path) > 50:
            score = max(0, score - 40)

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if Python is available (uses the Python running this script)."""
        return True, f"Python {sys.version.split()[0]}"

    def get_current_version(self) -> Optional[str]:
        """Return current Python version as major.minor."""
        return f"{sys.version_info.major}.{sys.version_info.minor}"

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required Python version - delegate to PytestRunner logic."""
        return PytestRunner().get_required_version(repo_path)

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Install Python dependencies."""
        python_cmd = sys.executable

        # Similar to pytest, but without needing pytest
        install_methods = []

        if (repo_path / "pyproject.toml").exists():
            install_methods.append([python_cmd, "-m", "pip", "install", "-e", "."])

        if (repo_path / "setup.py").exists():
            install_methods.append([python_cmd, "-m", "pip", "install", "-e", "."])

        for req_file in ["requirements.txt", "requirements-dev.txt"]:
            if (repo_path / req_file).exists():
                install_methods.append(
                    [python_cmd, "-m", "pip", "install", "-r", req_file]
                )

        if not install_methods:
            return True, ""  # No dependencies to install

        errors = []
        for cmd in install_methods:
            try:
                returncode, stdout, stderr = self._run_command(
                    cmd, repo_path, timeout=timeout
                )
                if returncode != 0:
                    errors.append(f"{' '.join(cmd)}: {stderr}")
            except Exception as e:
                errors.append(str(e))

        return len(errors) == 0, "; ".join(errors)

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        python_cmd = sys.executable
        if (repo_path / "manage.py").exists():
            return [python_cmd, "manage.py", "test"]
        standalone = self._find_standalone_tests(repo_path)
        if standalone:
            return [python_cmd, str(standalone[0])]
        return [python_cmd, "-m", "unittest", "discover", "-v"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run unittest and return results."""
        python_cmd = sys.executable

        if (repo_path / "manage.py").exists():
            cmd = [python_cmd, "manage.py", "test"]
            try:
                returncode, stdout, stderr = self._run_command(
                    cmd, repo_path, timeout=timeout
                )
                output = stdout + "\n" + stderr
                return self._parse_unittest_output(output, returncode)
            except TestTimeoutError as e:
                return TestResult(error=str(e))
        else:
            standalone = self._find_standalone_tests(repo_path)
            if standalone:
                print(f"running standalone tests: {standalone}")
                return self._run_standalone_tests(
                    python_cmd, repo_path, standalone, timeout
                )
            cmd = [python_cmd, "-m", "unittest", "discover", "-v"]

        try:
            returncode, stdout, stderr = self._run_command(
                cmd, repo_path, timeout=timeout
            )
            output = stdout + "\n" + stderr

            return self._parse_unittest_output(output, returncode)

        except TestTimeoutError as e:
            return TestResult(error=str(e))

    def _find_standalone_tests(self, repo_path: Path) -> List[Path]:
        tests = list(repo_path.glob("test_*.py")) + list(repo_path.glob("*_test.py"))
        return [p for p in tests if p.is_file()]

    def _run_standalone_tests(
        self,
        python_cmd: str,
        repo_path: Path,
        tests: List[Path],
        timeout: int,
    ) -> TestResult:
        passed = []
        failed = []
        outputs = []

        for test_file in tests:
            returncode, stdout, stderr = self._run_command(
                [python_cmd, str(test_file)],
                repo_path,
                timeout=timeout,
            )
            outputs.append(stdout + "\n" + stderr)
            if returncode == 0:
                passed.append(str(test_file))
            else:
                failed.append(str(test_file))

        result = TestResult(
            passed=passed,
            failed=failed,
            skipped=[],
            raw_output="\n".join(outputs),
        )
        if failed:
            result.error = f"standalone tests failed: {len(failed)}"
        return result

    def _parse_unittest_output(self, output: str, returncode: int) -> TestResult:
        """Parse unittest verbose output."""
        passed = []
        failed = []
        skipped = []

        # Pattern: test_name (module.ClassName) ... ok/FAIL/ERROR/skipped
        import re

        pattern = r"^(\w+)\s+\(([\w.]+)\)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped)"

        for line in output.split("\n"):
            match = re.match(pattern, line.strip())
            if match:
                test_name = match.group(1)
                class_name = match.group(2)
                status = match.group(3)
                full_name = f"{class_name}::{test_name}"

                if status == "ok":
                    passed.append(full_name)
                elif status in ("FAIL", "ERROR"):
                    failed.append(full_name)
                elif status == "skipped":
                    skipped.append(full_name)

        # Try to extract duration
        duration = 0.0
        duration_match = re.search(r"Ran \d+ tests? in ([\d.]+)s", output)
        if duration_match:
            try:
                duration = float(duration_match.group(1))
            except ValueError:
                pass

        result = TestResult(
            passed=passed,
            failed=failed,
            skipped=skipped,
            duration_seconds=duration,
            raw_output=output,
        )

        if result.total_tests == 0 and returncode != 0:
            result.error = f"unittest failed with exit code {returncode}"

        return result
