"""
Test Runners for F2P/P2P Analysis

This module provides automated test running capabilities for multiple languages
and frameworks. It's used to verify PR quality by checking F2P (Fail-to-Pass)
and P2P (Pass-to-Pass) test coverage.

Usage:
    from test_runners import analyze_f2p_p2p, preflight_check, get_runner

    # Pre-flight check
    check = preflight_check("/path/to/repo")
    if not check["can_run"]:
        print("Cannot run tests:", check["blockers"])

    # Run analysis
    result = analyze_f2p_p2p(
        repo_path="/path/to/repo",
        base_sha="abc123",
        head_sha="def456",
    )

    if result.success:
        print(f"F2P: {len(result.f2p_tests)}")
        print(f"P2P: {len(result.p2p_tests)}")
        print(f"Verdict: {result.verdict}")
"""

# Core data structures
from .base import (
    TestResult,
    F2PP2PResult,
    TestRunner,
    TestRunnerError,
    RuntimeNotFoundError,
    DependencyInstallError,
    BuildError,
    TestTimeoutError,
    OutputParseError,
)

# Runner registry
from .registry import (
    get_runner,
    get_runner_by_name,
    get_all_detected_runners,
    list_available_runners,
)

# Main analyzer
from .analyzer import (
    F2PP2PAnalyzer,
    analyze_f2p_p2p,
    preflight_check,
)

# Individual runners (for advanced usage)
from .python import PytestRunner, UnittestRunner
from .javascript import JestRunner, VitestRunner, MochaRunner, NodeTestRunner
from .go import GoTestRunner
from .rust import CargoRunner
from .jvm import MavenRunner, GradleRunner, SbtRunner
from .ruby import RSpecRunner, MinitestRunner
from .c_cpp import CMakeRunner, MakeRunner, GoogleTestRunner
from .dotnet import DotNetRunner
from .dotnet_framework import DotNetFrameworkRunner
from .php import PHPUnitRunner, PestRunner
from .cobol import CobolCheckRunner

__all__ = [
    # Data structures
    "TestResult",
    "F2PP2PResult",
    "TestRunner",
    # Exceptions
    "TestRunnerError",
    "RuntimeNotFoundError",
    "DependencyInstallError",
    "BuildError",
    "TestTimeoutError",
    "OutputParseError",
    # Registry
    "get_runner",
    "get_runner_by_name",
    "get_all_detected_runners",
    "list_available_runners",
    # Analyzer
    "F2PP2PAnalyzer",
    "analyze_f2p_p2p",
    "preflight_check",
    # Individual runners
    "PytestRunner",
    "UnittestRunner",
    "JestRunner",
    "VitestRunner",
    "MochaRunner",
    "NodeTestRunner",
    "GoTestRunner",
    "CargoRunner",
    "MavenRunner",
    "GradleRunner",
    "SbtRunner",
    "RSpecRunner",
    "MinitestRunner",
    "CMakeRunner",
    "MakeRunner",
    "GoogleTestRunner",
    "DotNetFrameworkRunner",
    "DotNetRunner",
    "PHPUnitRunner",
    "PestRunner",
    "CobolCheckRunner",
]
