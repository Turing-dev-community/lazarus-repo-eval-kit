"""
JVM test runners: Maven, Gradle (Java/Scala/Kotlin).
"""

import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

from .base import TestRunner, TestResult, TestTimeoutError
from .parsers import parse_junit_xml


def get_required_java_version(repo_path: Path) -> Optional[str]:
    """Extract required Java version from repo config files."""
    # Check pom.xml for maven.compiler.source/target
    pom = repo_path / "pom.xml"
    if pom.exists():
        try:
            content = pom.read_text()
            match = re.search(r'<maven\.compiler\.source>(\d+)</maven\.compiler\.source>', content)
            if match:
                return match.group(1)
            match = re.search(r'<java\.version>(\d+)</java\.version>', content)
            if match:
                return match.group(1)
        except Exception:
            pass

    # Check build.gradle for sourceCompatibility
    for gradle_file in ["build.gradle", "build.gradle.kts"]:
        gradle = repo_path / gradle_file
        if gradle.exists():
            try:
                content = gradle.read_text()
                match = re.search(r'sourceCompatibility\s*=\s*["\']?(\d+)', content)
                if match:
                    return match.group(1)
                match = re.search(r'JavaVersion\.VERSION_(\d+)', content)
                if match:
                    return match.group(1)
            except Exception:
                pass

    return None


class MavenRunner(TestRunner):
    """Test runner for Maven (Java/Scala/Kotlin)."""

    name = "maven"
    language = "Java"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses Maven."""
        score = 0

        # Check for pom.xml
        if (repo_path / "pom.xml").exists():
            score += 70

        # Check for mvnw (Maven wrapper)
        if (repo_path / "mvnw").exists():
            score += 20

        # Check for src/main/java structure
        if (repo_path / "src" / "main" / "java").exists():
            score += 10

        # Check for src/test/java structure
        if (repo_path / "src" / "test" / "java").exists():
            score += 10

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if Maven/Java is available."""
        # Prefer Maven wrapper if available
        if not self._check_command_exists("mvn") and not self._check_command_exists("java"):
            return False, "Maven and Java not found"

        try:
            cmd = ["mvn", "--version"] if self._check_command_exists("mvn") else ["java", "--version"]
            returncode, stdout, stderr = self._run_command(cmd, Path.cwd(), timeout=30)
            return True, stdout.strip().split('\n')[0]
        except Exception as e:
            return False, str(e)

    def get_current_version(self) -> Optional[str]:
        """Return current Java major version."""
        if not self._check_command_exists("java"):
            return None
        try:
            returncode, stdout, stderr = self._run_command(
                ["java", "-version"], Path.cwd(), timeout=10
            )
            output = stdout + stderr
            match = re.search(r'version\s*["\']?(\d+)', output)
            return match.group(1) if match else None
        except Exception:
            return None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required Java version from repo config."""
        return get_required_java_version(repo_path)

    def _versions_compatible(self, required: str, current: str) -> bool:
        """Check Java version compatibility."""
        try:
            return int(current) >= int(required)
        except (ValueError, TypeError):
            return True

    def _get_mvn_cmd(self, repo_path: Path) -> str:
        """Get maven command (prefer wrapper if available)."""
        if (repo_path / "mvnw").exists():
            return "./mvnw"
        return "mvn"

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Install Maven dependencies."""
        mvn = self._get_mvn_cmd(repo_path)

        try:
            cmd = [mvn, "dependency:resolve", "-DskipTests", "-q"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            if returncode != 0:
                return False, f"mvn dependency:resolve failed: {stderr}"
            return True, ""
        except TestTimeoutError:
            return False, "mvn dependency:resolve timed out"
        except Exception as e:
            return False, str(e)

    def get_install_command(self, repo_path: Path) -> List[str]:
        """Return install command."""
        mvn = self._get_mvn_cmd(repo_path)
        return [mvn, "dependency:resolve"]

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        mvn = self._get_mvn_cmd(repo_path)
        return [mvn, "test"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run Maven tests and return results."""
        mvn = self._get_mvn_cmd(repo_path)

        try:
            cmd = [mvn, "test", "-Dsurefire.useFile=false"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            output = stdout + "\n" + stderr

            # Try to find and parse surefire XML reports
            surefire_dir = repo_path / "target" / "surefire-reports"
            if surefire_dir.exists():
                result = self._parse_surefire_reports(surefire_dir)
                if result.total_tests > 0:
                    result.raw_output = output
                    return result

            # Fall back to parsing output
            result = self._parse_maven_output(output, returncode)
            return result

        except TestTimeoutError as e:
            return TestResult(error=str(e))

    def _parse_surefire_reports(self, surefire_dir: Path) -> TestResult:
        """Parse all surefire XML reports in directory."""
        passed = []
        failed = []
        skipped = []
        total_time = 0.0

        for xml_file in surefire_dir.glob("TEST-*.xml"):
            try:
                result = parse_junit_xml(xml_file)
                passed.extend(result.passed)
                failed.extend(result.failed)
                skipped.extend(result.skipped)
                total_time += result.duration_seconds
            except Exception:
                continue

        return TestResult(
            passed=passed,
            failed=failed,
            skipped=skipped,
            duration_seconds=total_time
        )

    def _parse_maven_output(self, output: str, returncode: int) -> TestResult:
        """Parse Maven test output as fallback."""
        import re

        passed = []
        failed = []
        skipped = []

        # Look for test results summary
        # Pattern: Tests run: 10, Failures: 2, Errors: 1, Skipped: 1
        summary_match = re.search(
            r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)',
            output
        )

        if summary_match:
            total = int(summary_match.group(1))
            failures = int(summary_match.group(2))
            errors = int(summary_match.group(3))
            skipped_count = int(summary_match.group(4))

            # We don't have test names from summary, create placeholders
            passed_count = total - failures - errors - skipped_count
            passed = [f"test_{i}" for i in range(passed_count)]
            failed = [f"failed_test_{i}" for i in range(failures + errors)]
            skipped = [f"skipped_test_{i}" for i in range(skipped_count)]

        result = TestResult(
            passed=passed,
            failed=failed,
            skipped=skipped,
            raw_output=output
        )

        if result.total_tests == 0 and returncode != 0:
            result.error = f"mvn test failed with exit code {returncode}"

        return result


class GradleRunner(TestRunner):
    """Test runner for Gradle (Java/Scala/Kotlin)."""

    name = "gradle"
    language = "Java"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses Gradle."""
        score = 0

        # Check for build.gradle or build.gradle.kts
        if (repo_path / "build.gradle").exists():
            score += 60
        if (repo_path / "build.gradle.kts").exists():
            score += 60

        # Check for gradlew (Gradle wrapper)
        if (repo_path / "gradlew").exists():
            score += 30

        # Check for settings.gradle
        if (repo_path / "settings.gradle").exists() or (repo_path / "settings.gradle.kts").exists():
            score += 10

        # Check for src/main/java or src/main/kotlin structure
        if (repo_path / "src" / "main" / "java").exists():
            score += 10
        if (repo_path / "src" / "main" / "kotlin").exists():
            score += 10

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if Gradle/Java is available."""
        if not self._check_command_exists("gradle") and not self._check_command_exists("java"):
            return False, "Gradle and Java not found"

        try:
            cmd = ["gradle", "--version"] if self._check_command_exists("gradle") else ["java", "--version"]
            returncode, stdout, stderr = self._run_command(cmd, Path.cwd(), timeout=30)
            return True, stdout.strip().split('\n')[0]
        except Exception as e:
            return False, str(e)

    def get_current_version(self) -> Optional[str]:
        """Return current Java major version."""
        if not self._check_command_exists("java"):
            return None
        try:
            returncode, stdout, stderr = self._run_command(
                ["java", "-version"], Path.cwd(), timeout=10
            )
            output = stdout + stderr
            match = re.search(r'version\s*["\']?(\d+)', output)
            return match.group(1) if match else None
        except Exception:
            return None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required Java version from repo config."""
        return get_required_java_version(repo_path)

    def _versions_compatible(self, required: str, current: str) -> bool:
        """Check Java version compatibility."""
        try:
            return int(current) >= int(required)
        except (ValueError, TypeError):
            return True

    def _get_gradle_cmd(self, repo_path: Path) -> str:
        """Get gradle command (prefer wrapper if available)."""
        if (repo_path / "gradlew").exists():
            return "./gradlew"
        return "gradle"

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Install Gradle dependencies."""
        gradle = self._get_gradle_cmd(repo_path)

        try:
            cmd = [gradle, "dependencies", "--quiet"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            if returncode != 0:
                # Try build without tests as fallback
                cmd2 = [gradle, "build", "-x", "test", "--quiet"]
                returncode2, _, stderr2 = self._run_command(cmd2, repo_path, timeout=timeout)
                if returncode2 != 0:
                    return False, f"gradle dependencies failed: {stderr}"
            return True, ""
        except TestTimeoutError:
            return False, "gradle dependencies timed out"
        except Exception as e:
            return False, str(e)

    def get_install_command(self, repo_path: Path) -> List[str]:
        """Return install command."""
        gradle = self._get_gradle_cmd(repo_path)
        return [gradle, "dependencies"]

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        gradle = self._get_gradle_cmd(repo_path)
        return [gradle, "test"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run Gradle tests and return results."""
        gradle = self._get_gradle_cmd(repo_path)

        try:
            cmd = [gradle, "test"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            output = stdout + "\n" + stderr

            # Try to find and parse JUnit XML reports
            # Gradle usually puts them in build/test-results/test/
            test_results_dir = repo_path / "build" / "test-results" / "test"
            if test_results_dir.exists():
                result = self._parse_gradle_test_reports(test_results_dir)
                if result.total_tests > 0:
                    result.raw_output = output
                    return result

            # Fall back to parsing output
            result = self._parse_gradle_output(output, returncode)
            return result

        except TestTimeoutError as e:
            return TestResult(error=str(e))

    def _parse_gradle_test_reports(self, test_results_dir: Path) -> TestResult:
        """Parse all JUnit XML reports in Gradle test results directory."""
        passed = []
        failed = []
        skipped = []
        total_time = 0.0

        for xml_file in test_results_dir.glob("TEST-*.xml"):
            try:
                result = parse_junit_xml(xml_file)
                passed.extend(result.passed)
                failed.extend(result.failed)
                skipped.extend(result.skipped)
                total_time += result.duration_seconds
            except Exception:
                continue

        return TestResult(
            passed=passed,
            failed=failed,
            skipped=skipped,
            duration_seconds=total_time
        )

    def _parse_gradle_output(self, output: str, returncode: int) -> TestResult:
        """Parse Gradle test output as fallback."""
        import re

        # Look for test count in Gradle output
        # Pattern: X tests completed, Y failed
        match = re.search(r'(\d+)\s+tests?\s+completed,?\s+(\d+)\s+failed', output, re.IGNORECASE)

        passed = []
        failed = []

        if match:
            total = int(match.group(1))
            fail_count = int(match.group(2))
            pass_count = total - fail_count
            passed = [f"test_{i}" for i in range(pass_count)]
            failed = [f"failed_test_{i}" for i in range(fail_count)]

        result = TestResult(
            passed=passed,
            failed=failed,
            raw_output=output
        )

        if result.total_tests == 0 and returncode != 0:
            result.error = f"gradle test failed with exit code {returncode}"

        return result


class SbtRunner(TestRunner):
    """Test runner for sbt (Scala)."""

    name = "sbt"
    language = "Scala"

    def detect(self, repo_path: Path) -> int:
        """Detect if this repo uses sbt."""
        score = 0

        # Check for build.sbt
        if (repo_path / "build.sbt").exists():
            score += 70

        # Check for project directory
        if (repo_path / "project").exists():
            score += 20
            if (repo_path / "project" / "build.properties").exists():
                score += 10

        # Check for src/main/scala structure
        if (repo_path / "src" / "main" / "scala").exists():
            score += 10

        return min(score, 100)

    def check_runtime(self) -> Tuple[bool, str]:
        """Check if sbt is available."""
        if not self._check_command_exists("sbt"):
            return False, "sbt not found"

        try:
            returncode, stdout, stderr = self._run_command(
                ["sbt", "--version"], Path.cwd(), timeout=60
            )
            return True, stdout.strip().split('\n')[0]
        except Exception as e:
            return False, str(e)

    def get_current_version(self) -> Optional[str]:
        """Return current Scala/Java version."""
        if not self._check_command_exists("java"):
            return None
        try:
            returncode, stdout, stderr = self._run_command(
                ["java", "-version"], Path.cwd(), timeout=10
            )
            output = stdout + stderr
            match = re.search(r'version\s*["\']?(\d+)', output)
            return match.group(1) if match else None
        except Exception:
            return None

    def get_required_version(self, repo_path: Path) -> Optional[str]:
        """Extract required Scala/Java version from build.sbt."""
        build_sbt = repo_path / "build.sbt"
        if build_sbt.exists():
            try:
                content = build_sbt.read_text()
                match = re.search(r'scalaVersion\s*:=\s*["\'](\d+\.\d+)', content)
                if match:
                    return match.group(1)
            except Exception:
                pass
        return None

    def install_deps(self, repo_path: Path, timeout: int = 300) -> Tuple[bool, str]:
        """Install sbt dependencies."""
        try:
            cmd = ["sbt", "update"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            if returncode != 0:
                return False, f"sbt update failed: {stderr}"
            return True, ""
        except TestTimeoutError:
            return False, "sbt update timed out"
        except Exception as e:
            return False, str(e)

    def get_test_command(self, repo_path: Path) -> List[str]:
        """Return test command."""
        return ["sbt", "test"]

    def run_tests(self, repo_path: Path, timeout: int = 600) -> TestResult:
        """Run sbt tests and return results."""
        try:
            cmd = ["sbt", "test"]
            returncode, stdout, stderr = self._run_command(cmd, repo_path, timeout=timeout)
            output = stdout + "\n" + stderr

            # Try to find JUnit XML reports
            test_reports_dir = repo_path / "target" / "test-reports"
            if test_reports_dir.exists():
                result = self._parse_sbt_test_reports(test_reports_dir)
                if result.total_tests > 0:
                    result.raw_output = output
                    return result

            # Parse sbt output
            result = self._parse_sbt_output(output, returncode)
            return result

        except TestTimeoutError as e:
            return TestResult(error=str(e))

    def _parse_sbt_test_reports(self, reports_dir: Path) -> TestResult:
        """Parse sbt test reports."""
        passed = []
        failed = []
        skipped = []
        total_time = 0.0

        for xml_file in reports_dir.glob("*.xml"):
            try:
                result = parse_junit_xml(xml_file)
                passed.extend(result.passed)
                failed.extend(result.failed)
                skipped.extend(result.skipped)
                total_time += result.duration_seconds
            except Exception:
                continue

        return TestResult(
            passed=passed,
            failed=failed,
            skipped=skipped,
            duration_seconds=total_time
        )

    def _parse_sbt_output(self, output: str, returncode: int) -> TestResult:
        """Parse sbt test output."""
        import re

        passed = []
        failed = []

        # Look for ScalaTest output patterns
        # [info] - test name
        # [info] + test name (passed)
        for line in output.split('\n'):
            if '[info] +' in line or 'passed' in line.lower():
                # Extract test name
                match = re.search(r'\[info\]\s*[+-]\s*(.+)', line)
                if match:
                    passed.append(match.group(1).strip())
            elif '[error]' in line and 'failed' in line.lower():
                match = re.search(r'\[error\]\s*(.+)', line)
                if match:
                    failed.append(match.group(1).strip())

        result = TestResult(
            passed=passed,
            failed=failed,
            raw_output=output
        )

        if result.total_tests == 0 and returncode != 0:
            result.error = f"sbt test failed with exit code {returncode}"

        return result
