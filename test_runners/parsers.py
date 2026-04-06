"""
Shared output parsers for common test output formats.
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Tuple
from .base import TestResult, OutputParseError


def parse_junit_xml(xml_path: Path) -> TestResult:
    """
    Parse JUnit XML format (used by pytest, Java, C#, etc.).

    Format:
    <testsuite name="TestSuite" tests="3" failures="1" errors="0">
      <testcase name="test_login" classname="AuthTests" time="0.5"/>
      <testcase name="test_payment" classname="PaymentTests" time="1.2">
        <failure message="AssertionError">Expected 200, got 500</failure>
      </testcase>
      <testcase name="test_skipped" classname="Tests">
        <skipped message="Not implemented"/>
      </testcase>
    </testsuite>
    """
    if not xml_path.exists():
        raise OutputParseError(f"JUnit XML file not found: {xml_path}")

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError as e:
        raise OutputParseError(f"Failed to parse JUnit XML: {e}")

    passed = []
    failed = []
    skipped = []
    total_time = 0.0

    # Collect all <testcase> elements regardless of nesting depth.
    # PHPUnit 10+ nests <testsuite> elements 3+ levels deep.
    if root.tag in ("testsuites", "testsuite"):
        all_testcases = root.findall(".//testcase")
    else:
        all_testcases = root.findall(".//testcase")

    for testcase in all_testcases:
            classname = testcase.get("classname", "")
            name = testcase.get("name", "")
            time_str = testcase.get("time", "0")

            # Build full test name
            if classname:
                full_name = f"{classname}::{name}"
            else:
                full_name = name

            try:
                total_time += float(time_str)
            except ValueError:
                pass

            # Check for failure/error/skipped
            failure = testcase.find("failure")
            error = testcase.find("error")
            skip = testcase.find("skipped")

            if failure is not None or error is not None:
                failed.append(full_name)
            elif skip is not None:
                skipped.append(full_name)
            else:
                passed.append(full_name)

    return TestResult(
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_seconds=total_time
    )


def parse_jest_json(json_path: Path, project_root: Optional[Path] = None) -> TestResult:
    """
    Parse Jest JSON output format.

    Format:
    {
      "numPassedTests": 10,
      "numFailedTests": 2,
      "testResults": [
        {
          "name": "/path/to/test.js",
          "assertionResults": [
            {"title": "should login", "status": "passed"},
            {"title": "should logout", "status": "failed"}
          ]
        }
      ]
    }
    """
    if not json_path.exists():
        raise OutputParseError(f"Jest JSON file not found: {json_path}")

    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise OutputParseError(f"Failed to parse Jest JSON: {e}")

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

        # Suite-level failures: Jest may mark the file status as failed with 0 assertionResults.
        # If we ignore these, totals can look like "3 tests ran" when in reality most suites crashed.
        if (test_file.get("status") == "failed") and len(assertion_results) == 0:
            raw_name = test_file.get("name") or ""
            name = raw_name
            if project_root and raw_name:
                try:
                    pr = str(project_root).rstrip("/") + "/"
                    if raw_name.startswith(pr):
                        name = raw_name[len(pr):]
                except Exception:
                    name = raw_name
            failed.append(f"{name}::(suite failed to run)" if name else "(suite failed to run)")

    # Get duration if available
    duration = 0.0
    if "startTime" in data and "endTime" in data:
        duration = (data["endTime"] - data["startTime"]) / 1000.0

    return TestResult(
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_seconds=duration
    )


def parse_jest_verbose_output(output: str) -> TestResult:
    """
    Parse Jest `--verbose` output (✓/✕/○ lines).

    Captures only single-line test titles. Duplicate titles may collide, so this should be
    used as a fallback when JSON output is unavailable.
    """
    passed: List[str] = []
    failed: List[str] = []
    skipped: List[str] = []

    pattern = re.compile(r"^\\s*(✓|✕|○)\\s(.+?)(?:\\s+\\(\\d+\\s*m?s\\))?\\s*$", re.MULTILINE)
    for sym, name in pattern.findall(output or ""):
        if sym == "✓":
            passed.append(name)
        elif sym == "✕":
            failed.append(name)
        else:
            skipped.append(name)

    return TestResult(passed=passed, failed=failed, skipped=skipped, raw_output=output)


def parse_go_test_json(output: str) -> TestResult:
    """
    Parse Go test JSON output (line-delimited JSON).

    Format (one JSON object per line):
    {"Action":"run","Test":"TestLogin"}
    {"Action":"pass","Test":"TestLogin","Elapsed":0.5}
    {"Action":"run","Test":"TestPayment"}
    {"Action":"fail","Test":"TestPayment","Elapsed":1.2}
    """
    passed = []
    failed = []
    skipped = []
    total_time = 0.0

    for line in output.strip().split('\n'):
        if not line.strip():
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        action = event.get("Action", "")
        test_name = event.get("Test", "")
        package = event.get("Package", "")
        elapsed = event.get("Elapsed", 0.0)

        # Build full test name
        if test_name:
            if package:
                full_name = f"{package}::{test_name}"
            else:
                full_name = test_name

            if action == "pass":
                passed.append(full_name)
                total_time += elapsed
            elif action == "fail":
                failed.append(full_name)
                total_time += elapsed
            elif action == "skip":
                skipped.append(full_name)

    return TestResult(
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_seconds=total_time
    )


def parse_pytest_output(output: str) -> TestResult:
    """
    Parse pytest verbose output when JUnit XML is not available.

    Looks for patterns like:
    test_file.py::test_name PASSED
    test_file.py::test_name FAILED
    test_file.py::test_name SKIPPED
    """
    passed = []
    failed = []
    skipped = []

    # Pattern for pytest verbose output
    pattern = r'^([\w/.-]+::\w+(?:\[.*?\])?)\s+(PASSED|FAILED|SKIPPED|ERROR)'

    for line in output.split('\n'):
        match = re.match(pattern, line.strip())
        if match:
            test_name = match.group(1)
            status = match.group(2)

            if status == "PASSED":
                passed.append(test_name)
            elif status in ("FAILED", "ERROR"):
                failed.append(test_name)
            elif status == "SKIPPED":
                skipped.append(test_name)

    # Try to extract duration from summary line
    duration = 0.0
    duration_match = re.search(r'in ([\d.]+)s', output)
    if duration_match:
        try:
            duration = float(duration_match.group(1))
        except ValueError:
            pass

    return TestResult(
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_seconds=duration,
        raw_output=output
    )


def parse_cargo_test_output(output: str) -> TestResult:
    """
    Parse cargo test output.

    Looks for patterns like:
    test module::test_name ... ok
    test module::test_name ... FAILED
    test module::test_name ... ignored
    """
    passed = []
    failed = []
    skipped = []

    # Pattern for cargo test output
    pattern = r'^test\s+([\w:]+)\s+\.\.\.\s+(ok|FAILED|ignored)'

    for line in output.split('\n'):
        match = re.match(pattern, line.strip())
        if match:
            test_name = match.group(1)
            status = match.group(2)

            if status == "ok":
                passed.append(test_name)
            elif status == "FAILED":
                failed.append(test_name)
            elif status == "ignored":
                skipped.append(test_name)

    # Try to extract duration
    duration = 0.0
    duration_match = re.search(r'finished in ([\d.]+)s', output)
    if duration_match:
        try:
            duration = float(duration_match.group(1))
        except ValueError:
            pass

    return TestResult(
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_seconds=duration,
        raw_output=output
    )


def parse_rspec_json(json_path: Path) -> TestResult:
    """
    Parse RSpec JSON output format.

    Format:
    {
      "examples": [
        {"full_description": "User login", "status": "passed"},
        {"full_description": "User logout", "status": "failed"}
      ],
      "summary": {"duration": 1.5}
    }
    """
    if not json_path.exists():
        raise OutputParseError(f"RSpec JSON file not found: {json_path}")

    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise OutputParseError(f"Failed to parse RSpec JSON: {e}")

    passed = []
    failed = []
    skipped = []

    for example in data.get("examples", []):
        description = example.get("full_description", "")
        status = example.get("status", "")

        if status == "passed":
            passed.append(description)
        elif status == "failed":
            failed.append(description)
        elif status in ("pending", "skipped"):
            skipped.append(description)

    duration = data.get("summary", {}).get("duration", 0.0)

    return TestResult(
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_seconds=duration
    )


def parse_dotnet_trx(trx_path: Path) -> TestResult:
    """
    Parse .NET TRX (Visual Studio Test Results) format.
    """
    if not trx_path.exists():
        raise OutputParseError(f"TRX file not found: {trx_path}")

    try:
        tree = ET.parse(trx_path)
        root = tree.getroot()
    except ET.ParseError as e:
        raise OutputParseError(f"Failed to parse TRX: {e}")

    # TRX uses namespaces
    ns = {"t": "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"}

    passed = []
    failed = []
    skipped = []
    total_time = 0.0

    for result in root.findall(".//t:UnitTestResult", ns):
        test_name = result.get("testName", "")
        outcome = result.get("outcome", "")
        duration_str = result.get("duration", "00:00:00")

        # Parse duration (format: HH:MM:SS.mmm)
        try:
            parts = duration_str.split(":")
            if len(parts) == 3:
                hours = float(parts[0])
                minutes = float(parts[1])
                seconds = float(parts[2])
                total_time += hours * 3600 + minutes * 60 + seconds
        except ValueError:
            pass

        if outcome == "Passed":
            passed.append(test_name)
        elif outcome == "Failed":
            failed.append(test_name)
        elif outcome in ("NotExecuted", "Inconclusive"):
            skipped.append(test_name)

    return TestResult(
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_seconds=total_time
    )


def parse_vitest_json(json_path: Path) -> TestResult:
    """
    Parse Vitest JSON output format.
    Similar to Jest but may have slight differences.
    """
    # Vitest JSON format is similar to Jest
    return parse_jest_json(json_path)


def parse_mocha_json(json_path: Path) -> TestResult:
    """
    Parse Mocha JSON output format.

    Format:
    {
      "stats": {"duration": 1500},
      "passes": [{"title": "test1", "fullTitle": "Suite test1"}],
      "failures": [{"title": "test2", "fullTitle": "Suite test2"}],
      "pending": [{"title": "test3", "fullTitle": "Suite test3"}]
    }
    """
    if not json_path.exists():
        raise OutputParseError(f"Mocha JSON file not found: {json_path}")

    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise OutputParseError(f"Failed to parse Mocha JSON: {e}")

    passed = [t.get("fullTitle", t.get("title", "")) for t in data.get("passes", [])]
    failed = [t.get("fullTitle", t.get("title", "")) for t in data.get("failures", [])]
    skipped = [t.get("fullTitle", t.get("title", "")) for t in data.get("pending", [])]

    duration = data.get("stats", {}).get("duration", 0) / 1000.0  # ms to seconds

    return TestResult(
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_seconds=duration
    )
