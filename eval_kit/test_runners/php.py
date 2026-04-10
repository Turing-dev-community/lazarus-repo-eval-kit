"""
PHP test runners: PHPUnit and Pest.
"""

import json
import re
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from .base import TestRunner, TestResult, TestTimeoutError
from .parsers import parse_junit_xml


def _read_composer_json(repo_path: Path) -> Optional[dict]:
    composer = repo_path / "composer.json"
    if not composer.exists():
        return None
    try:
        return json.loads(composer.read_text(encoding="utf-8"))
    except Exception:
        return None


def _find_php_project_root(repo_path: Path) -> Path:
    if (repo_path / "composer.json").exists():
        return repo_path
    for child in repo_path.iterdir():
        if child.is_dir() and (child / "composer.json").exists():
            return child
    return repo_path


def _required_php_version(repo_path: Path) -> Optional[str]:
    composer = _read_composer_json(repo_path)
    if not composer:
        return None
    require = composer.get("require", {})
    if not isinstance(require, dict):
        return None
    php_req = str(require.get("php", ""))
    match = re.search(r"(\d+\.\d+)", php_req)
    return match.group(1) if match else None


class PHPUnitRunner(TestRunner):
    name = "phpunit"
    language = "PHP"

    def detect(self, repo_path: Path) -> int:
        project_root = _find_php_project_root(repo_path)
        score = 0
        if (project_root / "phpunit.xml").exists() or (
            project_root / "phpunit.xml.dist"
        ).exists():
            score += 50
        if (project_root / "vendor" / "bin" / "phpunit").exists():
            score += 40
        if (project_root / "vendor" / "bin" / "simple-phpunit").exists():
            score += 35
        composer = _read_composer_json(project_root)
        if composer:
            deps = {
                **(
                    composer.get("require", {})
                    if isinstance(composer.get("require"), dict)
                    else {}
                ),
                **(
                    composer.get("require-dev", {})
                    if isinstance(composer.get("require-dev"), dict)
                    else {}
                ),
            }
            if "phpunit/phpunit" in deps:
                score += 40
            if "symfony/phpunit-bridge" in deps:
                score += 30
            scripts = composer.get("scripts", {})
            if isinstance(scripts, dict):
                test_script = str(scripts.get("test", "")).lower()
                if "phpunit" in test_script or "simple-phpunit" in test_script:
                    score += 20
        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        if not self._check_command_exists("php"):
            return False, "PHP not found"
        try:
            _, out, _ = self._run_command(["php", "--version"], Path.cwd(), timeout=10)
            return True, out.strip()
        except Exception as e:
            return False, str(e)

    def get_current_version(self) -> Optional[str]:
        ok, ver = self.check_runtime()
        if not ok:
            return None
        m = re.search(r"(\d+\.\d+)", ver)
        return m.group(1) if m else None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        return _required_php_version(_find_php_project_root(repo_path))

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        project_root = _find_php_project_root(repo_path)
        composer = project_root / "composer.json"
        vendor_phpunit = project_root / "vendor" / "bin" / "phpunit"
        if not composer.exists():
            return (
                (True, "")
                if vendor_phpunit.exists()
                else (False, "composer.json not found")
            )
        if not self._check_command_exists("composer"):
            return (
                (True, "") if vendor_phpunit.exists() else (False, "Composer not found")
            )
        try:
            cmd = ["composer", "install", "--no-interaction", "--no-progress"]
            rc, _, err = self._run_command(cmd, project_root, timeout=timeout)
            return (True, "") if rc == 0 else (False, f"composer install failed: {err}")
        except TestTimeoutError:
            return False, "composer install timed out"
        except Exception as e:
            return False, str(e)

    def get_test_command(self, repo_path: Path) -> List[str]:
        project_root = _find_php_project_root(repo_path)
        if (project_root / "vendor" / "bin" / "phpunit").exists():
            return ["php", "vendor/bin/phpunit", "--colors=never"]
        if (project_root / "vendor" / "bin" / "simple-phpunit").exists():
            return ["php", "vendor/bin/simple-phpunit", "--colors=never"]
        if (
            self._check_command_exists("composer")
            and (project_root / "composer.json").exists()
        ):
            return ["composer", "test", "--", "--colors=never"]
        return ["phpunit", "--colors=never"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        project_root = _find_php_project_root(repo_path)
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
            junit_path = Path(f.name)
        try:
            if (project_root / "vendor" / "bin" / "phpunit").exists():
                cmd = [
                    "php",
                    "vendor/bin/phpunit",
                    "--colors=never",
                    "--log-junit",
                    str(junit_path),
                ]
            elif (project_root / "vendor" / "bin" / "simple-phpunit").exists():
                cmd = [
                    "php",
                    "vendor/bin/simple-phpunit",
                    "--colors=never",
                    "--log-junit",
                    str(junit_path),
                ]
            elif (
                self._check_command_exists("composer")
                and (project_root / "composer.json").exists()
            ):
                cmd = [
                    "composer",
                    "test",
                    "--",
                    "--colors=never",
                    "--log-junit",
                    str(junit_path),
                ]
            else:
                cmd = ["phpunit", "--colors=never", "--log-junit", str(junit_path)]
            rc, out, err = self._run_command(cmd, project_root, timeout=timeout)
            output = (out or "") + "\n" + (err or "")
            if junit_path.exists() and junit_path.stat().st_size > 0:
                try:
                    result = parse_junit_xml(junit_path)
                    result.raw_output = output
                    result.exit_code = rc
                    if result.total_tests == 0 and rc != 0:
                        result.error = f"phpunit failed with exit code {rc}"
                    return result
                except Exception:
                    pass
            return self._parse_fallback_output(output, rc, "phpunit")
        except TestTimeoutError as e:
            return TestResult(error=str(e))
        except Exception as e:
            return TestResult(error=str(e))
        finally:
            try:
                if junit_path.exists():
                    junit_path.unlink()
            except Exception:
                pass

    def _parse_fallback_output(
        self, output: str, returncode: int, tool: str
    ) -> TestResult:
        passed: List[str] = []
        failed: List[str] = []
        skipped: List[str] = []
        tests = 0
        failures = 0
        errors = 0
        skips = 0

        m = re.search(
            r"Tests:\s*(\d+)(?:,\s*Assertions:\s*\d+)?(?:,\s*Failures:\s*(\d+))?(?:,\s*Errors:\s*(\d+))?(?:,\s*Skipped:\s*(\d+))?",
            output,
            re.IGNORECASE,
        )
        if m:
            tests = int(m.group(1) or 0)
            failures = int(m.group(2) or 0)
            errors = int(m.group(3) or 0)
            skips = int(m.group(4) or 0)
        else:
            m2 = re.search(r"OK\s*\((\d+)\s+tests?", output, re.IGNORECASE)
            if m2:
                tests = int(m2.group(1))

        fail_count = failures + errors
        pass_count = max(0, tests - fail_count - skips)
        passed = [f"test_{i}" for i in range(pass_count)]
        failed = [f"failed_test_{i}" for i in range(fail_count)]
        skipped = [f"skipped_test_{i}" for i in range(skips)]

        result = TestResult(
            passed=passed,
            failed=failed,
            skipped=skipped,
            raw_output=output,
            exit_code=returncode,
        )
        if result.total_tests == 0 and returncode != 0:
            result.error = f"{tool} failed with exit code {returncode}"
        return result


class PestRunner(PHPUnitRunner):
    name = "pest"
    language = "PHP"

    def detect(self, repo_path: Path) -> int:
        project_root = _find_php_project_root(repo_path)
        score = 0
        if (project_root / "pest.php").exists():
            score += 50
        if (project_root / "vendor" / "bin" / "pest").exists():
            score += 40
        composer = _read_composer_json(project_root)
        if composer:
            deps = {
                **(
                    composer.get("require", {})
                    if isinstance(composer.get("require"), dict)
                    else {}
                ),
                **(
                    composer.get("require-dev", {})
                    if isinstance(composer.get("require-dev"), dict)
                    else {}
                ),
            }
            if "pestphp/pest" in deps:
                score += 50
            scripts = composer.get("scripts", {})
            if (
                isinstance(scripts, dict)
                and "pest" in str(scripts.get("test", "")).lower()
            ):
                score += 20
        return min(score, 100)

    def get_test_command(self, repo_path: Path) -> List[str]:
        project_root = _find_php_project_root(repo_path)
        if (project_root / "vendor" / "bin" / "pest").exists():
            return ["php", "vendor/bin/pest", "--colors=never"]
        return ["pest", "--colors=never"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        project_root = _find_php_project_root(repo_path)
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
            junit_path = Path(f.name)
        try:
            if (project_root / "vendor" / "bin" / "pest").exists():
                cmd = [
                    "php",
                    "vendor/bin/pest",
                    "--colors=never",
                    "--log-junit",
                    str(junit_path),
                ]
            else:
                cmd = ["pest", "--colors=never", "--log-junit", str(junit_path)]
            rc, out, err = self._run_command(cmd, project_root, timeout=timeout)
            output = (out or "") + "\n" + (err or "")
            if junit_path.exists() and junit_path.stat().st_size > 0:
                try:
                    result = parse_junit_xml(junit_path)
                    result.raw_output = output
                    result.exit_code = rc
                    if result.total_tests == 0 and rc != 0:
                        result.error = f"pest failed with exit code {rc}"
                    return result
                except Exception:
                    pass
            return self._parse_fallback_output(output, rc, "pest")
        except TestTimeoutError as e:
            return TestResult(error=str(e))
        finally:
            try:
                if junit_path.exists():
                    junit_path.unlink()
            except Exception:
                pass
