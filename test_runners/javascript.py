"""
JavaScript/TypeScript test runners: Jest, Vitest, Mocha.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from .base import (
    TestRunner, TestResult, RuntimeNotFoundError,
    DependencyInstallError, TestTimeoutError
)
from .parsers import parse_jest_json, parse_jest_verbose_output, parse_vitest_json, parse_mocha_json


def _load_test_env(project_root: Path) -> dict:
    """
    Load test env configuration.

    Priority:
    1) REPO_EVAL_TEST_ENV_JSON (if set)
    2) repo_evaluator_test_env.json in project root (if present)
    Always includes CI=true by default.
    """
    env: dict = {"CI": "true"}

    raw_env = os.getenv("REPO_EVAL_TEST_ENV_JSON", "").strip()
    if raw_env:
        try:
            parsed = json.loads(raw_env)
            if isinstance(parsed, dict):
                env.update({str(k): str(v) for k, v in parsed.items()})
                return env
        except Exception:
            return env

    cfg = project_root / "repo_evaluator_test_env.json"
    if cfg.exists():
        try:
            parsed = json.loads(cfg.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                env.update({str(k): str(v) for k, v in parsed.items()})
        except Exception:
            pass

    return env


def _load_write_empty_json_files(project_root: Path) -> List[str]:
    """
    Load a list of JSON files to create (with `{}`) if missing.

    Priority:
    1) REPO_EVAL_WRITE_EMPTY_JSON_FILES (comma-separated)
    2) repo_evaluator_write_empty_json_files.txt (newline-separated) in project root
    """
    raw = os.getenv("REPO_EVAL_WRITE_EMPTY_JSON_FILES", "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]

    cfg = project_root / "repo_evaluator_write_empty_json_files.txt"
    if cfg.exists():
        try:
            lines = [ln.strip() for ln in cfg.read_text(encoding="utf-8").splitlines()]
            return [ln for ln in lines if ln and not ln.startswith("#")]
        except Exception:
            return []

    return []


def detect_package_manager(repo_path: Path) -> str:
    """Detect which package manager the project uses."""
    if (repo_path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (repo_path / "yarn.lock").exists():
        return "yarn"
    if (repo_path / "bun.lockb").exists():
        return "bun"
    return "npm"  # default


def get_package_json(repo_path: Path) -> Optional[dict]:
    """Read and parse package.json."""
    pkg_path = repo_path / "package.json"
    if not pkg_path.exists():
        return None
    try:
        with open(pkg_path, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def get_required_node_version(repo_path: Path) -> Optional[str]:
    """Extract required Node.js version from repo config files."""
    import re

    # Check .nvmrc
    nvmrc = repo_path / ".nvmrc"
    if nvmrc.exists():
        try:
            content = nvmrc.read_text().strip()
            match = re.search(r'(\d+)', content)
            if match:
                return match.group(1)
        except Exception:
            pass

    # Check .node-version
    node_version = repo_path / ".node-version"
    if node_version.exists():
        try:
            content = node_version.read_text().strip()
            match = re.search(r'(\d+)', content)
            if match:
                return match.group(1)
        except Exception:
            pass

    # Check package.json engines.node
    pkg = get_package_json(repo_path)
    if pkg:
        engines = pkg.get("engines", {})
        node_req = engines.get("node", "")
        if node_req:
            match = re.search(r'(\d+)', node_req)
            if match:
                return match.group(1)

    return None


def find_js_project_root(repo_path: Path) -> Path:
    """Find the JS project root, checking for monorepo structures."""
    if (repo_path / "package.json").exists():
        pkg = get_package_json(repo_path)
        if pkg:
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            scripts = pkg.get("scripts", {})
            test_script = scripts.get("test", "")
            if any(d in deps for d in ["jest", "vitest", "mocha", "@testing-library/react"]) and test_script != "":
                return repo_path

    monorepo_dirs = ["web", "app", "apps", "packages", "frontend", "client", "src", "backend", "api"]
    for subdir in monorepo_dirs:
        sub_path = repo_path / subdir
        if sub_path.exists() and (sub_path / "package.json").exists():
            return sub_path

    for sub_path in repo_path.iterdir():
        if sub_path.is_dir() and (sub_path / "package.json").exists():
            pkg = get_package_json(sub_path)
            if pkg:
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if any(d in deps for d in ["jest", "vitest", "mocha"]):
                    return sub_path

    return repo_path


class JestRunner(TestRunner):
    """Test runner for Jest."""

    name = "jest"
    language = "JavaScript"
    _project_root: Optional[Path] = None
    _jest_config_file: Optional[str] = None
    _is_cra: bool = False

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses Jest."""
        score = 0
        project_root = find_js_project_root(repo_path)
        self._project_root = project_root
        self._jest_config_file = None
        self._is_cra = False

        jest_configs = [
            "jest.config.js", "jest.config.ts", "jest.config.mjs",
            "jest.config.cjs", "jest.config.json"
        ]
        for config in jest_configs:
            if (project_root / config).exists():
                score += 50
                self._jest_config_file = config
                break

        pkg = get_package_json(project_root)
        if pkg:
            all_deps = {
                **pkg.get("dependencies", {}),
                **pkg.get("devDependencies", {})
            }
            if "jest" in all_deps or "@testing-library/jest-dom" in all_deps:
                score += 30

            if "jest" in pkg:
                score += 40

            scripts = pkg.get("scripts", {})
            test_script = scripts.get("test", "")
            if "jest" in test_script:
                score += 20

            if "react-scripts" in all_deps and "react-scripts test" in test_script:
                self._is_cra = True

        return min(score, 100)

    def _has_config_conflict(self, project_root: Path) -> bool:
        """Check if both jest.config.js and package.json jest key exist."""
        pkg = get_package_json(project_root)
        has_pkg_jest = pkg and "jest" in pkg
        has_config_file = self._jest_config_file is not None
        return has_pkg_jest and has_config_file

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if Node.js is available."""
        if not self._check_command_exists("node"):
            return False, "Node.js not found"

        try:
            returncode, stdout, stderr = self._run_command(
                ["node", "--version"], Path.cwd(), timeout=10
            )
            return True, stdout.strip()
        except Exception as e:
            return False, str(e)

    def get_current_version(self) -> Optional[str]:
        """Return current Node.js major version."""
        import re
        available, version = self.check_runtime()
        if not available:
            return None
        match = re.search(r'v?(\d+)', version)
        return match.group(1) if match else None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required Node.js version from repo config."""
        project_root = self._get_project_root(repo_path) if hasattr(self, '_project_root') else repo_path
        return get_required_node_version(project_root)

    def _versions_compatible(self, required: str, current: str) -> bool:
        """Check Node.js version compatibility (major version match)."""
        try:
            return int(required) <= int(current)
        except (ValueError, TypeError):
            return True

    def _get_project_root(self, repo_path: Path) -> Path:
        """Get the JS project root (handles monorepos)."""
        if (repo_path / "package.json").exists():
            return repo_path
        if self._project_root and self._project_root.exists():
            return self._project_root
        return find_js_project_root(repo_path)

    def _get_pm_commands(self, repo_path: Path) -> Tuple[str, List[str], List[str]]:
        """Get package manager and its install/run commands."""
        project_root = self._get_project_root(repo_path)
        pm = detect_package_manager(project_root)

        if pm == "pnpm":
            return pm, ["pnpm", "install"], ["pnpm", "exec"]
        elif pm == "yarn":
            return pm, ["yarn", "install"], ["yarn"]
        elif pm == "bun":
            return pm, ["bun", "install"], ["bun"]
        else:
            # Use npm exec instead of npx to ensure project-local modules are resolved
            return pm, ["npm", "install", "--legacy-peer-deps", "--ignore-scripts"], ["npm", "exec", "--"]

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Install JavaScript dependencies."""
        project_root = self._get_project_root(repo_path)
        pm, install_cmd, _ = self._get_pm_commands(repo_path)

        try:
            returncode, stdout, stderr = self._run_command(
                install_cmd, project_root, timeout=timeout
            )
            if returncode != 0:
                return False, f"{pm} install failed: {stderr}"
            return True, ""
        except TestTimeoutError:
            return False, f"{pm} install timed out"
        except Exception as e:
            return False, str(e)

    def get_install_command(self, repo_path: Path) -> List[str]:
        """Return install command."""
        _, install_cmd, _ = self._get_pm_commands(repo_path)
        return install_cmd

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        _, _, run_cmd = self._get_pm_commands(repo_path)
        return run_cmd + ["jest", "--json"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run Jest and return results."""
        project_root = repo_path if (repo_path / "package.json").exists() else self._get_project_root(repo_path)
        self._project_root = project_root

        self._jest_config_file = None
        for config in ["jest.config.js", "jest.config.ts", "jest.config.mjs", "jest.config.cjs", "jest.config.json"]:
            if (project_root / config).exists():
                self._jest_config_file = config
                break

        pkg = get_package_json(project_root)
        test_script = ""
        if pkg:
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            test_script = pkg.get("scripts", {}).get("test", "")
            self._is_cra = "react-scripts" in all_deps and "react-scripts test" in test_script

        pm, _, run_cmd = self._get_pm_commands(repo_path)

        # Optional: run the repository's `scripts.test` more "as-is",
        # instead of forcing Jest JSON flags.
        #
        # Enable with: REPO_EVAL_JS_USE_TEST_SCRIPT=1
        use_test_script = os.getenv("REPO_EVAL_JS_USE_TEST_SCRIPT", "").strip().lower() in ("1", "true", "yes", "y")

        # Env defaults and optional config:
        # - Always inject CI=true (safe default)
        # - Optionally read REPO_EVAL_TEST_ENV_JSON OR repo_evaluator_test_env.json
        test_env: dict = _load_test_env(project_root)

        # Optional file creation for repos that expect these to exist:
        # - REPO_EVAL_WRITE_EMPTY_JSON_FILES='firebase.secrets.json,other.json'
        # - repo_evaluator_write_empty_json_files.txt (newline-separated)
        files_to_write = _load_write_empty_json_files(project_root)
        for rel in files_to_write:
            try:
                target = project_root / rel
                if not target.exists():
                    target.write_text("{}", encoding="utf-8")
            except Exception:
                pass

        if use_test_script and test_script:
            if pm == "yarn":
                cmd = ["yarn", "test"]
            elif pm == "pnpm":
                cmd = ["pnpm", "test"]
            else:
                cmd = ["npm", "test"]

            returncode, stdout, stderr = self._run_command(cmd, project_root, timeout=timeout, env=test_env)
            output = stdout + "\n" + stderr

            parsed = parse_jest_verbose_output(output)
            parsed.exit_code = returncode
            if parsed.total_tests > 0:
                return parsed
            # If verbose parsing yields nothing, fall through to JSON mode below.

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode='w') as f:
            json_path = Path(f.name)

        try:
            if self._is_cra:
                cmd = run_cmd + [
                    "react-scripts", "test",
                    "--json", f"--outputFile={json_path}",
                    "--watchAll=false", "--passWithNoTests"
                ]
            elif test_script and "jest" in test_script:
                # Use npm/yarn/pnpm test to run project's configured jest
                # This ensures project-local transformers (next/jest, ts-jest) are used
                if pm == "yarn":
                    cmd = ["yarn", "test", "--", "--json", f"--outputFile={json_path}", "--passWithNoTests", "--watchAll=false"]
                elif pm == "pnpm":
                    cmd = ["pnpm", "test", "--", "--json", f"--outputFile={json_path}", "--passWithNoTests", "--watchAll=false"]
                else:
                    cmd = ["npm", "test", "--", "--json", f"--outputFile={json_path}", "--passWithNoTests", "--watchAll=false"]
            else:
                cmd = run_cmd + ["jest", "--json", f"--outputFile={json_path}", "--passWithNoTests"]
                if self._has_config_conflict(project_root):
                    cmd.insert(-2, f"--config={self._jest_config_file}")

            returncode, stdout, stderr = self._run_command(cmd, project_root, timeout=timeout, env=test_env)
            output = stdout + "\n" + stderr

            if json_path.exists() and json_path.stat().st_size > 0:
                try:
                    result = parse_jest_json(json_path, project_root=project_root)
                    result.raw_output = output
                    result.exit_code = returncode
                    return result
                except Exception:
                    pass

            try:
                data = json.loads(stdout)
                parsed = self._parse_jest_stdout(data, output)
                parsed.exit_code = returncode
                return parsed
            except json.JSONDecodeError:
                pass

            fallback = parse_jest_verbose_output(output)
            fallback.exit_code = returncode
            if fallback.total_tests > 0:
                return fallback

            # Auto-fallback: if JSON mode couldn't produce parseable tests, try a Jest-typical
            # verbose run (helps repos whose test setup strips JSON output or prints only text).
            auto_verbose_fallback = os.getenv("REPO_EVAL_JS_AUTO_VERBOSE_FALLBACK", "").strip().lower() not in ("0", "false", "no", "n")
            if auto_verbose_fallback and (not use_test_script) and test_script:
                if pm == "yarn":
                    cmd2 = ["yarn", "test", "--", "--runInBand", "--verbose"]
                elif pm == "pnpm":
                    cmd2 = ["pnpm", "test", "--", "--runInBand", "--verbose"]
                else:
                    cmd2 = ["npm", "test", "--", "--runInBand", "--verbose"]
                rc2, out2, err2 = self._run_command(cmd2, project_root, timeout=timeout, env=test_env)
                output2 = out2 + "\n" + err2
                parsed2 = parse_jest_verbose_output(output2)
                parsed2.exit_code = rc2
                if parsed2.total_tests > 0:
                    return parsed2

            result = TestResult(raw_output=output, exit_code=returncode)
            if returncode != 0:
                result.error = f"Jest failed with exit code {returncode}"
            return result

        except TestTimeoutError as e:
            return TestResult(error=str(e))
        finally:
            try:
                if json_path.exists():
                    json_path.unlink()
            except Exception:
                pass

    def _parse_jest_stdout(self, data: dict, output: str) -> TestResult:
        """Parse Jest JSON from stdout."""
        passed = []
        failed = []
        skipped = []

        for test_file in data.get("testResults", []):
            assertion_results = test_file.get("assertionResults", []) or []
            for assertion in assertion_results:
                full_name = assertion.get("fullName")
                if not full_name:
                    ancestors = assertion.get("ancestorTitles", [])
                    test_title = assertion.get("title", "")
                    full_name = " ".join(ancestors + [test_title]) if ancestors else test_title
                status = assertion.get("status", "")

                if status == "passed":
                    passed.append(full_name)
                elif status == "failed":
                    failed.append(full_name)
                elif status in ("pending", "skipped", "todo"):
                    skipped.append(full_name)

            # Suite-level failures can have 0 assertions but status=failed.
            if (test_file.get("status") == "failed") and len(assertion_results) == 0:
                raw_name = test_file.get("name") or ""
                name = raw_name
                try:
                    if self._project_root and raw_name:
                        pr = str(self._project_root).rstrip("/") + "/"
                        if raw_name.startswith(pr):
                            name = raw_name[len(pr):]
                except Exception:
                    name = raw_name
                failed.append(f"{name}::(suite failed to run)" if name else "(suite failed to run)")

        return TestResult(
            passed=passed,
            failed=failed,
            skipped=skipped,
            raw_output=output
        )


class VitestRunner(TestRunner):
    """Test runner for Vitest."""

    name = "vitest"
    language = "JavaScript"
    _project_root: Optional[Path] = None

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses Vitest."""
        score = 0
        project_root = find_js_project_root(repo_path)
        self._project_root = project_root

        vitest_configs = [
            "vitest.config.ts", "vitest.config.js", "vitest.config.mts",
            "vitest.config.mjs", "vitest.config.cts", "vitest.config.cjs"
        ]
        for config in vitest_configs:
            if (project_root / config).exists():
                score += 60
                break

        pkg = get_package_json(project_root)
        if pkg:
            all_deps = {
                **pkg.get("dependencies", {}),
                **pkg.get("devDependencies", {})
            }
            # if "vitest" in all_deps or "@vitejs/plugin-react" in all_deps:
            #     score += 40

            if "vitest" in all_deps:
                score += 40

            scripts = pkg.get("scripts", {})
            test_script = scripts.get("test", "")
            if "vitest" in test_script:
                score += 30

        return min(score, 100)

    def _get_project_root(self, repo_path: Path) -> Path:
        if self._project_root and self._project_root.exists():
            return self._project_root
        return find_js_project_root(repo_path)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if Node.js is available."""
        if not self._check_command_exists("node"):
            return False, "Node.js not found"

        try:
            returncode, stdout, _ = self._run_command(
                ["node", "--version"], Path.cwd(), timeout=10
            )
            return True, stdout.strip()
        except Exception as e:
            return False, str(e)

    def get_current_version(self) -> Optional[str]:
        """Return current Node.js major version."""
        import re
        available, version = self.check_runtime()
        if not available:
            return None
        match = re.search(r'v?(\d+)', version)
        return match.group(1) if match else None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required Node.js version from repo config."""
        project_root = self._get_project_root(repo_path)
        return get_required_node_version(project_root)

    def _versions_compatible(self, required: str, current: str) -> bool:
        """Check Node.js version compatibility (major version match)."""
        try:
            return int(required) <= int(current)
        except (ValueError, TypeError):
            return True

    def _get_pm_commands(self, repo_path: Path) -> Tuple[str, List[str], List[str]]:
        """Get package manager and its install/run commands."""
        project_root = self._get_project_root(repo_path)
        pm = detect_package_manager(project_root)

        if pm == "pnpm":
            return pm, ["pnpm", "install"], ["pnpm", "exec"]
        elif pm == "yarn":
            return pm, ["yarn", "install"], ["yarn"]
        elif pm == "bun":
            return pm, ["bun", "install"], ["bun"]
        else:
            return pm, ["npm", "install", "--legacy-peer-deps"], ["npm", "exec", "--"]

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Install JavaScript dependencies."""
        project_root = self._get_project_root(repo_path)
        pm, install_cmd, _ = self._get_pm_commands(repo_path)

        try:
            returncode, stdout, stderr = self._run_command(
                install_cmd, project_root, timeout=timeout
            )
            if returncode != 0:
                return False, f"{pm} install failed: {stderr}"
            return True, ""
        except TestTimeoutError:
            return False, f"{pm} install timed out"
        except Exception as e:
            return False, str(e)

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        _, _, run_cmd = self._get_pm_commands(repo_path)
        return run_cmd + ["vitest", "run", "--reporter=json"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run Vitest and return results."""
        project_root = self._get_project_root(repo_path)
        _, _, run_cmd = self._get_pm_commands(repo_path)

        try:
            cmd = run_cmd + ["vitest", "run", "--reporter=json"]
            returncode, stdout, stderr = self._run_command(cmd, project_root, timeout=timeout)
            output = stdout + "\n" + stderr

            # Try to parse JSON from stdout
            try:
                data = json.loads(stdout)
                result = self._parse_vitest_output(data, output)
                result.exit_code = returncode
                return result
            except json.JSONDecodeError:
                pass

            result = TestResult(raw_output=output, exit_code=returncode)
            if returncode != 0:
                result.error = f"Vitest failed with exit code {returncode}"
            return result

        except TestTimeoutError as e:
            return TestResult(error=str(e))

    def _parse_vitest_output(self, data: dict, output: str) -> TestResult:
        """Parse Vitest JSON output."""
        passed = []
        failed = []
        skipped = []

        for test_file in data.get("testResults", []):
            for assertion in test_file.get("assertionResults", []):
                full_name = assertion.get("fullName")
                if not full_name:
                    ancestors = assertion.get("ancestorTitles", [])
                    test_title = assertion.get("name") or assertion.get("title", "")
                    full_name = " ".join(ancestors + [test_title]) if ancestors else test_title
                status = assertion.get("status", "")

                if status == "passed":
                    passed.append(full_name)
                elif status == "failed":
                    failed.append(full_name)
                elif status in ("pending", "skipped", "todo"):
                    skipped.append(full_name)

        return TestResult(
            passed=passed,
            failed=failed,
            skipped=skipped,
            raw_output=output
        )


class MochaRunner(TestRunner):
    """Test runner for Mocha."""

    name = "mocha"
    language = "JavaScript"
    _project_root: Optional[Path] = None

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses Mocha."""
        score = 0
        project_root = find_js_project_root(repo_path)
        self._project_root = project_root

        mocha_configs = [".mocharc.js", ".mocharc.json", ".mocharc.yml", ".mocharc.yaml", "mocha.opts"]
        for config in mocha_configs:
            if (project_root / config).exists():
                score += 50
                break

        pkg = get_package_json(project_root)
        if pkg:
            all_deps = {
                **pkg.get("dependencies", {}),
                **pkg.get("devDependencies", {})
            }
            if "mocha" in all_deps:
                score += 40

            if "mocha" in pkg:
                score += 20

            scripts = pkg.get("scripts", {})
            test_script = scripts.get("test", "")
            if "mocha" in test_script:
                score += 20

        return min(score, 100)

    def _get_project_root(self, repo_path: Path) -> Path:
        if self._project_root and self._project_root.exists():
            return self._project_root
        return find_js_project_root(repo_path)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if Node.js is available."""
        if not self._check_command_exists("node"):
            return False, "Node.js not found"

        try:
            returncode, stdout, _ = self._run_command(
                ["node", "--version"], Path.cwd(), timeout=10
            )
            return True, stdout.strip()
        except Exception as e:
            return False, str(e)

    def get_current_version(self) -> Optional[str]:
        """Return current Node.js major version."""
        import re
        available, version = self.check_runtime()
        if not available:
            return None
        match = re.search(r'v?(\d+)', version)
        return match.group(1) if match else None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required Node.js version from repo config."""
        project_root = self._get_project_root(repo_path)
        return get_required_node_version(project_root)

    def _versions_compatible(self, required: str, current: str) -> bool:
        """Check Node.js version compatibility (major version match)."""
        try:
            return int(required) <= int(current)
        except (ValueError, TypeError):
            return True

    def _get_pm_commands(self, repo_path: Path) -> Tuple[str, List[str], List[str]]:
        """Get package manager and its install/run commands."""
        project_root = self._get_project_root(repo_path)
        pm = detect_package_manager(project_root)

        if pm == "pnpm":
            return pm, ["pnpm", "install"], ["pnpm", "exec"]
        elif pm == "yarn":
            return pm, ["yarn", "install"], ["yarn"]
        elif pm == "bun":
            return pm, ["bun", "install"], ["bun"]
        else:
            return pm, ["npm", "install", "--legacy-peer-deps", "--ignore-scripts"], ["npm", "exec", "--"]

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Install JavaScript dependencies."""
        project_root = self._get_project_root(repo_path)
        pm, install_cmd, _ = self._get_pm_commands(repo_path)

        try:
            returncode, stdout, stderr = self._run_command(
                install_cmd, project_root, timeout=timeout
            )
            if returncode != 0:
                return False, f"{pm} install failed: {stderr}"
            return True, ""
        except TestTimeoutError:
            return False, f"{pm} install timed out"
        except Exception as e:
            return False, str(e)

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        _, _, run_cmd = self._get_pm_commands(repo_path)
        return run_cmd + ["mocha", "--reporter", "json"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run Mocha and return results."""
        project_root = self._get_project_root(repo_path)
        _, _, run_cmd = self._get_pm_commands(repo_path)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode='w') as f:
            json_path = Path(f.name)

        try:
            cmd = run_cmd + [
                "mocha",
                "--reporter", "json",
                f"--reporter-option", f"output={json_path}"
            ]

            returncode, stdout, stderr = self._run_command(cmd, project_root, timeout=timeout)
            output = stdout + "\n" + stderr

            # Try to parse JSON output file
            if json_path.exists() and json_path.stat().st_size > 0:
                try:
                    result = parse_mocha_json(json_path)
                    result.raw_output = output
                    result.exit_code = returncode
                    return result
                except Exception:
                    pass

            # Fall back to parsing JSON from stdout
            try:
                data = json.loads(stdout)
                result = self._parse_mocha_stdout(data, output)
                result.exit_code = returncode
                return result
            except json.JSONDecodeError:
                pass

            result = TestResult(raw_output=output, exit_code=returncode)
            if returncode != 0:
                result.error = f"Mocha failed with exit code {returncode}"
            return result

        except TestTimeoutError as e:
            return TestResult(error=str(e))
        finally:
            try:
                if json_path.exists():
                    json_path.unlink()
            except Exception:
                pass

    def _parse_mocha_stdout(self, data: dict, output: str) -> TestResult:
        """Parse Mocha JSON from stdout."""
        passed = [t.get("fullTitle", t.get("title", "")) for t in data.get("passes", [])]
        failed = [t.get("fullTitle", t.get("title", "")) for t in data.get("failures", [])]
        skipped = [t.get("fullTitle", t.get("title", "")) for t in data.get("pending", [])]

        duration = data.get("stats", {}).get("duration", 0) / 1000.0

        return TestResult(
            passed=passed,
            failed=failed,
            skipped=skipped,
            duration_seconds=duration,
            raw_output=output
        )


class NodeTestRunner(TestRunner):
    """Test runner for Node.js built-in test runner (node --test)."""

    name = "node:test"
    language = "JavaScript"
    _project_root: Optional[Path] = None

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses Node's built-in test runner."""
        score = 0
        project_root = find_js_project_root(repo_path)
        self._project_root = project_root

        pkg = get_package_json(project_root)
        if not pkg:
            return 0

        scripts = pkg.get("scripts", {})
        test_script = scripts.get("test", "")

        if "node --test" in test_script or "node --import" in test_script and "--test" in test_script:
            score += 60

        all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        if "tsx" in all_deps and "--test" in test_script:
            score += 20
        if "@types/node" in all_deps:
            score += 10

        test_dir = project_root / "test"
        if test_dir.exists():
            has_ts_tests = any(test_dir.glob("*.ts")) or any(test_dir.glob("**/*.ts"))
            has_js_tests = any(test_dir.glob("*.js")) or any(test_dir.glob("**/*.js"))
            if has_ts_tests or has_js_tests:
                score += 10

        return min(score, 100)

    def _get_project_root(self, repo_path: Path) -> Path:
        if self._project_root and self._project_root.exists():
            return self._project_root
        return find_js_project_root(repo_path)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if Node.js is available (18+ required for --test)."""
        if not self._check_command_exists("node"):
            return False, "Node.js not found"

        try:
            returncode, stdout, _ = self._run_command(
                ["node", "--version"], Path.cwd(), timeout=10
            )
            version = stdout.strip()
            import re
            match = re.search(r'v?(\d+)', version)
            if match:
                major = int(match.group(1))
                if major < 18:
                    return False, f"Node.js 18+ required for --test (found {version})"
            return True, version
        except Exception as e:
            return False, str(e)

    def get_current_version(self) -> Optional[str]:
        """Return current Node.js major version."""
        import re
        available, version = self.check_runtime()
        if not available:
            return None
        match = re.search(r'v?(\d+)', version)
        return match.group(1) if match else None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required Node.js version from repo config."""
        project_root = self._get_project_root(repo_path)
        return get_required_node_version(project_root)

    def _versions_compatible(self, required: str, current: str) -> bool:
        """Check Node.js version compatibility."""
        try:
            return int(required) <= int(current)
        except (ValueError, TypeError):
            return True

    def _get_pm_commands(self, repo_path: Path) -> Tuple[str, List[str], List[str]]:
        """Get package manager and its install/run commands."""
        project_root = self._get_project_root(repo_path)
        pm = detect_package_manager(project_root)

        if pm == "pnpm":
            return pm, ["pnpm", "install"], ["pnpm"]
        elif pm == "yarn":
            return pm, ["yarn", "install"], ["yarn"]
        elif pm == "bun":
            return pm, ["bun", "install"], ["bun"]
        else:
            return pm, ["npm", "install", "--legacy-peer-deps", "--ignore-scripts"], ["npm"]

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Install JavaScript dependencies."""
        project_root = self._get_project_root(repo_path)
        pm, install_cmd, _ = self._get_pm_commands(repo_path)

        try:
            returncode, stdout, stderr = self._run_command(
                install_cmd, project_root, timeout=timeout
            )
            if returncode != 0:
                return False, f"{pm} install failed: {stderr}"
            return True, ""
        except TestTimeoutError:
            return False, f"{pm} install timed out"
        except Exception as e:
            return False, str(e)

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        _, _, run_cmd = self._get_pm_commands(repo_path)
        return run_cmd + ["test"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run node --test via npm test and return results."""
        project_root = self._get_project_root(repo_path)
        pm, _, run_cmd = self._get_pm_commands(repo_path)

        try:
            cmd = run_cmd + ["test"]
            returncode, stdout, stderr = self._run_command(cmd, project_root, timeout=timeout)
            output = stdout + "\n" + stderr

            passed, failed, skipped = self._parse_tap_output(output)

            result = TestResult(
                passed=passed,
                failed=failed,
                skipped=skipped,
                raw_output=output,
                exit_code=returncode
            )
            if returncode != 0 and not failed:
                result.error = f"node --test failed with exit code {returncode}"
            return result

        except TestTimeoutError as e:
            return TestResult(error=str(e))

    def _parse_tap_output(self, output: str) -> Tuple[List[str], List[str], List[str]]:
        """Parse TAP-like output from node --test."""
        import re
        passed = []
        failed = []
        skipped = []

        for line in output.split('\n'):
            pass_match = re.match(r'✔\s+(.+?)(?:\s+\(\d+\.?\d*m?s\))?$', line.strip())
            if pass_match:
                passed.append(pass_match.group(1).strip())
                continue

            fail_match = re.match(r'✖\s+(.+?)(?:\s+\(\d+\.?\d*m?s\))?$', line.strip())
            if fail_match:
                failed.append(fail_match.group(1).strip())
                continue

            skip_match = re.match(r'⊘\s+(.+?)(?:\s+\(\d+\.?\d*m?s\))?$', line.strip())
            if skip_match:
                skipped.append(skip_match.group(1).strip())
                continue

            ok_match = re.match(r'ok \d+ - (.+)', line.strip())
            if ok_match:
                passed.append(ok_match.group(1).strip())
                continue

            not_ok_match = re.match(r'not ok \d+ - (.+)', line.strip())
            if not_ok_match:
                failed.append(not_ok_match.group(1).strip())
                continue

        return passed, failed, skipped
