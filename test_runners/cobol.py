import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from .base import TestRunner, TestResult, TestTimeoutError

COBOL_CHECK_INSTALL_DIR = os.environ.get(
    "COBOL_CHECK_HOME", "/opt/cobol-check"
)


def _find_cobol_check_jar(repo_path: Path) -> Optional[Path]:
    """Locate the cobol-check JAR, first in the repo then in the system install."""
    for base in [repo_path, Path(COBOL_CHECK_INSTALL_DIR)]:
        bin_dir = base / "bin"
        if bin_dir.is_dir():
            jars = sorted(bin_dir.glob("cobol-check-*.jar"), reverse=True)
            if jars:
                return jars[0]
    return None


def _find_config_properties(repo_path: Path) -> Optional[Path]:
    """Find cobol-check config.properties in the repo."""
    candidate = repo_path / "config.properties"
    if candidate.exists():
        return candidate
    for name in ("testconfig.properties", "cobol-check.properties"):
        candidate = repo_path / name
        if candidate.exists():
            return candidate
    return None


def _has_cobol_sources(repo_path: Path) -> bool:
    exts = {".cob", ".cbl", ".cobol", ".CBL", ".COB", ".COBOL"}
    for root, _dirs, files in os.walk(repo_path):
        for f in files:
            if any(f.endswith(ext) for ext in exts):
                return True
        if root != str(repo_path) and len(os.path.relpath(root, repo_path).split(os.sep)) > 3:
            break
    return False


def _has_test_suites(repo_path: Path) -> bool:
    """Check for .cut (Cobol Unit Test) files, the standard cobol-check test suite extension."""
    for root, _dirs, files in os.walk(repo_path):
        for f in files:
            if f.endswith(".cut") or f.endswith(".CUT"):
                return True
    return False


def _read_config_value(config_path: Path, key: str) -> Optional[str]:
    """Read a value from a Java-style .properties file."""
    try:
        for line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            k, _, v = stripped.partition("=")
            if k.strip() == key:
                return v.strip()
    except Exception:
        pass
    return None


def _parse_cobol_check_output(output: str) -> TestResult:
    """Parse cobol-check text output into a TestResult.

    Expected format per test line:
        PASS:   1. Test description
    **** FAIL:   2. Test description (should fail)
    Summary line:
     60 TEST CASES WERE EXECUTED
     35 PASSED
     25 FAILED
    """
    passed: List[str] = []
    failed: List[str] = []
    current_suite = ""

    for line in output.splitlines():
        stripped = line.strip()

        if stripped.startswith("TESTSUITE:"):
            continue
        if not stripped.startswith("PASS:") and not stripped.startswith("**** FAIL:") and not stripped.startswith("FAIL:"):
            suite_match = re.match(r'^[A-Za-z].*[a-z].*$', stripped)
            if suite_match and not any(kw in stripped for kw in [
                "TEST CASES", "PASSED", "FAILED", "CALLS NOT MOCKED",
                "CobolCheck:", "EXPECTED", "WAS", "===",
            ]):
                current_suite = stripped
            continue

        pass_match = re.match(r'\s*PASS:\s*(\d+)\.\s*(.+)', stripped)
        fail_match = re.match(r'\*{4}\s*FAIL:\s*(\d+)\.\s*(.+)', stripped)
        if not fail_match:
            fail_match = re.match(r'\s*FAIL:\s*(\d+)\.\s*(.+)', stripped)

        if pass_match:
            num, desc = pass_match.group(1), pass_match.group(2).strip()
            name = f"{current_suite}::{desc}" if current_suite else desc
            passed.append(name)
        elif fail_match:
            num, desc = fail_match.group(1), fail_match.group(2).strip()
            name = f"{current_suite}::{desc}" if current_suite else desc
            failed.append(name)

    return TestResult(passed=passed, failed=failed, raw_output=output)


class CobolCheckRunner(TestRunner):
    name = "cobol-check"
    language = "COBOL"

    def detect(self, repo_path: Path) -> int:
        score = 0

        if _find_config_properties(repo_path) is not None:
            score += 40

        if _has_test_suites(repo_path):
            score += 40

        if _has_cobol_sources(repo_path):
            score += 10

        jar = _find_cobol_check_jar(repo_path)
        if jar is not None:
            score += 10

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        if not self._check_command_exists("cobc"):
            return False, "GnuCOBOL (cobc) not found"
        if not self._check_command_exists("java"):
            return False, "Java runtime not found (required for cobol-check)"
        try:
            _, cobc_ver, _ = self._run_command(["cobc", "--version"], Path.cwd(), timeout=10)
            _, java_ver, java_err = self._run_command(["java", "-version"], Path.cwd(), timeout=10)
            java_info = (java_ver or java_err or "").strip().splitlines()[0] if (java_ver or java_err) else "unknown"
            cobc_info = cobc_ver.strip().splitlines()[0] if cobc_ver else "unknown"
            return True, f"{cobc_info}; {java_info}"
        except Exception as e:
            return False, str(e)

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        jar = _find_cobol_check_jar(repo_path)
        if jar is None:
            return False, (
                "cobol-check JAR not found. Install cobol-check from "
                "https://github.com/openmainframeproject/cobol-check or "
                f"set COBOL_CHECK_HOME (currently: {COBOL_CHECK_INSTALL_DIR})"
            )

        config = _find_config_properties(repo_path)
        if config is None:
            system_config = Path(COBOL_CHECK_INSTALL_DIR) / "config.properties"
            if system_config.exists():
                try:
                    shutil.copy2(system_config, repo_path / "config.properties")
                except Exception as e:
                    return False, f"Failed to copy config.properties: {e}"

        scripts_dir = repo_path / "scripts"
        if not scripts_dir.exists():
            system_scripts = Path(COBOL_CHECK_INSTALL_DIR) / "scripts"
            if system_scripts.is_dir():
                try:
                    shutil.copytree(system_scripts, scripts_dir)
                except Exception as e:
                    return False, f"Failed to copy scripts: {e}"

        for script in scripts_dir.glob("*"):
            if script.is_file():
                try:
                    content = script.read_bytes()
                    if b"\r\n" in content:
                        script.write_bytes(content.replace(b"\r\n", b"\n"))
                    script.chmod(0o755)
                except Exception:
                    pass

        testruns_dir = repo_path / "testruns"
        testruns_dir.mkdir(exist_ok=True)

        return True, ""

    def get_test_command(self, repo_path: Path) -> List[str]:
        jar = _find_cobol_check_jar(repo_path)
        if jar is None:
            return ["cobol-check"]
        return ["java", "-jar", str(jar)]

    def _discover_programs(self, repo_path: Path) -> List[str]:
        """Discover COBOL programs that have test suites."""
        config = _find_config_properties(repo_path)

        test_dir_rel = "src/test/cobol"
        source_dir_rel = "src/main/cobol"
        source_suffixes = {"CBL", "cbl", "COB", "cob"}

        if config:
            val = _read_config_value(config, "test.suite.directory")
            if val:
                test_dir_rel = val
            val = _read_config_value(config, "application.source.directory")
            if val:
                source_dir_rel = val
            val = _read_config_value(config, "application.source.filename.suffix")
            if val:
                source_suffixes = {s.strip() for s in val.split(",")}

        test_dir = repo_path / test_dir_rel
        source_dir = repo_path / source_dir_rel
        programs = []

        if test_dir.is_dir():
            for child in test_dir.iterdir():
                if child.is_dir():
                    has_cut = any(f.suffix.lower() == ".cut" for f in child.iterdir() if f.is_file())
                    if has_cut:
                        programs.append(child.name)

        if not programs and source_dir.is_dir():
            for src_file in source_dir.iterdir():
                if src_file.is_file() and src_file.suffix.lstrip(".") in source_suffixes:
                    programs.append(src_file.stem)

        return programs

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        jar = _find_cobol_check_jar(repo_path)
        if jar is None:
            return TestResult(error="cobol-check JAR not found")

        programs = self._discover_programs(repo_path)
        if not programs:
            return TestResult(error="No COBOL programs with test suites found")

        program_arg = ":".join(programs)
        config = _find_config_properties(repo_path)

        cmd = ["java", "-jar", str(jar), "-p", program_arg]
        if config:
            cmd.extend(["-c", str(config)])

        try:
            rc, out, err = self._run_command(cmd, repo_path, timeout=timeout)
            output = (out or "") + "\n" + (err or "")
            result = _parse_cobol_check_output(output)
            result.exit_code = rc

            if result.total_tests == 0 and rc != 0:
                result.error = f"cobol-check failed with exit code {rc}"

            return result
        except TestTimeoutError as e:
            return TestResult(error=str(e))
        except Exception as e:
            return TestResult(error=str(e))
