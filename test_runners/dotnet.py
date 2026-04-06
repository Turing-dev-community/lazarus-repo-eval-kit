"""
.NET test runner (dotnet test).
"""

import json
import re
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from .base import TestRunner, TestResult, TestTimeoutError
from .parsers import parse_dotnet_trx, parse_junit_xml


class DotNetRunner(TestRunner):
    """Test runner for .NET (C#, F#, VB.NET)."""

    name = "dotnet"
    language = "C#"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses .NET."""
        score = 0

        # Check for .sln file
        sln_files = list(repo_path.glob("*.sln"))
        if sln_files:
            score += 40

        # Check for .csproj files
        csproj_files = list(repo_path.rglob("*.csproj"))
        if csproj_files:
            score += 40

            # Check if any csproj has test references
            for csproj in csproj_files:
                try:
                    content = csproj.read_text()
                    if any(fw in content.lower() for fw in ["xunit", "nunit", "mstest", "test"]):
                        score += 20
                        break
                except Exception:
                    pass

        # Check for .fsproj files (F#)
        fsproj_files = list(repo_path.rglob("*.fsproj"))
        if fsproj_files:
            score += 30

        # Check for global.json
        if (repo_path / "global.json").exists():
            score += 10

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if .NET SDK is available."""
        if not self._check_command_exists("dotnet"):
            return False, ".NET SDK not found"

        try:
            returncode, stdout, stderr = self._run_command(
                ["dotnet", "--version"], Path.cwd(), timeout=10
            )
            return True, f".NET SDK {stdout.strip()}"
        except Exception as e:
            return False, str(e)

    def get_current_version(self) -> Optional[str]:
        """Return current .NET major version."""
        if not self._check_command_exists("dotnet"):
            return None
        try:
            returncode, stdout, _ = self._run_command(
                ["dotnet", "--version"], Path.cwd(), timeout=10
            )
            match = re.search(r'(\d+)', stdout)
            return match.group(1) if match else None
        except Exception:
            return None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required .NET version from global.json or csproj."""
        # Check global.json
        global_json = repo_path / "global.json"
        if global_json.exists():
            try:
                content = json.loads(global_json.read_text())
                sdk_version = content.get("sdk", {}).get("version", "")
                match = re.search(r'^(\d+)', sdk_version)
                if match:
                    return match.group(1)
            except Exception:
                pass

        # Check .csproj files for TargetFramework
        for csproj in repo_path.rglob("*.csproj"):
            try:
                content = csproj.read_text()
                match = re.search(r'<TargetFramework>net(\d+)', content)
                if match:
                    return match.group(1)
            except Exception:
                pass

        return None

    def _versions_compatible(self, required: str, current: str) -> bool:
        """Check .NET version compatibility."""
        try:
            return int(current) >= int(required)
        except (ValueError, TypeError):
            return True

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Restore .NET dependencies."""
        try:
            cmd = ["dotnet", "restore"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            if returncode != 0:
                return False, f"dotnet restore failed: {stderr}"

            # Also build to ensure compilation works
            cmd = ["dotnet", "build", "--no-restore"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            if returncode != 0:
                return False, f"dotnet build failed: {stderr}"

            return True, ""
        except TestTimeoutError:
            return False, "dotnet restore/build timed out"
        except Exception as e:
            return False, str(e)

    def get_install_command(self, repo_path: Path) -> List[str]:
        """Return install command."""
        return ["dotnet", "restore"]

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        return ["dotnet", "test"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run dotnet test and return results."""
        # Create temp directory for results
        with tempfile.TemporaryDirectory() as tmpdir:
            results_dir = Path(tmpdir)
            trx_path = results_dir / "results.trx"

            try:
                cmd = [
                    "dotnet", "test",
                    "--no-build",
                    f"--logger", f"trx;LogFileName={trx_path}",
                    "--verbosity", "normal"
                ]

                returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
                output = stdout + "\n" + stderr

                # Try to parse TRX file
                if trx_path.exists():
                    try:
                        result = parse_dotnet_trx(trx_path)
                        result.raw_output = output
                        return result
                    except Exception:
                        pass

                # Try to find any TRX files in TestResults directories
                for trx_file in repo_path.rglob("TestResults/*.trx"):
                    try:
                        result = parse_dotnet_trx(trx_file)
                        if result.total_tests > 0:
                            result.raw_output = output
                            return result
                    except Exception:
                        continue

                # Fall back to parsing output
                result = self._parse_dotnet_output(output, returncode)
                return result

            except TestTimeoutError as e:
                return TestResult(error=str(e))

    def _parse_dotnet_output(self, output: str, returncode: int) -> TestResult:
        """Parse dotnet test output as fallback."""
        import re

        passed = []
        failed = []
        skipped = []

        # Look for summary line
        # Pattern: Passed!  - Failed:     0, Passed:    10, Skipped:     0, Total:    10
        # or: Failed!  - Failed:     2, Passed:     8, Skipped:     0, Total:    10
        summary_match = re.search(
            r'Failed:\s*(\d+),\s*Passed:\s*(\d+),\s*Skipped:\s*(\d+)',
            output
        )

        if summary_match:
            fail_count = int(summary_match.group(1))
            pass_count = int(summary_match.group(2))
            skip_count = int(summary_match.group(3))

            passed = [f"test_{i}" for i in range(pass_count)]
            failed = [f"failed_test_{i}" for i in range(fail_count)]
            skipped = [f"skipped_test_{i}" for i in range(skip_count)]

        result = TestResult(
            passed=passed,
            failed=failed,
            skipped=skipped,
            raw_output=output
        )

        if result.total_tests == 0:
            if "no test" in output.lower():
                result.error = "No tests found"
            elif returncode != 0:
                result.error = f"dotnet test failed with exit code {returncode}"

        return result
