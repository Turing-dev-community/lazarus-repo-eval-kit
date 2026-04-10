"""
.NET Framework test runner (MSBuild + vstest).
For legacy .NET Framework 4.x projects that don't use dotnet CLI.
"""

import re
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from .base import TestRunner, TestResult, TestTimeoutError
from .parsers import parse_dotnet_trx


class DotNetFrameworkRunner(TestRunner):
    """Test runner for .NET Framework 4.x (using MSBuild)."""

    name = "dotnet-framework"
    language = "C#"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses .NET Framework (not .NET Core)."""
        score = 0

        # Check for .sln file
        sln_files = list(repo_path.glob("*.sln"))
        if sln_files:
            score += 20

        # Check for .csproj files with TargetFrameworkVersion (not TargetFramework)
        csproj_files = list(repo_path.rglob("*.csproj"))
        is_framework = False

        for csproj in csproj_files:
            try:
                content = csproj.read_text()

                # .NET Framework uses TargetFrameworkVersion (e.g., v4.6.1)
                # .NET Core uses TargetFramework (e.g., net6.0)
                if "<TargetFrameworkVersion>" in content:
                    is_framework = True
                    score += 50

                    # Check for test frameworks
                    if any(
                        fw in content.lower()
                        for fw in [
                            "mstest",
                            "nunit",
                            "xunit",
                            "microsoft.visualstudio.testplatform",
                        ]
                    ):
                        score += 30
                    break

            except Exception:
                pass

        # Only return score if this is actually a .NET Framework project
        if not is_framework:
            return 0

        # Check for packages.config (old NuGet style)
        if list(repo_path.rglob("packages.config")):
            score += 10

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if MSBuild is available."""
        # Try msbuild directly (Windows with VS installed or Build Tools)
        if self._check_command_exists("msbuild"):
            try:
                returncode, stdout, stderr = self._run_command(
                    ["msbuild", "-version"], Path.cwd(), timeout=30
                )
                if returncode == 0:
                    version = stdout.strip().split("\n")[-1] if stdout else "unknown"
                    return True, f"MSBuild {version}"
            except Exception:
                pass

        # Try vswhere to find MSBuild (Windows)
        if self._check_command_exists("vswhere"):
            try:
                returncode, stdout, stderr = self._run_command(
                    [
                        "vswhere",
                        "-latest",
                        "-requires",
                        "Microsoft.Component.MSBuild",
                        "-find",
                        "MSBuild\\**\\Bin\\MSBuild.exe",
                    ],
                    Path.cwd(),
                    timeout=30,
                )
                if returncode == 0 and stdout.strip():
                    return True, "MSBuild found via vswhere"
            except Exception:
                pass

        return (
            False,
            "MSBuild not found. Install Visual Studio Build Tools or .NET Framework SDK.",
        )

    def _find_msbuild(self) -> Optional[str]:
        """Find MSBuild executable path."""
        # Direct msbuild command
        if self._check_command_exists("msbuild"):
            return "msbuild"

        # Try common Windows paths
        common_paths = [
            r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe",
            r"C:\Program Files\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe",
            r"C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe",
            r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe",
            r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Enterprise\MSBuild\Current\Bin\MSBuild.exe",
            r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Professional\MSBuild\Current\Bin\MSBuild.exe",
            r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Community\MSBuild\Current\Bin\MSBuild.exe",
            r"C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\MSBuild\Current\Bin\MSBuild.exe",
        ]

        for path in common_paths:
            if Path(path).exists():
                return path

        return None

    def _find_vstest(self) -> Optional[str]:
        """Find vstest.console.exe path."""
        if self._check_command_exists("vstest.console.exe"):
            return "vstest.console.exe"

        # Try common Windows paths
        common_paths = [
            r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\TestWindow\vstest.console.exe",
            r"C:\Program Files\Microsoft Visual Studio\2022\Professional\Common7\IDE\CommonExtensions\Microsoft\TestWindow\vstest.console.exe",
            r"C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\TestWindow\vstest.console.exe",
            r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\TestWindow\vstest.console.exe",
            r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Enterprise\Common7\IDE\CommonExtensions\Microsoft\TestWindow\vstest.console.exe",
            r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Professional\Common7\IDE\CommonExtensions\Microsoft\TestWindow\vstest.console.exe",
            r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Community\Common7\IDE\CommonExtensions\Microsoft\TestWindow\vstest.console.exe",
            r"C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\Common7\IDE\CommonExtensions\Microsoft\TestWindow\vstest.console.exe",
        ]

        for path in common_paths:
            if Path(path).exists():
                return path

        return None

    def get_current_version(self) -> Optional[str]:
        """Return current .NET Framework version (from MSBuild)."""
        msbuild = self._find_msbuild()
        if not msbuild:
            return None
        try:
            returncode, stdout, _ = self._run_command(
                [msbuild, "-version"], Path.cwd(), timeout=30
            )
            # Return the last line which contains version
            lines = stdout.strip().split("\n")
            return lines[-1] if lines else None
        except Exception:
            return None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required .NET Framework version from csproj."""
        for csproj in repo_path.rglob("*.csproj"):
            try:
                content = csproj.read_text()
                match = re.search(r"<TargetFrameworkVersion>v([\d.]+)", content)
                if match:
                    return match.group(1)
            except Exception:
                pass
        return None

    def _versions_compatible(self, required: str, current: str) -> bool:
        """Check .NET Framework version compatibility."""
        # For .NET Framework, we generally need the exact version or higher installed
        # The targeting pack handles most compatibility
        return True

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Restore NuGet packages and build with MSBuild."""
        msbuild = self._find_msbuild()
        if not msbuild:
            return False, "MSBuild not found"

        try:
            # First try nuget restore if packages.config exists
            if list(repo_path.rglob("packages.config")):
                if self._check_command_exists("nuget"):
                    returncode, stdout, stderr = self._run_command(
                        ["nuget", "restore"], repo_path, timeout=timeout
                    )
                    if returncode != 0:
                        # Try msbuild restore as fallback
                        pass

            # Build with MSBuild (this also restores PackageReference style packages)
            sln_files = list(repo_path.glob("*.sln"))
            if sln_files:
                target = str(sln_files[0])
            else:
                target = str(repo_path)

            cmd = [
                msbuild,
                target,
                "/t:Restore;Build",
                "/p:Configuration=Debug",
                "/m",
                "/verbosity:minimal",
            ]
            returncode, stdout, stderr = self._run_command(
                cmd, repo_path, timeout=timeout
            )

            if returncode != 0:
                return False, f"MSBuild failed: {stderr or stdout}"

            return True, ""

        except TestTimeoutError:
            return False, "MSBuild timed out"
        except Exception as e:
            return False, str(e)

    def get_install_command(self, repo_path: Path) -> List[str]:
        """Return install command."""
        msbuild = self._find_msbuild() or "msbuild"
        return [msbuild, "/t:Restore"]

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        vstest = self._find_vstest() or "vstest.console.exe"
        return [vstest, "**/*.Tests.dll"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run tests using vstest.console.exe."""
        vstest = self._find_vstest()
        if not vstest:
            return TestResult(error="vstest.console.exe not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            results_dir = Path(tmpdir)
            trx_path = results_dir / "results.trx"

            try:
                # Find test DLLs - .NET Framework outputs to bin/Debug/ directly
                test_dlls = []

                # Only match DLLs whose FILENAME contains "Test" (not just path)
                for dll in repo_path.rglob("*.dll"):
                    dll_str = str(dll)
                    dll_name = dll.name  # Just the filename, not path

                    # Must be in bin/Debug
                    if "bin" not in dll_str or "Debug" not in dll_str:
                        continue

                    # Skip known dependencies
                    skip_patterns = [
                        "Microsoft",
                        "VisualStudio",
                        "System.",
                        "EntityFramework",
                        "Newtonsoft",
                        "Moq",
                        "nunit",
                        "xunit",
                    ]
                    if any(p in dll_name for p in skip_patterns):
                        continue

                    # Filename must contain "Test" (case-insensitive)
                    if "test" in dll_name.lower() and dll_str not in test_dlls:
                        test_dlls.append(dll_str)

                if not test_dlls:
                    return TestResult(error="No test DLLs found in bin/Debug")

                import logging

                logger = logging.getLogger(__name__)
                logger.info(f"Found test DLLs: {test_dlls}")

                cmd = [
                    vstest,
                    *test_dlls,
                    f"/Logger:trx;LogFileName={trx_path}",
                    "/Platform:x64",
                    "/TestAdapterPath:.",  # Look for adapters in current dir
                ]

                returncode, stdout, stderr = self._run_command(
                    cmd, repo_path, timeout=timeout
                )
                output = stdout + "\n" + stderr
                logger.info(f"vstest returncode: {returncode}")
                logger.debug(f"vstest output: {output[:1000]}")

                # Parse TRX file if it exists
                if trx_path.exists():
                    try:
                        result = parse_dotnet_trx(trx_path)
                        result.raw_output = output
                        return result
                    except Exception:
                        pass

                # Fall back to parsing output
                return self._parse_vstest_output(output, returncode)

            except TestTimeoutError as e:
                return TestResult(error=str(e))

    def _parse_vstest_output(self, output: str, returncode: int) -> TestResult:
        """Parse vstest.console output."""
        passed = []
        failed = []
        skipped = []

        # Look for summary: Total tests: X. Passed: Y. Failed: Z. Skipped: W.
        match = re.search(
            r"Total tests:\s*(\d+).*?Passed:\s*(\d+).*?Failed:\s*(\d+).*?Skipped:\s*(\d+)",
            output,
            re.DOTALL,
        )

        if match:
            pass_count = int(match.group(2))
            fail_count = int(match.group(3))
            skip_count = int(match.group(4))

            passed = [f"test_{i}" for i in range(pass_count)]
            failed = [f"failed_test_{i}" for i in range(fail_count)]
            skipped = [f"skipped_test_{i}" for i in range(skip_count)]

        result = TestResult(
            passed=passed, failed=failed, skipped=skipped, raw_output=output
        )

        if result.total_tests == 0:
            if returncode != 0:
                result.error = f"vstest failed with exit code {returncode}"

        return result
