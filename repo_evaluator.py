#!/usr/bin/env python3
"""
Analyzes repositories for quality and suitability by combining:
- Repository-level metrics (file structure, test coverage, CI/CD, etc.)
- PR-level analysis with detailed rejection tracking

Supports GitHub, Bitbucket, and GitLab repositories.

Usage:
    # With GitHub token (for private repos or higher rate limits)
    python repo_evaluator.py owner/repo-name --token $GITHUB_TOKEN

    # With Bitbucket repository
    python repo_evaluator.py bitbucket:owner/repo-name --token $BITBUCKET_TOKEN --platform bitbucket

    # Auto-detect platform from URL
    python repo_evaluator.py https://bitbucket.org/owner/repo --token $BITBUCKET_TOKEN

    # With GitLab repository
    python repo_evaluator.py gitlab:group/subgroup/repo --token $GITLAB_TOKEN --platform gitlab

Examples:
    python repo_evaluator.py microsoft/vscode --token $GITHUB_TOKEN
    python repo_evaluator.py bitbucket:owner/repo --token $BITBUCKET_TOKEN --platform bitbucket
    python repo_evaluator.py gitlab:group/repo --token $GITLAB_TOKEN --platform gitlab
"""

import argparse
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from constants import (
    AI_MARKER_PATTERNS,
    DATA_FILE_EXTENSIONS,
    FEATURE_LABEL_NEGATIVE,
    FEATURE_LABEL_POSITIVE,
    FEATURE_NEGATIVE_PATTERNS,
    FEATURE_POSITIVE_PATTERNS,
    GENERIC_COMMIT_PATTERNS,
    MAX_CHANGED_FILES,
    MAX_FEATURE_CHANGED_FILES,
    MAX_NON_TEST_FILES,
    MAX_TEST_FILES,
    MIN_FEATURE_NET_ADDITIONS,
    MIN_FEATURE_SOURCE_FILES,
    MIN_PR_CODE_CHANGES,
    MIN_TEST_FILES,
    OPEN_SOURCE_HINT_FILES,
    OPEN_SOURCE_KEYWORDS,
    REPO_HEALTH_THRESHOLDS,
)
from platform_clients import (
    BitbucketClient,
    GitHubClient,
    GitLabClient,
    PlatformClient,
    detect_platform,
)
from quality_checks import run_all_quality_checks
from repo_evaluator_helpers import (
    MAX_ISSUE_WORDS,
    MIN_ISSUE_WORDS,
    count_words,
    get_language_config,
    has_rust_embedded_tests,
    has_sufficient_code_changes,
    has_valid_issue_word_count,
    is_asset_file_path,
    is_english,
    is_test_file_path,
    load_language_config,
    normalize_to_utc,
)
from taxonomy_check import run_taxonomy_for_accepted_prs

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def _is_data_file(filepath: str) -> bool:
    return Path(filepath).suffix.lower() in DATA_FILE_EXTENSIONS


def _safe_div(n: float, d: float) -> float:
    return float(n) / float(d) if d else 0.0


def _make_check(value, *, min_val=None, max_val=None, range_min=None, range_max=None):
    if range_min is not None and range_max is not None:
        passed = value >= range_min and value <= range_max
        threshold = {"min": range_min, "max": range_max}
    elif min_val is not None and max_val is not None:
        passed = value >= min_val and value <= max_val
        threshold = {"min": min_val, "max": max_val}
    elif min_val is not None:
        passed = value >= min_val
        threshold = {"min": min_val}
    elif max_val is not None:
        passed = value <= max_val
        threshold = {"max": max_val}
    else:
        passed = True
        threshold = {}
    return {"value": value, "threshold": threshold, "passed": bool(passed)}


def _make_signal(value):
    return {"value": value}


def _find_readme_metrics(repo_path: Path) -> dict:
    candidates = [
        "README.md",
        "README.MD",
        "readme.md",
        "Readme.md",
        "README.rst",
        "README",
    ]
    readme_path = None
    for name in candidates:
        path = repo_path / name
        if path.exists():
            readme_path = path
            break

    if not readme_path:
        return {
            "readme_length_chars": 0,
            "readme_has_badges": False,
            "readme_has_installation": False,
            "readme_has_usage": False,
        }

    try:
        text = readme_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        text = ""

    txt_lower = text.lower()
    return {
        "readme_length_chars": len(text),
        "readme_has_badges": ("shields.io" in txt_lower)
        or ("![" in text and "](" in text),
        "readme_has_installation": any(
            keyword in txt_lower
            for keyword in ["installation", "install", "setup", "getting started"]
        ),
        "readme_has_usage": any(
            keyword in txt_lower
            for keyword in ["usage", "example", "how to run", "quickstart"]
        ),
    }


def _estimate_comment_density(files: List[Path]) -> dict:
    text_exts = {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".phtml",
        ".php3",
        ".php4",
        ".php5",
        ".phps",
        ".cs",
        ".swift",
        ".kt",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cob",
        ".cbl",
        ".cpy",
        ".cobol",
        ".cut",
    }
    single_prefixes = ("#", "//", "--", "*")
    comment_lines = 0
    code_lines = 0

    for file_path in files:
        if file_path.suffix.lower() not in text_exts:
            continue
        try:
            in_block = False
            for raw_line in file_path.read_text(
                encoding="utf-8", errors="ignore"
            ).splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if in_block:
                    comment_lines += 1
                    if "*/" in line:
                        in_block = False
                    continue
                if line.startswith("/*"):
                    comment_lines += 1
                    if "*/" not in line:
                        in_block = True
                    continue
                if line.startswith(single_prefixes):
                    comment_lines += 1
                else:
                    code_lines += 1
        except Exception:
            continue

    return {
        "comment_lines": comment_lines,
        "code_lines": code_lines,
        "comment_density": _safe_div(comment_lines, code_lines),
    }


def compute_process_health_checks(
    repo_metrics: "RepoMetrics",
    pr_analysis: "PRRejectionStats",
    git_metrics: Dict[str, Any],
    readme_metrics: dict,
    comment_metrics: dict,
) -> Dict[str, Any]:
    checks = {}
    t = REPO_HEALTH_THRESHOLDS

    total_commits = int(git_metrics.get("total_commits") or 0)
    total_prs = int(pr_analysis.total_prs or 0)
    pr_to_commit_ratio = _safe_div(total_prs, total_commits)
    avg_loc_per_pr = float(pr_analysis.avg_loc_per_pr or 0.0)
    issue_linked_pr_ratio = float(pr_analysis.issue_linked_pr_ratio or 0.0)

    test_to_source_file_ratio = _safe_div(
        repo_metrics.test_files, repo_metrics.source_files
    )
    test_loc_to_source_loc_ratio = _safe_div(
        repo_metrics.test_loc, repo_metrics.source_loc
    )
    test_files_to_source_files_ratio = _safe_div(
        repo_metrics.test_files, repo_metrics.source_files
    )

    checks["repo_age_days"] = _make_signal(int(git_metrics.get("repo_age_days") or 0))
    checks["contributors_total"] = _make_signal(
        int(git_metrics.get("contributors_total") or 0)
    )
    checks["commit_spread_ratio"] = _make_signal(
        float(git_metrics.get("commit_spread_ratio") or 0.0)
    )
    checks["unique_commit_spread_days"] = _make_signal(
        int(git_metrics.get("unique_commit_days") or 0)
    )
    checks["distinct_commit_days"] = _make_check(
        int(git_metrics.get("unique_commit_days") or 0),
        min_val=t["distinct_commit_days_min"],
    )
    checks["first_commit_loc"] = _make_check(
        int(git_metrics.get("first_commit_loc") or 0), max_val=t["first_commit_loc_max"]
    )
    checks["single_commit_loc_share"] = _make_check(
        float(git_metrics.get("single_commit_loc_share") or 0.0),
        max_val=t["single_commit_loc_share_max"],
    )
    checks["top10_commit_loc_share"] = _make_check(
        float(git_metrics.get("top10_commit_loc_share") or 0.0),
        max_val=t["top10_commit_loc_share_max"],
    )
    checks["commit_message_unique_ratio"] = _make_signal(
        float(git_metrics.get("commit_message_unique_ratio") or 0.0)
    )
    checks["commit_message_avg_len"] = _make_signal(
        float(git_metrics.get("commit_message_avg_len") or 0.0)
    )
    checks["avg_loc_per_commit"] = _make_check(
        float(git_metrics.get("avg_loc_per_commit") or 0.0),
        max_val=t["avg_loc_per_commit_max"],
    )

    checks["pr_to_commit_ratio"] = _make_check(
        pr_to_commit_ratio, min_val=t["pr_to_commit_ratio_min"]
    )
    checks["avg_loc_per_pr"] = _make_check(
        avg_loc_per_pr, max_val=t["avg_loc_per_pr_max"]
    )
    checks["pr_acceptance_rate"] = _make_check(
        float(pr_analysis.acceptance_rate or 0.0), min_val=t["pr_acceptance_rate_min"]
    )
    checks["issue_linked_pr_ratio"] = _make_check(
        issue_linked_pr_ratio, min_val=t["issue_linked_pr_ratio_min"]
    )
    checks["branch_count"] = _make_signal(int(git_metrics.get("branch_count") or 0))

    checks["test_to_source_file_ratio"] = _make_check(
        test_to_source_file_ratio, min_val=t["test_to_source_file_ratio_min"]
    )
    checks["test_loc_to_source_loc_ratio"] = _make_check(
        test_loc_to_source_loc_ratio,
        range_min=t["test_loc_to_source_loc_min"],
        range_max=t["test_loc_to_source_loc_max"],
    )
    checks["test_files_to_source_files_ratio"] = _make_check(
        test_files_to_source_files_ratio,
        range_min=t["test_files_to_source_files_min"],
        range_max=t["test_files_to_source_files_max"],
    )
    checks["code_churn_rate"] = _make_signal(
        float(git_metrics.get("code_churn_rate") or 0.0)
    )
    checks["comment_density"] = _make_signal(
        float(comment_metrics.get("comment_density") or 0.0)
    )

    checks["readme_length_chars"] = _make_signal(
        int(readme_metrics.get("readme_length_chars") or 0)
    )
    checks["readme_has_badges"] = _make_signal(
        bool(readme_metrics.get("readme_has_badges"))
    )
    checks["readme_has_installation"] = _make_signal(
        bool(readme_metrics.get("readme_has_installation"))
    )
    checks["readme_has_usage"] = _make_signal(
        bool(readme_metrics.get("readme_has_usage"))
    )

    git_history_incomplete = (
        total_commits > 0 and int(git_metrics.get("repo_age_days") or 0) == 0
    )
    excluded_threshold_keys = set()
    if git_history_incomplete:
        excluded_threshold_keys = {
            "distinct_commit_days",
            "first_commit_loc",
            "single_commit_loc_share",
            "top10_commit_loc_share",
            "avg_loc_per_commit",
        }

    checks_with_threshold = [
        check
        for key, check in checks.items()
        if "passed" in check and key not in excluded_threshold_keys
    ]
    passed_count = sum(1 for check in checks_with_threshold if check.get("passed"))
    total_count = len(checks_with_threshold)
    summary = {
        "passed_count": passed_count,
        "total_count": total_count,
        "pass_rate": round(_safe_div(passed_count, total_count), 3),
    }
    if git_history_incomplete:
        summary["data_quality_warnings"] = [
            "incomplete_git_history_metrics: commit-history-derived checks excluded from pass-rate"
        ]
    return {"checks": checks, "summary": summary}


def classify_feature_pr(pr_data: dict, language_config: dict) -> dict:
    """Classify a PR as a feature PR using heuristic signals."""
    title = pr_data.get("title", "")
    labels = [
        ln.get("name", "").lower() for ln in pr_data.get("labels", {}).get("nodes", [])
    ]
    files = pr_data.get("files", {}).get("nodes", [])

    score = 0
    signals = []

    has_positive = any(p.search(title) for p in FEATURE_POSITIVE_PATTERNS)
    has_negative = any(p.search(title) for p in FEATURE_NEGATIVE_PATTERNS)

    if has_positive:
        score += 2
        signals.append("title_feature_keyword")
    if has_negative:
        score -= 3
        signals.append("title_bugfix_keyword")

    label_set = set(labels)
    if label_set & FEATURE_LABEL_POSITIVE:
        score += 3
        signals.append("feature_label")
    if label_set & FEATURE_LABEL_NEGATIVE:
        score -= 3
        signals.append("bugfix_label")

    source_files = [
        f
        for f in files
        if not _is_data_file(f["path"])
        and not is_asset_file_path(f["path"], language_config)
    ]
    added_files = [f for f in source_files if f.get("changeType") == "ADDED"]
    total_additions = sum(f.get("additions", 0) for f in source_files)
    total_deletions = sum(f.get("deletions", 0) for f in source_files)
    net_additions = total_additions - total_deletions

    if added_files and len(added_files) >= 6:
        score += 1
        signals.append(f"new_files:{len(added_files)}")

    if net_additions >= 50:
        score += 1
        signals.append(f"net_additions:{net_additions}")

    dirs = set(str(Path(f["path"]).parent) for f in source_files)
    if len(dirs) >= 3:
        score += 1
        signals.append(f"cross_directory:{len(dirs)}")

    if len(source_files) < MIN_FEATURE_SOURCE_FILES:
        return {
            "is_feature": False,
            "score": score,
            "signals": signals,
            "reason": "too_few_source_files",
        }
    if net_additions < MIN_FEATURE_NET_ADDITIONS:
        return {
            "is_feature": False,
            "score": score,
            "signals": signals,
            "reason": "insufficient_net_additions",
        }
    if len(files) > MAX_FEATURE_CHANGED_FILES:
        return {
            "is_feature": False,
            "score": score,
            "signals": signals,
            "reason": "too_many_changed_files",
        }
    if not added_files:
        return {
            "is_feature": False,
            "score": score,
            "signals": signals,
            "reason": "no_new_files",
        }

    is_feature = score >= 2 and has_positive
    reason = None if is_feature else "low_feature_score"
    return {
        "is_feature": is_feature,
        "score": score,
        "signals": signals,
        "reason": reason,
    }


# Data classes
@dataclass
class RepoMetrics:
    """Repository-level metrics."""

    repo_name: str
    total_files: int
    open_issues: int
    closed_issues: int
    total_issues: int
    test_files: int
    test_file_ratio: float
    source_files: int
    total_loc: int
    source_loc: int
    test_loc: int
    languages: Dict[str, int]
    primary_language: str
    has_ci_cd: bool
    ci_files: List[str]
    test_frameworks: List[str]
    has_test_runner: bool
    total_commits: Optional[int]
    recent_commits_6mo: Optional[int]
    recent_commits_12mo: Optional[int]
    commits_referencing_issues: int
    test_coverage_percentage: Optional[float]
    readiness_score: float
    recommendation: str
    strengths: List[str]
    weaknesses: List[str]
    # Days between first and latest commit
    commit_spread_days: Optional[float] = None
    # Median hours between commits (last 90 days)
    median_commit_interval_hours: Optional[float] = None
    open_source_score: int = 0
    open_source_likelihood: str = "low"
    open_source_signals: List[str] = None

    ai_risk_score: int = 0
    ai_risk_level: str = "low"
    ai_risk_signals: List[str] = None
    repo_age_days: Optional[int] = None
    contributors_total: Optional[int] = None
    commit_spread_ratio: Optional[float] = None
    unique_commit_spread_days: Optional[int] = None
    distinct_commit_days: Optional[int] = None
    first_commit_loc: Optional[int] = None
    single_commit_loc_share: Optional[float] = None
    top10_commit_loc_share: Optional[float] = None
    commit_message_unique_ratio: Optional[float] = None
    first_5_commits_loc: List[int] = None
    top_10_commits_by_loc: List[Dict[str, Any]] = None
    commit_message_avg_len: Optional[float] = None
    commit_message_median_len: Optional[float] = None
    commit_message_variance: Optional[float] = None
    avg_loc_per_commit: Optional[float] = None
    median_loc_per_commit: Optional[float] = None
    pr_to_commit_ratio: Optional[float] = None
    avg_loc_per_pr: Optional[float] = None
    pr_acceptance_rate: Optional[float] = None
    issue_linked_pr_ratio: Optional[float] = None
    branch_count: Optional[int] = None
    test_to_source_file_ratio: Optional[float] = None
    test_loc_to_source_loc_ratio: Optional[float] = None
    test_files_to_source_files_ratio: Optional[float] = None
    code_churn_rate: Optional[float] = None
    comment_density: Optional[float] = None
    readme_length_chars: Optional[int] = None
    readme_has_badges: Optional[bool] = None
    readme_has_installation: Optional[bool] = None
    readme_has_usage: Optional[bool] = None
    process_health_checks: Dict[str, Any] = None
    process_health_summary: Dict[str, Any] = None


@dataclass
class PRRejectionStats:
    """PR rejection statistics."""

    accepted_prs: List[dict]
    total_prs: int
    accepted: int
    rejected: int
    acceptance_rate: float
    rejection_breakdown: Dict[str, Dict[str, Any]]
    f2p_results: Optional[List[dict]] = None
    f2p_skipped_reason: Optional[str] = None
    feature_accepted_prs: Optional[List[dict]] = None
    feature_accepted: int = 0
    feature_rejection_breakdown: Optional[Dict[str, Dict[str, Any]]] = None
    avg_loc_per_pr: float = 0.0
    issue_linked_pr_ratio: float = 0.0
    pr_rubrics: Optional[List[dict]] = None
    pr_first_date: Optional[str] = None
    pr_last_date: Optional[str] = None
    pr_spread_days: Optional[int] = None
    pr_unique_dates_count: Optional[int] = None


@dataclass
class AnalysisReport:
    """Complete analysis report."""

    repo_name: str
    repo_full_name: str
    repo_metrics: RepoMetrics
    pr_analysis: PRRejectionStats
    overall_score: float
    recommendation: str


def _problem_statement_for_pr(pr: dict) -> str:
    """Prefer linked issue body; else PR title + body."""
    nodes = pr.get("closingIssuesReferences", {}).get("nodes", [])
    for issue in nodes:
        if issue.get("__typename") == "PullRequest":
            continue
        body = (issue.get("body") or "").strip()
        if body:
            return body
    title = (pr.get("title") or "").strip()
    body = (pr.get("body") or "").strip()
    chunks = [c for c in (title, body) if c]
    return "\n\n".join(chunks) if chunks else ""


# Rubric key in trimmed dict -> CSV column (count of PRs with score <= 1)
_PR_RUBRIC_AGG_KEYS: Tuple[Tuple[str, str], ...] = (
    ("issue_clarity", "pr_rubrics_issue_clarity"),
    ("gold_patch_clarity", "pr_rubrics_gold_patch_clarity"),
    ("test_clarity", "pr_rubrics_test_clarity"),
    ("gold_patch_to_issue_alignment", "pr_rubrics_gold_patch_issue_alignment"),
    ("test_to_issue_alignment", "pr_rubrics_test_issue_alignment"),
    ("false_negatives", "pr_rubrics_false_negatives"),
    ("false_positives", "pr_rubrics_false_positives"),
)


def _pr_rubric_aggregate_counts(pr_rubrics: Optional[List[dict]]) -> Dict[str, int]:
    """Count PRs per rubric where score <= 1 (lower is better on 0–3 scale)."""
    out: Dict[str, int] = {col: 0 for _, col in _PR_RUBRIC_AGG_KEYS}
    if not pr_rubrics:
        return out
    for entry in pr_rubrics:
        rubrics = entry.get("rubrics")
        if not isinstance(rubrics, dict):
            continue
        for rubric_key, col_name in _PR_RUBRIC_AGG_KEYS:
            block = rubrics.get(rubric_key)
            if not isinstance(block, dict):
                continue
            score = block.get("score")
            if score is None:
                continue
            try:
                if int(score) <= 1:
                    out[col_name] += 1
            except (TypeError, ValueError):
                continue
    return out


# Platform helpers


def _is_bot_username(username: str) -> bool:
    if not username:
        return False

    username_lower = username.lower()
    if username.endswith("[bot]"):
        return True

    common_bots = [
        "dependabot",
        "renovate",
        "codecov",
        "greenkeeper",
        "snyk-bot",
        "pyup-bot",
        "whitesource",
        "mergify",
        "stale",
        "github-actions",
        "allcontributors",
        "imgbot",
        "k8s-ci-robot",
        "k8s-bot",
        "k8s-mergebot",
    ]

    if username_lower in common_bots:
        return True

    return False


# Repository Analyzer
class RepoAnalyzer:
    """Analyze repository structure and metrics."""

    LANGUAGE_EXTENSIONS = {
        "Python": [".py"],
        "JavaScript": [".js", ".jsx", ".mjs"],
        "TypeScript": [".ts", ".tsx"],
        "Java": [".java"],
        "Scala": [".scala"],
        "C++": [".cpp", ".cc", ".cxx", ".hpp", ".h"],
        "C": [".c", ".h"],
        "Go": [".go"],
        "Rust": [".rs"],
        "Ruby": [".rb"],
        "PHP": [".php", ".phtml", ".php3", ".php4", ".php5", ".phps"],
        "C#": [".cs"],
        "Swift": [".swift"],
        "Kotlin": [".kt"],
        "COBOL": [".cob", ".cbl", ".cpy", ".cobol", ".cut"],
    }

    TEST_PATTERNS = [
        # Python
        r"test.*\.py$",
        r".*_test\.py$",
        # JavaScript / TypeScript
        r".*\.test\.(js|ts|jsx|tsx)$",
        r".*\.spec\.(js|ts|jsx|tsx)$",
        # Generic directory names
        r"test/.*",
        r"tests/.*",
        r"__tests__/.*",
        # Java / Scala
        r".*Test\.(java|scala)$",
        r".*Spec\.(java|scala)$",
        # C# (.NET)
        r".*Tests?\.cs$",
        r".*Spec\.cs$",
        # C# project-style directories (e.g. MyApp.Tests/, MyApp.UnitTests/)
        r".*\.Tests?[/\\].*",
        # Go
        r".*_test\.go$",
        # Ruby
        r".*_spec\.rb$",
        r".*_test\.rb$",
        # Kotlin
        r".*Tests?\.kt$",
        r".*Spec\.kt$",
        # PHP
        r".*Test\.php$",
        r".*Tests?\.php$",
        r".*_test\.php$",
        r".*Spec\.php$",
        r".*test.*\.php$",
        r".*_spec.*\.php$",
        # COBOL
        r".*test.*\.(cob|cbl|cobol)$",
        r".*spec.*\.(cob|cbl|cobol)$",
        r".*[/\\](test|tests|spec|specs|qa|integration|it)[/\\].*\.(cob|cbl|cpy|cobol)$",
        r".*\.cut$",
        # Swift
        r".*Tests?\.swift$",
        # C / C++
        r".*_test\.(cpp|cc|cxx|c)$",
    ]

    CI_FILES = [
        ".github/workflows",
        ".gitlab-ci.yml",
        ".travis.yml",
        "Jenkinsfile",
        ".circleci",
        "azure-pipelines.yml",
        ".drone.yml",
        "buildkite.yml",
    ]

    TEST_FRAMEWORKS = {
        "pytest": ["pytest", "pyproject.toml", "pytest.ini", "setup.cfg"],
        "unittest": ["unittest"],
        "jest": ["jest.config", "package.json"],
        "mocha": ["mocha", ".mocharc", "package.json"],
        "vitest": ["vitest.config", "package.json"],
        "junit": ["junit", "build.gradle", "pom.xml"],
        "scalatest": ["scalatest", "build.gradle", "build.sbt"],
        "rspec": ["rspec", ".rspec", "spec/"],
        "go test": ["_test.go"],
        "cargo test": ["Cargo.toml"],
        "xunit": ["xunit"],
        "nunit": ["nunit"],
        "mstest": ["mstest", "MSTest"],
        "phpunit": ["phpunit", "phpunit.xml", "phpunit.xml.dist"],
        "pest": ["pest", "pest.php"],
        "codeception": ["codeception", "codeception.yml", "codeception.yaml"],
        "behat": ["behat", "behat.yml", "behat.yaml"],
        "cobol-check": ["cobol-check"],
    }

    def __init__(
        self,
        repo_path: str,
        owner: Optional[str] = None,
        repo_name: Optional[str] = None,
        platform_client: Optional[PlatformClient] = None,
    ):
        self.repo_path = Path(repo_path).resolve()
        if not self.repo_path.exists():
            raise ValueError(f"Repository path does not exist: {repo_path}")
        self.repo_name = self.repo_path.name
        self.is_git_repo = (self.repo_path / ".git").exists()
        self.owner = owner
        self.repo_name_github = repo_name
        self.platform_client = platform_client

    def analyze(self) -> RepoMetrics:
        """Run full repository analysis."""
        logger.info(f"Analyzing repository: {self.repo_name}")

        files = self._get_all_files()
        total_files = len(files)

        # Always count languages for metrics, but try GitHub API first for primary language
        language_counts = self._count_by_language(files)

        # Try to get primary language from platform API first
        primary_language = self._get_primary_language_from_api()

        # If GitHub API didn't return a language, fall back to file counting
        if not primary_language:
            primary_language = (
                max(language_counts.items(), key=lambda x: x[1])[0]
                if language_counts
                else "Unknown"
            )

            # If no language detected from files, try fallback detection
            if primary_language == "Unknown":
                primary_language = self._detect_language_from_indicators()

        source_files = self._count_source_files(files, language_counts)
        test_files = self._find_test_files(files)
        test_file_ratio = len(test_files) / total_files if total_files > 0 else 0

        loc_counts = self._count_lines_of_code(files)
        ci_files = self._find_ci_files()
        has_ci_cd = len(ci_files) > 0
        test_frameworks = self._detect_test_frameworks()
        has_test_runner = len(test_frameworks) > 0
        comment_metrics = _estimate_comment_density(files)

        git_metrics = self._analyze_git_history() if self.is_git_repo else {}

        # Try to find coverage reports
        test_coverage = self._find_coverage_reports()

        score_data = self._calculate_score(
            test_file_ratio=test_file_ratio,
            has_ci_cd=has_ci_cd,
            has_test_runner=has_test_runner,
            test_frameworks=test_frameworks,
            git_metrics=git_metrics,
            primary_language=primary_language,
            test_coverage=test_coverage,
        )

        open_source_signals = self._compute_open_source_signals(files=files)
        ai_risk_signals = self._compute_ai_risk_signals(
            files=files, git_metrics=git_metrics, test_file_ratio=test_file_ratio
        )

        issue_count = self.platform_client.fetch_issue_count()

        return RepoMetrics(
            repo_name=self.repo_name,
            total_files=total_files,
            open_issues=issue_count.get("open", 0),
            closed_issues=issue_count.get("closed", 0),
            total_issues=issue_count.get("total", 0),
            test_files=len(test_files),
            test_file_ratio=test_file_ratio,
            source_files=source_files,
            total_loc=loc_counts["total_loc"],
            source_loc=loc_counts["source_loc"],
            test_loc=loc_counts["test_loc"],
            languages=language_counts,
            primary_language=primary_language,
            has_ci_cd=has_ci_cd,
            ci_files=ci_files,
            test_frameworks=test_frameworks,
            has_test_runner=has_test_runner,
            total_commits=git_metrics.get("total_commits"),
            recent_commits_6mo=git_metrics.get("recent_commits_6mo"),
            recent_commits_12mo=git_metrics.get("recent_commits_12mo"),
            commits_referencing_issues=git_metrics.get("commits_referencing_issues", 0),
            test_coverage_percentage=test_coverage,
            readiness_score=score_data["score"],
            recommendation=score_data["recommendation"],
            strengths=score_data["strengths"],
            weaknesses=score_data["weaknesses"],
            commit_spread_days=git_metrics.get("commit_spread_days"),
            median_commit_interval_hours=git_metrics.get(
                "median_commit_interval_hours"
            ),
            open_source_score=open_source_signals["score"],
            open_source_likelihood=open_source_signals["level"],
            open_source_signals=open_source_signals["signals"],
            ai_risk_score=ai_risk_signals["score"],
            ai_risk_level=ai_risk_signals["level"],
            ai_risk_signals=ai_risk_signals["signals"],
            repo_age_days=git_metrics.get("repo_age_days"),
            contributors_total=git_metrics.get("contributors_total"),
            commit_spread_ratio=git_metrics.get("commit_spread_ratio"),
            unique_commit_spread_days=git_metrics.get("unique_commit_days"),
            distinct_commit_days=git_metrics.get("unique_commit_days"),
            first_commit_loc=git_metrics.get("first_commit_loc"),
            single_commit_loc_share=git_metrics.get("single_commit_loc_share"),
            top10_commit_loc_share=git_metrics.get("top10_commit_loc_share"),
            commit_message_unique_ratio=git_metrics.get("commit_message_unique_ratio"),
            first_5_commits_loc=git_metrics.get("first_5_commits_loc", []),
            top_10_commits_by_loc=git_metrics.get("top_10_commits_by_loc", []),
            commit_message_avg_len=git_metrics.get("commit_message_avg_len"),
            commit_message_median_len=git_metrics.get("commit_message_median_len"),
            commit_message_variance=git_metrics.get("commit_message_variance"),
            avg_loc_per_commit=git_metrics.get("avg_loc_per_commit"),
            median_loc_per_commit=git_metrics.get("median_loc_per_commit"),
            pr_to_commit_ratio=None,
            avg_loc_per_pr=None,
            pr_acceptance_rate=None,
            issue_linked_pr_ratio=None,
            branch_count=git_metrics.get("branch_count"),
            test_to_source_file_ratio=None,
            test_loc_to_source_loc_ratio=None,
            test_files_to_source_files_ratio=None,
            code_churn_rate=git_metrics.get("code_churn_rate"),
            comment_density=comment_metrics.get("comment_density"),
            readme_length_chars=None,
            readme_has_badges=None,
            readme_has_installation=None,
            readme_has_usage=None,
        )

    def _get_all_files(self) -> List[Path]:
        """Get all files in repository."""
        files = []
        if self.is_git_repo:
            try:
                result = subprocess.run(
                    ["git", "ls-files"],
                    cwd=self.repo_path,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    file_paths = [
                        f.strip()
                        for f in result.stdout.strip().split("\n")
                        if f.strip()
                    ]
                    for f in file_paths:
                        file_path = self.repo_path / f
                        if file_path.exists():
                            files.append(file_path)
                    if len(files) > 0:
                        return files
            except Exception:
                pass

        ignore_dirs = {
            ".git",
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
            "dist",
            "build",
            ".gradle",
            "target",
        }
        for root, dirs, filenames in os.walk(self.repo_path):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for filename in filenames:
                files.append(Path(root) / filename)
        return files

    def _get_primary_language_from_api(self) -> Optional[str]:
        """Get primary language from platform API based on highest byte count."""
        if not self.platform_client:
            return None

        try:
            languages = self.platform_client.fetch_repo_languages()
            if not languages:
                return None

            # Find language with highest byte count
            primary_language = (
                max(languages.items(), key=lambda x: x[1])[0] if languages else None
            )

            # Only return if the language exists in our supported languages
            if primary_language and primary_language in self.LANGUAGE_EXTENSIONS:
                return primary_language
            else:
                # return the next language with the highest byte count
                primary_language = (
                    list(languages.keys())[1] if len(languages) > 1 else None
                )
                if primary_language and primary_language in self.LANGUAGE_EXTENSIONS:
                    return primary_language

            return None
        except Exception as e:
            logger.debug(f"Error getting primary language from API: {e}")
            return None

    def _count_by_language(self, files: List[Path]) -> Dict[str, int]:
        """Count files by language."""
        counts = {}
        for file_path in files:
            ext = file_path.suffix.lower()
            for language, extensions in self.LANGUAGE_EXTENSIONS.items():
                if ext in extensions:
                    counts[language] = counts.get(language, 0) + 1
                    break
        return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))

    def _detect_language_from_indicators(self) -> str:
        """Detect language from indicator files when no source files are found."""
        # Language indicator files mapping
        indicators = {
            "Python": [
                "requirements.txt",
                "setup.py",
                "pyproject.toml",
                "Pipfile",
                "setup.cfg",
                "tox.ini",
                "manage.py",
            ],
            "JavaScript": [
                "package.json",
                "package-lock.json",
                "yarn.lock",
                ".nvmrc",
                "bower.json",
            ],
            "TypeScript": ["tsconfig.json", "tsconfig.*.json"],
            "Java": [
                "pom.xml",
                "build.gradle",
                "build.gradle.kts",
                "gradlew",
                "gradlew.bat",
                ".classpath",
                ".project",
            ],
            "Scala": ["build.sbt", "project/build.properties"],
            "Go": ["go.mod", "go.sum", "Gopkg.toml", "Gopkg.lock", "glide.yaml"],
            "Rust": ["Cargo.toml", "Cargo.lock"],
            "Ruby": ["Gemfile", "Gemfile.lock", "Rakefile", ".ruby-version"],
            "PHP": ["composer.json", "composer.lock", ".php-version"],
            "C#": [".csproj", ".sln", "project.json", "paket.dependencies"],
            "Swift": ["Package.swift", ".swift-version"],
            "Kotlin": ["build.gradle.kts"],
            "COBOL": [
                ".cbl",
                ".cob",
                ".cobol",
                ".cpy",
                "GnuCOBOL",
                "cobol-check",
                "cobol-check.toml",
            ],
            "C++": ["CMakeLists.txt", "Makefile", "configure", "configure.ac"],
            "C": ["Makefile", "configure", "configure.ac", "autogen.sh"],
        }

        # Check for indicator files
        for language, indicator_files in indicators.items():
            for indicator in indicator_files:
                # Handle patterns like 'tsconfig.*.json'
                if "*" in indicator:
                    pattern = indicator.replace("*", ".*")
                    for file_path in self.repo_path.rglob(
                        indicator.split("*")[0] + "*"
                    ):
                        if file_path.is_file() and re.match(pattern, file_path.name):
                            return language
                else:
                    indicator_path = self.repo_path / indicator
                    if indicator_path.exists() and indicator_path.is_file():
                        return language

        return "Unknown"

    def _count_source_files(
        self, files: List[Path], language_counts: Dict[str, int]
    ) -> int:
        """Count source files."""
        code_extensions = set()
        for lang in [
            "Python",
            "JavaScript",
            "TypeScript",
            "Java",
            "Scala",
            "Go",
            "Rust",
            "C++",
            "C",
            "Ruby",
            "PHP",
            "C#",
            "Swift",
            "Kotlin",
            "COBOL",
        ]:
            if lang in language_counts:
                code_extensions.update(self.LANGUAGE_EXTENSIONS[lang])

        source_count = 0
        for file_path in files:
            ext = file_path.suffix.lower()
            if ext in code_extensions:
                rel_path = str(file_path.relative_to(self.repo_path))
                if not any(
                    re.search(pattern, rel_path, flags=re.IGNORECASE)
                    for pattern in self.TEST_PATTERNS
                ):
                    if not any(
                        name in rel_path for name in ["config", "setup", "__init__"]
                    ):
                        source_count += 1
        return source_count

    def _count_lines_of_code(self, files: List[Path]) -> Dict[str, int]:
        """Count lines of code."""
        code_extensions = set()
        for lang in [
            "Python",
            "JavaScript",
            "TypeScript",
            "Java",
            "Scala",
            "Go",
            "Rust",
            "C++",
            "C",
            "Ruby",
            "PHP",
            "C#",
            "Swift",
            "Kotlin",
            "COBOL",
        ]:
            code_extensions.update(self.LANGUAGE_EXTENSIONS.get(lang, []))

        total_loc = 0
        source_loc = 0
        test_loc = 0

        for file_path in files:
            ext = file_path.suffix.lower()
            if ext in code_extensions:
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = [line for line in f if line.strip()]
                        loc = len(lines)

                    total_loc += loc
                    rel_path = str(file_path.relative_to(self.repo_path))
                    is_test = any(
                        re.search(pattern, rel_path, flags=re.IGNORECASE)
                        for pattern in self.TEST_PATTERNS
                    )

                    if is_test:
                        test_loc += loc
                    else:
                        source_loc += loc
                except Exception:
                    pass

        return {"total_loc": total_loc, "source_loc": source_loc, "test_loc": test_loc}

    def _find_test_files(self, files: List[Path]) -> List[Path]:
        """Find test files."""
        test_files = []
        for file_path in files:
            rel_path = str(file_path.relative_to(self.repo_path))
            if any(
                re.search(pattern, rel_path, flags=re.IGNORECASE)
                for pattern in self.TEST_PATTERNS
            ):
                test_files.append(file_path)
        return test_files

    def _find_ci_files(self) -> List[str]:
        """Find CI/CD files."""
        ci_files = []
        for ci_path in self.CI_FILES:
            full_path = self.repo_path / ci_path
            if full_path.exists():
                ci_files.append(ci_path)
        return ci_files

    def _detect_test_frameworks(self) -> List[str]:
        """Detect test frameworks."""
        frameworks = []
        config_files = [
            "package.json",
            "pyproject.toml",
            "requirements.txt",
            "build.gradle",
            "pom.xml",
            "Cargo.toml",
            "go.mod",
            "composer.json",
            "composer.lock",
            "Makefile",
            "config.properties",
        ]
        csproj_files = list(self.repo_path.rglob("*.csproj"))
        all_config_paths = [self.repo_path / f for f in config_files] + csproj_files

        for framework, indicators in self.TEST_FRAMEWORKS.items():
            for indicator in indicators:
                if "/" in indicator or "." in indicator:
                    if (self.repo_path / indicator).exists():
                        if framework not in frameworks:
                            frameworks.append(framework)
                        break
                else:
                    for config_path in all_config_paths:
                        if config_path.exists():
                            try:
                                content = config_path.read_text()
                                if indicator.lower() in content.lower():
                                    if framework not in frameworks:
                                        frameworks.append(framework)
                                    break
                            except Exception:
                                pass
        return frameworks

    def _find_coverage_reports(self) -> Optional[float]:
        """Find and parse coverage reports if available."""
        # Common coverage report locations
        coverage_paths = [
            self.repo_path / "coverage.xml",
            self.repo_path / "coverage" / "coverage.xml",
            self.repo_path / "coverage" / "cobertura.xml",
            self.repo_path / "htmlcov" / "coverage.xml",
            self.repo_path / ".coverage.xml",
            self.repo_path / "lcov.info",
            self.repo_path / "coverage" / "lcov.info",
            self.repo_path / "coverage-final.json",
            self.repo_path / "coverage" / "coverage-final.json",
            self.repo_path / ".nyc_output" / "coverage-final.json",
        ]

        for cov_path in coverage_paths:
            if not cov_path.exists():
                continue

            try:
                # Try parsing coverage.xml (Cobertura format)
                if cov_path.suffix == ".xml":
                    coverage = self._parse_coverage_xml(cov_path)
                    if coverage is not None:
                        logger.info(
                            f"Found coverage report: {cov_path} ({coverage:.1f}% coverage)"
                        )
                        return coverage

                # Try parsing lcov.info
                elif cov_path.name == "lcov.info":
                    coverage = self._parse_lcov_info(cov_path)
                    if coverage is not None:
                        logger.info(
                            f"Found coverage report: {cov_path} ({coverage:.1f}% coverage)"
                        )
                        return coverage

                # Try parsing coverage-final.json (Istanbul/NYC)
                elif cov_path.name == "coverage-final.json":
                    coverage = self._parse_coverage_json(cov_path)
                    if coverage is not None:
                        logger.info(
                            f"Found coverage report: {cov_path} ({coverage:.1f}% coverage)"
                        )
                        return coverage
            except Exception as e:
                logger.debug(f"Failed to parse coverage report {cov_path}: {e}")
                continue

        return None

    def _parse_coverage_xml(self, xml_path: Path) -> Optional[float]:
        """Parse Cobertura XML coverage report."""
        try:
            import xml.etree.ElementTree as ET

            tree = ET.parse(xml_path)
            root = tree.getroot()

            # Cobertura format: <coverage line-rate="0.85" branch-rate="0.70">
            line_rate = root.get("line-rate")
            if line_rate:
                return float(line_rate) * 100

            # Alternative: calculate from packages
            total_lines = 0
            covered_lines = 0
            for package in root.findall(".//package"):
                for class_elem in package.findall(".//class"):
                    for line in class_elem.findall(".//line"):
                        total_lines += 1
                        if line.get("hits") and int(line.get("hits", 0)) > 0:
                            covered_lines += 1

            if total_lines > 0:
                return (covered_lines / total_lines) * 100
        except Exception as e:
            logger.debug(f"Error parsing XML coverage: {e}")
        return None

    def _parse_lcov_info(self, lcov_path: Path) -> Optional[float]:
        """Parse LCOV info coverage report."""
        try:
            total_lines = 0
            covered_lines = 0

            with open(lcov_path, "r") as f:
                for line in f:
                    line = line.strip()
                    # LCOV format: DA:<line_number>,<execution_count>
                    if line.startswith("DA:"):
                        parts = line[3:].split(",")
                        if len(parts) == 2:
                            total_lines += 1
                            try:
                                if int(parts[1]) > 0:
                                    covered_lines += 1
                            except ValueError:
                                pass
                    # Use summary at end_of_record if available (more accurate)
                    elif line.startswith("LF:"):
                        # LF: total lines found
                        try:
                            lf_value = int(line.split(":")[1])
                            # Use summary if we haven't counted many lines yet
                            if total_lines < 100:  # Prefer summary for large files
                                total_lines = lf_value
                        except (ValueError, IndexError):
                            pass
                    elif line.startswith("LH:"):
                        # LH: lines hit
                        try:
                            lh_value = int(line.split(":")[1])
                            # Use summary if we haven't counted many lines yet
                            if covered_lines < 100:  # Prefer summary for large files
                                covered_lines = lh_value
                        except (ValueError, IndexError):
                            pass

            if total_lines > 0:
                return (covered_lines / total_lines) * 100
        except Exception as e:
            logger.debug(f"Error parsing LCOV coverage: {e}")
        return None

    def _parse_coverage_json(self, json_path: Path) -> Optional[float]:
        """Parse Istanbul/NYC coverage-final.json report."""
        try:
            with open(json_path, "r") as f:
                data = json.load(f)

            total_statements = 0
            covered_statements = 0

            # NYC format: { "path/to/file.js": { "s": { "1": 1, "2": 0, ... } } }
            for file_path, file_data in data.items():
                # Skip test files and node_modules
                if "test" in file_path.lower() or "node_modules" in file_path:
                    continue

                statements = file_data.get("s", {})
                for stmt_id, count in statements.items():
                    total_statements += 1
                    if count and count > 0:
                        covered_statements += 1

            if total_statements > 0:
                return (covered_statements / total_statements) * 100
        except Exception as e:
            logger.debug(f"Error parsing JSON coverage: {e}")
        return None

    def _calculate_score(
        self,
        test_file_ratio: float,
        has_ci_cd: bool,
        has_test_runner: bool,
        test_frameworks: List[str],
        git_metrics: Dict[str, Any],
        primary_language: str,
        test_coverage: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Calculate readiness score."""
        score = 0.0
        strengths = []
        weaknesses = []

        # Test coverage (40 points max)
        # Use actual coverage percentage if available, otherwise fall back to file ratio
        if test_coverage is not None:
            # Use actual coverage percentage (0-100 scale)
            # 100% coverage = 40 points
            test_score = min(test_coverage * 0.4, 40)
            score += test_score
            if test_coverage >= 80:
                strengths.append(f"Excellent test coverage ({test_coverage:.1f}%)")
            elif test_coverage >= 60:
                strengths.append(f"Good test coverage ({test_coverage:.1f}%)")
            elif test_coverage >= 40:
                strengths.append(f"Moderate test coverage ({test_coverage:.1f}%)")
            else:
                weaknesses.append(f"Low test coverage ({test_coverage:.1f}%)")
        else:
            # Fallback to file ratio
            test_score = min(test_file_ratio * 400, 40)
            score += test_score
            if test_file_ratio >= 0.10:
                strengths.append(f"Good test file ratio ({test_file_ratio * 100:.1f}%)")
            elif test_file_ratio >= 0.05:
                strengths.append(
                    f"Moderate test file ratio ({test_file_ratio * 100:.1f}%)"
                )
            else:
                weaknesses.append(f"Low test file ratio ({test_file_ratio * 100:.1f}%)")

        # CI/CD (15 points)
        if has_ci_cd:
            score += 15
            strengths.append("CI/CD pipeline configured")
        else:
            weaknesses.append("No CI/CD pipeline detected")

        # Test runner (15 points)
        if has_test_runner:
            score += 15
            strengths.append(f"Test frameworks: {', '.join(test_frameworks)}")
        else:
            weaknesses.append("No test framework detected")

        # Git activity (15 points)
        if git_metrics.get("recent_commits_6mo", 0) > 10:
            score += 15
            strengths.append(
                f"Active development ({git_metrics['recent_commits_6mo']} commits in 6mo)"
            )
        elif git_metrics.get("recent_commits_6mo", 0) > 0:
            score += 7
            strengths.append(
                f"Some recent activity ({git_metrics['recent_commits_6mo']} commits in 6mo)"
            )
        else:
            weaknesses.append("No recent commits (last 6 months)")

        # Issue tracking (15 points)
        issue_refs = git_metrics.get("commits_referencing_issues", 0)
        if issue_refs > 20:
            score += 15
            strengths.append(
                f"Good issue tracking ({issue_refs} commits reference issues)"
            )
        elif issue_refs > 5:
            score += 10
            # strengths.append(
            #     f"Some issue tracking ({issue_refs} commits reference issues)")
        elif issue_refs > 0:
            score += 5
        else:
            weaknesses.append("Few/no commits reference issues")

        # Recommendation
        if score >= 70:
            recommendation = "🌟 EXCELLENT - Highly suitable for training samples"
        elif score >= 50:
            recommendation = (
                "✅ GOOD - Suitable for training samples with some limitations"
            )
        elif score >= 30:
            recommendation = "⚠️  FAIR - May be suitable but has significant gaps"
        else:
            recommendation = "❌ POOR - Not recommended for training samples"

        return {
            "score": round(score, 1),
            "recommendation": recommendation,
            "strengths": strengths,
            "weaknesses": weaknesses,
        }

    def _analyze_git_history(self) -> Dict[str, Any]:
        """Analyze git history."""
        metrics = {}
        try:
            result = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if result.returncode == 0:
                metrics["total_commits"] = int(result.stdout.strip())

            result = subprocess.run(
                ["git", "rev-list", "--count", "--since=6.months.ago", "HEAD"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if result.returncode == 0:
                metrics["recent_commits_6mo"] = int(result.stdout.strip())

            result = subprocess.run(
                ["git", "rev-list", "--count", "--since=12.months.ago", "HEAD"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if result.returncode == 0:
                metrics["recent_commits_12mo"] = int(result.stdout.strip())

            result = subprocess.run(
                ["git", "log", "--all", "--oneline", "--grep=#[0-9]"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if result.returncode == 0:
                metrics["commits_referencing_issues"] = (
                    len(result.stdout.strip().split("\n"))
                    if result.stdout.strip()
                    else 0
                )

            # Calculate commit spread (days between first and latest commit)
            # Get ALL commit timestamps to find min (first) and max (latest)
            all_ts_result = subprocess.run(
                ["git", "log", "--format=%ct"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            if all_ts_result.returncode == 0 and all_ts_result.stdout.strip():
                all_timestamps = [
                    int(ts) for ts in all_ts_result.stdout.strip().split("\n") if ts
                ]
                if len(all_timestamps) >= 2:
                    first_timestamp = min(all_timestamps)
                    latest_timestamp = max(all_timestamps)
                    spread_seconds = latest_timestamp - first_timestamp
                    metrics["commit_spread_days"] = round(
                        spread_seconds / 86400, 1
                    )  # 86400 seconds in a day

            # Calculate median commit interval in the last 90 days
            result = subprocess.run(
                ["git", "log", "--since=90 days ago", "--format=%ct"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                timestamps = [int(ts) for ts in result.stdout.strip().split("\n") if ts]
                if len(timestamps) >= 2:
                    # Sort timestamps in ascending order (oldest first)
                    timestamps.sort()
                    # Calculate intervals between consecutive commits
                    intervals_hours = []
                    for i in range(1, len(timestamps)):
                        interval_seconds = timestamps[i] - timestamps[i - 1]
                        intervals_hours.append(
                            interval_seconds / 3600
                        )  # Convert to hours
                    # Calculate median
                    intervals_hours.sort()
                    n = len(intervals_hours)
                    if n % 2 == 0:
                        median = (
                            intervals_hours[n // 2 - 1] + intervals_hours[n // 2]
                        ) / 2
                    else:
                        median = intervals_hours[n // 2]
                    metrics["median_commit_interval_hours"] = round(median, 2)

            # Commit-level metadata (timestamps, authors, messages)
            commit_meta_result = subprocess.run(
                ["git", "log", "--format=%ct\t%an\t%s"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
            )
            commit_messages = []
            commit_days = set()
            contributors = set()
            if commit_meta_result.returncode == 0 and commit_meta_result.stdout.strip():
                for line in commit_meta_result.stdout.splitlines():
                    parts = line.split("\t", 2)
                    if len(parts) < 3:
                        continue
                    ts_str, author, message = parts
                    try:
                        ts = int(ts_str)
                        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                            "%Y-%m-%d"
                        )
                        commit_days.add(day)
                    except Exception:
                        pass
                    if author:
                        contributors.add(author.strip().lower())
                    if message:
                        commit_messages.append(message.strip())

            metrics["unique_commit_days"] = len(commit_days)
            metrics["contributors_total"] = len(contributors)
            metrics["commit_messages"] = commit_messages

            # Repo age days from first to latest commit timestamp
            if all_ts_result.returncode == 0 and all_ts_result.stdout.strip():
                all_timestamps = [
                    int(ts) for ts in all_ts_result.stdout.strip().split("\n") if ts
                ]
                if all_timestamps:
                    first_ts = min(all_timestamps)
                    latest_ts = max(all_timestamps)
                    repo_age_days = max(1, int((latest_ts - first_ts) / 86400))
                    metrics["repo_age_days"] = repo_age_days
                    metrics["commit_spread_ratio"] = round(
                        _safe_div(len(commit_days), repo_age_days), 4
                    )

            # Branch count
            branch_result = subprocess.run(
                ["git", "branch", "--all", "--format=%(refname:short)"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if branch_result.returncode == 0:
                branches = set()
                for ref in branch_result.stdout.splitlines():
                    ref = ref.strip()
                    if not ref or ref.endswith("/HEAD"):
                        continue
                    branches.add(ref)
                metrics["branch_count"] = len(branches)

            # Commit LOC distribution + churn from non-merge commit diffs
            numstat_result = subprocess.run(
                ["git", "log", "--no-merges", "--numstat", "--format=__COMMIT__%x09%H"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            commit_loc_list = []
            commit_loc_items = []
            file_touch_counts: Dict[str, int] = {}
            if numstat_result.returncode == 0:
                current_loc = 0
                current_sha = None
                seen_in_commit = set()
                for line in numstat_result.stdout.splitlines():
                    if line.startswith("__COMMIT__"):
                        if seen_in_commit:
                            for fp in seen_in_commit:
                                file_touch_counts[fp] = file_touch_counts.get(fp, 0) + 1
                        if current_loc > 0:
                            commit_loc_list.append(current_loc)
                            commit_loc_items.append(
                                {"sha": current_sha, "loc": current_loc}
                            )
                        parts = line.split("\t")
                        current_sha = parts[1].strip() if len(parts) > 1 else None
                        current_loc = 0
                        seen_in_commit = set()
                        continue
                    parts = line.split("\t")
                    if len(parts) < 3:
                        continue
                    add_str, del_str, file_path = parts[0], parts[1], parts[2]
                    if add_str.isdigit() and del_str.isdigit():
                        current_loc += int(add_str) + int(del_str)
                    if file_path:
                        seen_in_commit.add(file_path)
                if seen_in_commit:
                    for fp in seen_in_commit:
                        file_touch_counts[fp] = file_touch_counts.get(fp, 0) + 1
                if current_loc > 0:
                    commit_loc_list.append(current_loc)
                    commit_loc_items.append({"sha": current_sha, "loc": current_loc})

            metrics["commit_loc_list"] = commit_loc_list
            metrics["commit_loc_items"] = commit_loc_items
            total_commit_loc = sum(commit_loc_list)
            sorted_locs = sorted(commit_loc_list, reverse=True)
            largest = sorted_locs[0] if sorted_locs else 0
            top10 = sum(sorted_locs[:10]) if sorted_locs else 0
            # Use parsed non-merge commits as denominator for changed-LOC realism.
            metrics["avg_loc_per_commit"] = round(
                _safe_div(
                    total_commit_loc,
                    len(commit_loc_list)
                    if commit_loc_list
                    else metrics.get("total_commits", 0),
                ),
                2,
            )
            if commit_loc_list:
                sorted_for_median = sorted(commit_loc_list)
                n_loc = len(sorted_for_median)
                if n_loc % 2 == 0:
                    median_loc = (
                        sorted_for_median[n_loc // 2 - 1]
                        + sorted_for_median[n_loc // 2]
                    ) / 2
                else:
                    median_loc = sorted_for_median[n_loc // 2]
                metrics["median_loc_per_commit"] = round(median_loc, 2)
            else:
                metrics["median_loc_per_commit"] = 0.0
            metrics["single_commit_loc_share"] = round(
                _safe_div(largest, total_commit_loc), 4
            )
            metrics["top10_commit_loc_share"] = round(
                _safe_div(top10, total_commit_loc), 4
            )
            metrics["first_5_commits_loc"] = list(reversed(commit_loc_list))[:5]
            metrics["top_10_commits_by_loc"] = sorted(
                commit_loc_items, key=lambda x: x.get("loc", 0), reverse=True
            )[:10]

            files_touched_total = len(file_touch_counts)
            files_touched_2plus = sum(
                1 for count in file_touch_counts.values() if count >= 2
            )
            metrics["files_touched_total"] = files_touched_total
            metrics["files_touched_2plus"] = files_touched_2plus
            metrics["code_churn_rate"] = round(
                _safe_div(files_touched_2plus, files_touched_total), 4
            )

            # First commit LOC
            root_result = subprocess.run(
                ["git", "rev-list", "--max-parents=0", "HEAD"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if root_result.returncode == 0 and root_result.stdout.strip():
                first_commit = root_result.stdout.strip().splitlines()[0]
                first_numstat = subprocess.run(
                    ["git", "show", "--numstat", "--format=", first_commit],
                    cwd=self.repo_path,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                first_loc = 0
                if first_numstat.returncode == 0:
                    for line in first_numstat.stdout.splitlines():
                        parts = line.split("\t")
                        if (
                            len(parts) >= 2
                            and parts[0].isdigit()
                            and parts[1].isdigit()
                        ):
                            first_loc += int(parts[0]) + int(parts[1])
                metrics["first_commit_loc"] = first_loc

            # Commit message variance
            if commit_messages:
                normalized = [msg.lower() for msg in commit_messages if msg.strip()]
                message_lengths = [len(msg) for msg in commit_messages if msg.strip()]
                metrics["commit_message_unique_ratio"] = round(
                    _safe_div(len(set(normalized)), len(normalized)), 4
                )
                metrics["commit_message_avg_len"] = round(
                    _safe_div(
                        sum(len(msg) for msg in commit_messages), len(commit_messages)
                    ),
                    2,
                )
                if message_lengths:
                    sorted_lengths = sorted(message_lengths)
                    n_msg = len(sorted_lengths)
                    if n_msg % 2 == 0:
                        median_len = (
                            sorted_lengths[n_msg // 2 - 1] + sorted_lengths[n_msg // 2]
                        ) / 2
                    else:
                        median_len = sorted_lengths[n_msg // 2]
                    avg_len = _safe_div(sum(message_lengths), n_msg)
                    variance = _safe_div(
                        sum((v - avg_len) ** 2 for v in message_lengths), n_msg
                    )
                    metrics["commit_message_median_len"] = round(median_len, 2)
                    metrics["commit_message_variance"] = round(variance, 2)

        except Exception as e:
            logger.warning(f"Could not analyze git history: {e}")

        return metrics

    def _compute_open_source_signals(self, files: List[Path]) -> Dict[str, Any]:
        score = 0
        signals = []

        names = {p.name for p in files}
        matched = sorted(OPEN_SOURCE_HINT_FILES.intersection(names))
        if matched:
            score += min(50, len(matched) * 15)
            signals.append(f"hint_files:{','.join(matched[:4])}")

        readme_path = self.repo_path / "README.md"
        if readme_path.exists():
            try:
                txt = readme_path.read_text(errors="ignore").lower()
                kw_hits = [kw for kw in OPEN_SOURCE_KEYWORDS if kw in txt]
                if kw_hits:
                    score += min(30, 10 * len(kw_hits))
                    signals.append(f"readme_keywords:{len(kw_hits)}")
            except Exception:
                pass

        # manifest license hints
        for name in (
            "package.json",
            "pyproject.toml",
            "Cargo.toml",
            "setup.py",
            "pom.xml",
            "build.gradle",
            "build.sbt",
        ):
            p = self.repo_path / name
            if not p.exists():
                continue
            try:
                txt = p.read_text(errors="ignore").lower()
                if '"license"' in txt or "license =" in txt:
                    score += 20
                    signals.append(f"manifest_license:{name}")
                    break
            except Exception:
                pass

        score = max(0, min(100, score))
        if score >= 60:
            level = "high"
        elif score >= 30:
            level = "medium"
        else:
            level = "low"

        return {"score": score, "level": level, "signals": signals}

    def _compute_ai_risk_signals(
        self,
        files: List[Path],
        git_metrics: Dict[str, Any],
        test_file_ratio: float,
    ) -> Dict[str, Any]:
        score = 0
        signals = []

        # explicit markers in repo text
        marker_hits = 0
        text_exts = {
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".java",
            ".go",
            ".rs",
            ".md",
            ".txt",
        }
        scanned = 0
        for p in files:
            if p.suffix.lower() not in text_exts:
                continue
            if scanned >= 300:
                break
            scanned += 1
            try:
                txt = p.read_text(errors="ignore").lower()
            except Exception:
                continue
            for pat in AI_MARKER_PATTERNS:
                if re.search(pat, txt):
                    marker_hits += 1
                    break

        if marker_hits:
            score += min(45, marker_hits * 9)
            signals.append(f"explicit_markers:{marker_hits}")

        # generic commit message ratio
        generic_ratio = 0.0
        try:
            result = subprocess.run(
                ["git", "log", "--pretty=%s", "-n", "200"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                msgs = [
                    m.strip().lower() for m in result.stdout.splitlines() if m.strip()
                ]
                if msgs:
                    generic = 0
                    for m in msgs:
                        if any(term in m for term in GENERIC_COMMIT_PATTERNS):
                            generic += 1
                    generic_ratio = generic / len(msgs)
        except Exception:
            pass

        if generic_ratio >= 0.5:
            score += 25
            signals.append(f"generic_commit_ratio:{generic_ratio:.2f}")
        elif generic_ratio >= 0.35:
            score += 12
            signals.append(f"generic_commit_ratio:{generic_ratio:.2f}")

        # weak tests + active repo => extra risk
        recent = git_metrics.get("recent_commits_6mo") or 0
        if recent >= 30 and test_file_ratio < 0.03:
            score += 20
            signals.append(
                f"low_tests_active_repo:tfr={test_file_ratio:.3f},recent={recent}"
            )

        score = max(0, min(100, score))
        if score >= 70:
            level = "high"
        elif score >= 40:
            level = "medium"
        else:
            level = "low"

        return {"score": score, "level": level, "signals": signals}


# PR Analyzer
class PRAnalyzer:
    """Analyze PRs with rejection tracking."""

    def __init__(
        self,
        platform_client: PlatformClient,
        language_config: dict,
        repo_path: str,
        min_test_files: int = MIN_TEST_FILES,
        max_non_test_files: int = MAX_NON_TEST_FILES,
        min_code_changes: int = MIN_PR_CODE_CHANGES,
        start_date: Optional[datetime] = None,
    ):
        self.platform_client = platform_client
        self.owner = platform_client.owner
        self.repo_name = platform_client.repo_name
        self.repo_full_name = platform_client.repo_full_name
        self.repo_path = Path(repo_path)
        self.language_config = language_config
        self.min_test_files = min_test_files
        self.max_non_test_files = max_non_test_files
        self.min_code_changes = min_code_changes
        self.start_date = start_date

    def _commit_exists(self, sha: str) -> bool:
        """Check if a commit exists in the local repo."""
        check = subprocess.run(
            ["git", "cat-file", "-t", sha],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return check.returncode == 0

    def _fetch_pr_ref(self, pr_number: int) -> bool:
        """Fetch a PR's head ref from origin."""
        try:
            result = subprocess.run(
                ["git", "fetch", "origin", f"pull/{pr_number}/head:pr-{pr_number}"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"Failed to fetch PR ref: {e}")
            return False

    def _has_origin_remote(self) -> bool:
        check = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return check.returncode == 0

    def _fetch_oid_from_origin(self, oid: str) -> bool:
        """Fetch a single commit into the local object db (helps shallow / --repo-path checkouts)."""
        if not oid or len(oid) < 7:
            return False
        if not self._has_origin_remote():
            return False
        try:
            result = subprocess.run(
                ["git", "fetch", "-q", "origin", oid],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.debug(
                    "git fetch origin %s failed: %s",
                    oid[:8],
                    (result.stderr or result.stdout or "").strip()[:200],
                )
            return result.returncode == 0
        except Exception as e:
            logger.debug("git fetch origin %s exception: %s", oid[:8], e)
            return False

    def _ensure_commits_for_patch(
        self,
        base_sha: str,
        head_sha: str,
        pr_number: Optional[int],
    ) -> None:
        """Best-effort: pull missing base/head objects from origin (GitHub-style remotes)."""
        if not self._has_origin_remote():
            return

        if not self._commit_exists(head_sha) and pr_number:
            logger.debug(
                "Head commit %s not found locally, fetching PR #%s ref...",
                head_sha[:8],
                pr_number,
            )
            self._fetch_pr_ref(pr_number)

        if not self._commit_exists(base_sha):
            logger.debug(
                "Base commit %s not found locally, fetching from origin...",
                base_sha[:8],
            )
            self._fetch_oid_from_origin(base_sha)

        if not self._commit_exists(head_sha):
            logger.debug(
                "Head commit %s still missing locally, fetching from origin...",
                head_sha[:8],
            )
            self._fetch_oid_from_origin(head_sha)

    def _get_patch_from_git(
        self, base_sha: str, head_sha: str, pr_number: Optional[int] = None
    ) -> Optional[str]:
        """Get patch/diff between two commits, with API fallback."""
        patch = None

        # Try local git first
        try:
            self._ensure_commits_for_patch(base_sha, head_sha, pr_number)

            # Try git diff if both commits exist
            if self._commit_exists(base_sha) and self._commit_exists(head_sha):
                result = subprocess.run(
                    ["git", "diff", f"{base_sha}...{head_sha}"],
                    cwd=self.repo_path,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                logger.debug("Git diff failed: %s", result.stderr)
            else:
                logger.debug(
                    "Commits not available locally after fetch attempts "
                    "(base: %s, head: %s)",
                    self._commit_exists(base_sha),
                    self._commit_exists(head_sha),
                )

        except subprocess.TimeoutExpired:
            logger.debug("Git diff timed out for %s..%s", base_sha[:8], head_sha[:8])
        except Exception as e:
            logger.debug("Git diff error: %s", e)

        # Fallback to API
        logger.info(
            "Using API for PR patch %s...%s%s (local git diff unavailable)",
            base_sha[:8],
            head_sha[:8],
            f" #{pr_number}" if pr_number else "",
        )
        try:
            from repo_evaluator_helpers import get_full_patch_content

            patch = get_full_patch_content(
                self.repo_full_name,
                base_sha,
                head_sha,
                token=self.platform_client.token,
                platform_client=self.platform_client,
            )
            if patch:
                logger.debug("Successfully retrieved patch from API")
            else:
                logger.warning(
                    "API returned no patch for %s...%s. "
                    "Pass --token if unauthenticated requests fail; for --repo-path use a "
                    "full clone or a remote 'origin' that matches %s.",
                    base_sha[:8],
                    head_sha[:8],
                    self.repo_full_name,
                )
            return patch
        except Exception as e:
            logger.warning("API patch retrieval failed: %s", e)
            return None

    def analyze_prs(self, max_prs: Optional[int] = None) -> PRRejectionStats:
        """Analyze PRs and track rejections."""
        logger.info(f"Analyzing PRs for {self.repo_full_name}...")

        cursor = None
        total_prs = 0
        accepted = 0
        rejected = 0
        rejection_reasons = {}
        accepted_prs = []

        feature_accepted = 0
        feature_rejected = 0
        feature_rejection_reasons = {}
        feature_accepted_prs = []
        total_pr_loc = 0
        prs_with_issue_links = 0
        pr_created_datetimes = []

        while True:
            try:
                res = self.platform_client.fetch_prs(
                    cursor, page_size=50, start_date=self.start_date
                )

                if res.get("errors"):
                    error_msg = res["errors"][0]["message"]
                    # Check for rate limit errors
                    if "rate limit" in error_msg.lower() or "403" in error_msg:
                        logger.error(
                            "API rate limit exceeded. Please provide a token using --token"
                        )
                    logger.error(f"API error: {error_msg}")
                    break

                repo_data = res.get("data", {}).get("repository", {})
                pr_data = repo_data.get("pullRequests", {})
                language_name = repo_data.get("primaryLanguage", {}).get("name", None)
                pr_nodes = pr_data.get("nodes", [])
                page_info = pr_data.get("pageInfo", {})

                if not pr_nodes:
                    break

                for pr_data in pr_nodes:
                    if max_prs and total_prs >= max_prs:
                        break

                    logger.info(f"Processing PR #{pr_data['number']}...")

                    # Parse dates - handle both GitHub (Z suffix) and Bitbucket (no Z) formats
                    created_at_str = pr_data["createdAt"]
                    if created_at_str.endswith("Z"):
                        created_at_str = created_at_str.replace("Z", "+00:00")
                    elif "+" not in created_at_str and created_at_str.count(":") == 2:
                        # Bitbucket format without timezone - assume UTC
                        created_at_str = created_at_str + "+00:00"
                    pr_created_at = normalize_to_utc(
                        datetime.fromisoformat(created_at_str)
                    )
                    pr_created_datetimes.append(pr_created_at)

                    merged_at_str = pr_data["mergedAt"]
                    if merged_at_str and merged_at_str.endswith("Z"):
                        merged_at_str = merged_at_str.replace("Z", "+00:00")
                    elif (
                        merged_at_str
                        and "+" not in merged_at_str
                        and merged_at_str.count(":") == 2
                    ):
                        # Bitbucket format without timezone - assume UTC
                        merged_at_str = merged_at_str + "+00:00"
                    pr_merged_at = (
                        normalize_to_utc(datetime.fromisoformat(merged_at_str))
                        if merged_at_str
                        else None
                    )
                    pr_number = pr_data["number"]
                    pr_files_nodes = pr_data.get("files", {}).get("nodes", [])
                    total_pr_loc += sum(
                        (f.get("additions", 0) or 0) + (f.get("deletions", 0) or 0)
                        for f in pr_files_nodes
                    )
                    raw_issue_nodes = pr_data.get("closingIssuesReferences", {}).get(
                        "nodes", []
                    )
                    if raw_issue_nodes:
                        prs_with_issue_links += 1
                    else:
                        extracted_issue_numbers = (
                            self.platform_client.extract_issue_number_from_text(
                                pr_data.get("body", "") or ""
                            )
                        )
                        if extracted_issue_numbers:
                            prs_with_issue_links += 1

                    total_prs += 1

                    # === Shared filters (reject from both buckets) ===
                    shared_failed = None
                    shared_reason = None

                    author_info = pr_data.get("author", {})
                    is_bot = (
                        author_info.get("__typename") == "Bot" if author_info else False
                    )
                    author_login = author_info.get("login", "") if author_info else ""
                    if not is_bot and author_login:
                        is_bot = _is_bot_username(author_login)

                    if is_bot:
                        shared_failed = "bot_pr"
                        shared_reason = f"PR is from bot account: {author_login}"

                    if (
                        not shared_failed
                        and self.start_date
                        and pr_merged_at < self.start_date
                    ):
                        shared_failed = "merge_date"
                        shared_reason = f"PR merged {pr_merged_at.date()} before start date {self.start_date.date()}"
                    elif not shared_failed and pr_created_at is None:
                        shared_failed = "creation_date"
                        shared_reason = "PR has no createdAt date"

                    pr_body = pr_data.get("body", "") or ""
                    if not shared_failed:
                        if not (
                            is_english(pr_data.get("title", "")) and is_english(pr_body)
                        ):
                            shared_failed = "content_not_in_english"
                            shared_reason = "Content may not be in English"

                    if shared_failed:
                        rejected += 1
                        rejection_reasons[shared_failed] = (
                            rejection_reasons.get(shared_failed, 0) + 1
                        )
                        feature_rejected += 1
                        feature_rejection_reasons[shared_failed] = (
                            feature_rejection_reasons.get(shared_failed, 0) + 1
                        )
                        logger.info(
                            f"PR #{pr_number} rejected (shared): {shared_failed} - {shared_reason}"
                        )
                        continue

                    # === Sample-specific filters ===
                    failed_filter = None
                    filter_reason = None

                    # Closing issues validation
                    if not failed_filter:
                        closing_issues = pr_data.get("closingIssuesReferences", {}).get(
                            "nodes", []
                        )

                        # If no closing issues from API, try regex extraction from PR body
                        if not closing_issues:
                            issue_numbers = (
                                self.platform_client.extract_issue_number_from_text(
                                    pr_body
                                )
                            )
                            if issue_numbers:
                                logger.info(
                                    f"PR #{pr_number}: Using regex fallback for issues {issue_numbers} via API."
                                )
                                for issue_num in issue_numbers:
                                    try:
                                        issue_data_from_api = (
                                            self.platform_client.fetch_issue(issue_num)
                                        )
                                        if issue_data_from_api:
                                            closing_issues.append(issue_data_from_api)
                                    except Exception as e:
                                        logger.warning(
                                            f"Failed to fetch fallback issue #{issue_num} via API: {e}"
                                        )

                        # If we have closing issues, validate them
                        if closing_issues:
                            found_valid_issue = False
                            for issue_data in closing_issues:
                                issue_number = issue_data.get("number")
                                issue_typename = issue_data.get("__typename", "Issue")

                                # Check if issue is not a PR
                                if issue_typename == "PullRequest":
                                    continue  # Skip this issue, try next one

                                # Check if issue is closed
                                issue_state = issue_data.get("state", "").lower()
                                if issue_state != "closed":
                                    continue  # Skip this issue, try next one

                                # Check word count
                                issue_body = issue_data.get("body", "") or ""
                                if not has_valid_issue_word_count(issue_body):
                                    continue  # Skip this issue, try next one

                                # If we get here, this issue passed all validations
                                found_valid_issue = True
                                break  # Found a valid issue, no need to check others

                            # If no issue passed validation, reject the PR
                            if not found_valid_issue:
                                # Use the first issue's failure reason (or a generic one)
                                first_issue = closing_issues[0]
                                issue_number = first_issue.get("number")
                                issue_typename = first_issue.get("__typename", "Issue")

                                if issue_typename == "PullRequest":
                                    failed_filter = "issue_is_a_pr"
                                    filter_reason = f"Linked issue #{issue_number} is a Pull Request"
                                else:
                                    issue_state = first_issue.get("state", "").lower()
                                    if issue_state != "closed":
                                        failed_filter = "issue_is_not_closed"
                                        filter_reason = f"Linked issue #{issue_number} is not closed (state: {issue_state})"
                                    else:
                                        issue_body = first_issue.get("body", "") or ""
                                        word_count = count_words(issue_body)
                                        failed_filter = "issue_word_count"
                                        filter_reason = f"Issue #{issue_number} word count ({word_count}) is outside {MIN_ISSUE_WORDS}-{MAX_ISSUE_WORDS} range"
                        # If no closing issues found at all, continue with PR analysis (don't reject)

                    # File filters
                    if not failed_filter:
                        test_files = [
                            f
                            for f in pr_files_nodes
                            if is_test_file_path(f["path"], self.language_config)
                            and not is_asset_file_path(f["path"], self.language_config)
                        ]
                        non_test_files = [
                            f
                            for f in pr_files_nodes
                            if not is_test_file_path(f["path"], self.language_config)
                            and not is_asset_file_path(f["path"], self.language_config)
                        ]

                        if len(test_files) < self.min_test_files:
                            failed_filter = "fewer_than_min_test_files"
                            filter_reason = (
                                f"PR has fewer than {self.min_test_files} test files"
                            )
                        elif len(non_test_files) > self.max_non_test_files:
                            failed_filter = "more_than_max_non_test_files"
                            filter_reason = f"PR has more than {self.max_non_test_files} non-test files"
                        elif len(non_test_files + test_files) <= 5:
                            failed_filter = "difficulty_not_hard"
                            filter_reason = (
                                "PR has less than 5 files (difficulty not hard enough)"
                            )
                        elif len(test_files) > MAX_TEST_FILES:
                            failed_filter = "too_many_test_files"
                            filter_reason = (
                                f"PR has more than {MAX_TEST_FILES} test files"
                            )
                        else:
                            code_files = [
                                f
                                for f in pr_files_nodes
                                if not _is_data_file(f["path"])
                            ]
                            if len(code_files) > MAX_CHANGED_FILES:
                                failed_filter = "too_many_changed_files"
                                filter_reason = f"PR has more than {MAX_CHANGED_FILES} changed code files"

                    # Code changes filter (requires patch)
                    if not failed_filter:
                        try:
                            full_patch = self._get_patch_from_git(
                                pr_data["baseRefOid"],
                                pr_data["headRefOid"],
                                pr_number=pr_number,
                            )
                            if not full_patch:
                                failed_filter = "full_patch_retrieval"
                                filter_reason = "Could not retrieve full patch"
                            else:
                                # Rust embedded tests check
                                if language_name == "Rust" and has_rust_embedded_tests(
                                    pr_files_nodes, full_patch, self.language_config
                                ):
                                    failed_filter = "rust_embedded_tests"
                                    filter_reason = "Rust files contain embedded tests"
                                else:
                                    # Check code changes
                                    has_sufficient, source_changes = (
                                        has_sufficient_code_changes(
                                            full_patch,
                                            self.language_config,
                                            self.min_code_changes,
                                        )
                                    )
                                    if not has_sufficient:
                                        failed_filter = "code_changes_not_sufficient"
                                        filter_reason = f"Code changes {source_changes} below {self.min_code_changes}"
                        except Exception as e:
                            logger.warning(f"Error processing PR #{pr_number}: {e}")
                            failed_filter = "pr_processing_error"
                            filter_reason = f"Exception during processing: {str(e)}"

                    # Track sample result
                    if failed_filter:
                        rejected += 1
                        rejection_reasons[failed_filter] = (
                            rejection_reasons.get(failed_filter, 0) + 1
                        )
                        logger.info(
                            f"PR #{pr_number} rejected: {failed_filter} - {filter_reason}"
                        )
                    else:
                        accepted += 1
                        accepted_prs.append(pr_data)
                        logger.info(f"PR #{pr_number} accepted")

                    # === Feature PR classification (independent of sample criteria) ===
                    feature_result = classify_feature_pr(pr_data, self.language_config)
                    if feature_result["is_feature"]:
                        feature_accepted += 1
                        feature_accepted_prs.append(
                            {
                                **pr_data,
                                "feature_score": feature_result["score"],
                                "feature_signals": feature_result["signals"],
                            }
                        )
                        logger.info(
                            f"PR #{pr_number} feature accepted (score={feature_result['score']}, signals={feature_result['signals']})"
                        )
                    else:
                        feature_rejected += 1
                        reason = feature_result["reason"]
                        feature_rejection_reasons[reason] = (
                            feature_rejection_reasons.get(reason, 0) + 1
                        )

                if max_prs and total_prs >= max_prs:
                    break

                if not page_info.get("hasNextPage"):
                    break
                cursor = page_info.get("endCursor")

            except Exception as e:
                error_str = str(e)
                # Check for rate limit errors
                if "rate limit" in error_str.lower() or "403" in error_str:
                    logger.error(
                        "API rate limit exceeded. Please provide a token using --token"
                    )
                    logger.error(f"Error: {e}")
                elif (
                    "Failed to resolve" in error_str
                    or "nodename nor servname" in error_str
                ):
                    logger.error(
                        f"Network/DNS error fetching PRs from {self.repo_full_name}: {e}"
                    )
                    logger.error(
                        "This could be a temporary network issue. Please check your internet connection and try again."
                    )
                else:
                    logger.error(
                        f"Error fetching PRs from {self.repo_full_name} for cursor {cursor}: {e}"
                    )
                break

        # Build rejection breakdowns
        rejection_breakdown = {}
        for filter_name, count in rejection_reasons.items():
            rejection_breakdown[filter_name] = {
                "count": count,
                "percentage": round((count / rejected * 100) if rejected > 0 else 0, 1),
            }

        feature_rejection_breakdown = {}
        for filter_name, count in feature_rejection_reasons.items():
            feature_rejection_breakdown[filter_name] = {
                "count": count,
                "percentage": round(
                    (count / feature_rejected * 100) if feature_rejected > 0 else 0, 1
                ),
            }

        acceptance_rate = (accepted / total_prs) if total_prs > 0 else 0.0
        avg_loc_per_pr = _safe_div(total_pr_loc, total_prs)
        issue_linked_pr_ratio = _safe_div(prs_with_issue_links, total_prs)

        if pr_created_datetimes:
            _first = min(pr_created_datetimes)
            _last = max(pr_created_datetimes)
            pr_first_date = _first.date().isoformat()
            pr_last_date = _last.date().isoformat()
            pr_spread_days = (_last - _first).days
            pr_unique_dates_count = len({d.date() for d in pr_created_datetimes})
        else:
            pr_first_date = pr_last_date = None
            pr_spread_days = pr_unique_dates_count = 0

        return PRRejectionStats(
            total_prs=total_prs,
            accepted=accepted,
            rejected=rejected,
            acceptance_rate=round(acceptance_rate, 3),
            rejection_breakdown=rejection_breakdown,
            accepted_prs=accepted_prs,
            feature_accepted_prs=feature_accepted_prs,
            feature_accepted=feature_accepted,
            feature_rejection_breakdown=feature_rejection_breakdown,
            avg_loc_per_pr=round(avg_loc_per_pr, 2),
            issue_linked_pr_ratio=round(issue_linked_pr_ratio, 3),
            pr_first_date=pr_first_date,
            pr_last_date=pr_last_date,
            pr_spread_days=pr_spread_days,
            pr_unique_dates_count=pr_unique_dates_count,
        )


class RepoEvaluator:
    def __init__(
        self,
        repo_path: str,
        owner: str,
        repo_name: str,
        platform_client: PlatformClient,
        min_test_files: int = MIN_TEST_FILES,
        max_non_test_files: int = MAX_NON_TEST_FILES,
        min_code_changes: int = MIN_PR_CODE_CHANGES,
        start_date: Optional[datetime] = None,
        max_prs: Optional[int] = None,
        skip_f2p: bool = False,
        f2p_timeout: int = 600,
        pr_number: Optional[int] = None,
        skip_pr_rubrics: bool = False,
    ):
        self.repo_path = repo_path
        self.owner = owner
        self.repo_name = repo_name
        self.repo_full_name = f"{owner}/{repo_name}"
        self.platform_client = platform_client

        self.min_test_files = min_test_files
        self.max_non_test_files = max_non_test_files
        self.min_code_changes = min_code_changes
        self.start_date = start_date
        self.max_prs = max_prs
        self.skip_f2p = skip_f2p
        self.f2p_timeout = f2p_timeout
        self.pr_number = pr_number
        self.language_config = load_language_config()
        self.skip_pr_rubrics = skip_pr_rubrics

    def evaluate(self) -> AnalysisReport:
        if not self.platform_client:
            raise ValueError("Platform client is required")
        if not self.owner:
            raise ValueError("Owner is required")
        if not self.repo_name:
            raise ValueError("Repository name is required")

        logger.info(f"Evaluating repository: {self.repo_full_name}")

        repo_analyzer = RepoAnalyzer(
            repo_path=self.repo_path,
            owner=self.owner,
            repo_name=self.repo_name,
            platform_client=self.platform_client,
        )
        repo_metrics = repo_analyzer.analyze()

        primary_language = repo_metrics.primary_language
        if primary_language in ["Vue", "React"]:
            primary_language = "TypeScript"

        language_config = get_language_config(primary_language)
        if not language_config:
            logger.warning(
                f"Language '{primary_language}' not found, using generic fallback"
            )
            language_config = get_language_config("Unknown")  # Generic fallback

        pr_analyzer = PRAnalyzer(
            platform_client=self.platform_client,
            language_config=language_config,
            repo_path=self.repo_path,
            min_test_files=self.min_test_files,
            max_non_test_files=self.max_non_test_files,
            min_code_changes=self.min_code_changes,
            start_date=self.start_date,
        )

        try:
            pr_analysis = pr_analyzer.analyze_prs(max_prs=self.max_prs)
            if pr_analysis.total_prs == 0:
                logger.warning("No PRs were analyzed. This could be due to:")
                logger.warning(
                    "Rate limit exceeded (provide --token)"
                ) if not self.platform_client.token else None
                logger.warning("No merged PRs found in the repository")
                logger.warning("API access issues")
        except Exception as e:
            logger.error(f"Error analyzing PRs: {e}")
            import traceback

            logger.debug(traceback.format_exc())
            pr_analysis = PRRejectionStats(
                total_prs=0,
                accepted=0,
                rejected=0,
                acceptance_rate=0.0,
                rejection_breakdown={},
                accepted_prs=[],
                pr_rubrics=[],
            )

        if not self.skip_f2p and pr_analysis.accepted_prs:
            pr_analysis = self._run_f2p_analysis(pr_analysis, primary_language)

        pr_analysis = self._run_pr_rubrics(pr_analysis, language_config)

        # Repo health checks
        files = repo_analyzer._get_all_files()
        git_metrics = (
            repo_analyzer._analyze_git_history() if repo_analyzer.is_git_repo else {}
        )
        readme_metrics = _find_readme_metrics(Path(self.repo_path))
        comment_metrics = _estimate_comment_density(files)
        process_health = compute_process_health_checks(
            repo_metrics=repo_metrics,
            pr_analysis=pr_analysis,
            git_metrics=git_metrics,
            readme_metrics=readme_metrics,
            comment_metrics=comment_metrics,
        )
        checks = process_health.get("checks", {})

        def _check_value(key: str, default=None):
            value = checks.get(key, {})
            if isinstance(value, dict):
                return value.get("value", default)
            return value if value is not None else default

        repo_metrics.repo_age_days = _check_value(
            "repo_age_days", repo_metrics.repo_age_days
        )
        repo_metrics.contributors_total = _check_value(
            "contributors_total", repo_metrics.contributors_total
        )
        repo_metrics.commit_spread_ratio = _check_value(
            "commit_spread_ratio", repo_metrics.commit_spread_ratio
        )
        repo_metrics.unique_commit_spread_days = _check_value(
            "unique_commit_spread_days", repo_metrics.unique_commit_spread_days
        )
        repo_metrics.distinct_commit_days = _check_value(
            "distinct_commit_days", repo_metrics.distinct_commit_days
        )
        repo_metrics.first_commit_loc = _check_value(
            "first_commit_loc", repo_metrics.first_commit_loc
        )
        repo_metrics.single_commit_loc_share = _check_value(
            "single_commit_loc_share", repo_metrics.single_commit_loc_share
        )
        repo_metrics.top10_commit_loc_share = _check_value(
            "top10_commit_loc_share", repo_metrics.top10_commit_loc_share
        )
        repo_metrics.commit_message_unique_ratio = _check_value(
            "commit_message_unique_ratio", repo_metrics.commit_message_unique_ratio
        )
        repo_metrics.avg_loc_per_commit = _check_value(
            "avg_loc_per_commit", repo_metrics.avg_loc_per_commit
        )
        repo_metrics.pr_to_commit_ratio = _check_value(
            "pr_to_commit_ratio",
            _safe_div(pr_analysis.total_prs, repo_metrics.total_commits or 0),
        )
        repo_metrics.avg_loc_per_pr = _check_value(
            "avg_loc_per_pr", pr_analysis.avg_loc_per_pr
        )
        repo_metrics.pr_acceptance_rate = _check_value(
            "pr_acceptance_rate", pr_analysis.acceptance_rate
        )
        repo_metrics.issue_linked_pr_ratio = _check_value(
            "issue_linked_pr_ratio", pr_analysis.issue_linked_pr_ratio
        )
        repo_metrics.test_to_source_file_ratio = _check_value(
            "test_to_source_file_ratio",
            _safe_div(repo_metrics.test_files, repo_metrics.source_files),
        )
        repo_metrics.test_loc_to_source_loc_ratio = _check_value(
            "test_loc_to_source_loc_ratio",
            _safe_div(repo_metrics.test_loc, repo_metrics.source_loc),
        )
        repo_metrics.test_files_to_source_files_ratio = _check_value(
            "test_files_to_source_files_ratio",
            _safe_div(repo_metrics.test_files, repo_metrics.source_files),
        )
        repo_metrics.code_churn_rate = _check_value(
            "code_churn_rate", repo_metrics.code_churn_rate
        )
        repo_metrics.comment_density = _check_value(
            "comment_density", repo_metrics.comment_density
        )
        repo_metrics.readme_length_chars = _check_value(
            "readme_length_chars", readme_metrics.get("readme_length_chars")
        )
        repo_metrics.readme_has_badges = _check_value(
            "readme_has_badges", readme_metrics.get("readme_has_badges")
        )
        repo_metrics.readme_has_installation = _check_value(
            "readme_has_installation", readme_metrics.get("readme_has_installation")
        )
        repo_metrics.readme_has_usage = _check_value(
            "readme_has_usage", readme_metrics.get("readme_has_usage")
        )
        repo_metrics.process_health_checks = process_health["checks"]
        repo_metrics.process_health_summary = process_health["summary"]

        # Calculate overall score (weighted: 60% repo, 40% PR acceptance rate)
        repo_score = repo_metrics.readiness_score
        pr_score = pr_analysis.acceptance_rate * 100 if pr_analysis.total_prs > 0 else 0
        overall_score = (repo_score * 0.6) + (pr_score * 0.4)

        # Overall recommendation
        if overall_score >= 70:
            recommendation = "🌟 GREAT"
        elif overall_score >= 50:
            recommendation = "✅ GOOD"
        elif overall_score >= 30:
            recommendation = "⚠️ FAIR"
        else:
            recommendation = "❌ POOR"

        return AnalysisReport(
            repo_name=self.repo_name,
            repo_full_name=self.repo_full_name,
            repo_metrics=repo_metrics,
            pr_analysis=pr_analysis,
            overall_score=overall_score,
            recommendation=recommendation,
        )

    def _run_f2p_analysis(
        self, pr_analysis: PRRejectionStats, primary_language: str
    ) -> PRRejectionStats:
        from test_runners import F2PP2PAnalyzer, get_runner, preflight_check

        check = preflight_check(self.repo_path, primary_language)
        if not check["can_run"]:
            blockers = check["blockers"]
            reason = blockers[0]["message"] if blockers else "Unknown blocker"
            logger.warning(f"F2P analysis skipped: {blockers}")
            pr_analysis.f2p_skipped_reason = reason
            return pr_analysis

        logger.info(
            f"Running F2P/P2P analysis on {len(pr_analysis.accepted_prs)} accepted PRs..."
        )
        logger.info(f"Detected: {check['detected']}")

        runner = get_runner(Path(self.repo_path), primary_language)
        if not runner:
            logger.warning("F2P analysis skipped: No suitable test runner found")
            pr_analysis.f2p_skipped_reason = "No suitable test runner found"
            return pr_analysis

        logger.info(f"Using runner: {runner.name} ({runner.language})")

        f2p_validated_prs = []
        f2p_results = []
        f2p_rejection_reasons = {}

        for pr in pr_analysis.accepted_prs:
            pr_number = pr.get("number")
            logger.info(f"F2P analysis for PR #{pr_number}...")
            if pr_number != self.pr_number and self.pr_number is not None:
                logger.info(
                    f"Skipping F2P analysis for PR #{pr_number} because it's not the target PR"
                )
                continue

            analyzer = F2PP2PAnalyzer(
                repo_path=Path(self.repo_path),
                runner=runner,
                test_timeout=self.f2p_timeout,
                language_hint=primary_language,
            )
            pr_files = [f["path"] for f in pr.get("files", {}).get("nodes", [])]
            result = analyzer.analyze(
                pr_number=pr_number,
                pr_title=pr.get("title", ""),
                base_sha=pr.get("baseRefOid", ""),
                head_sha=pr.get("headRefOid", ""),
                pr_files=pr_files,
                single_pr_mode=self.pr_number is not None,
            )

            f2p_results.append(result.to_dict())

            if result.success and result.has_valid_f2p and result.has_valid_p2p:
                logger.info(
                    f"PR #{pr_number}: ✅ VALID (F2P: {len(result.f2p_tests)}, P2P: {len(result.p2p_tests)})"
                )
                pr["f2p_result"] = result.to_dict()
                f2p_validated_prs.append(pr)
            else:
                reason = result.rejection_reason or result.error_code or "f2p_invalid"
                if reason == "empty_p2p":
                    message = "No P2P tests found"
                elif reason == "empty_f2p":
                    message = "No F2P tests found"
                else:
                    message = result.error or "Invalid F2P/P2P"
                logger.info(f"PR #{pr_number}: ❌ REJECTED - {reason}: {message}")
                f2p_rejection_reasons[reason] = f2p_rejection_reasons.get(reason, 0) + 1

        rejection_breakdown = pr_analysis.rejection_breakdown.copy()
        f2p_rejected_count = sum(f2p_rejection_reasons.values())
        if f2p_rejected_count > 0:
            total_rejected = pr_analysis.rejected + f2p_rejected_count
            for reason, count in f2p_rejection_reasons.items():
                rejection_breakdown[reason] = {
                    "count": count,
                    "percentage": round(
                        (count / total_rejected * 100) if total_rejected > 0 else 0, 1
                    ),
                }

        new_accepted = len(f2p_validated_prs)
        new_rejected = pr_analysis.rejected + f2p_rejected_count
        new_acceptance_rate = (
            new_accepted / pr_analysis.total_prs if pr_analysis.total_prs > 0 else 0.0
        )

        return PRRejectionStats(
            accepted_prs=f2p_validated_prs,
            total_prs=pr_analysis.total_prs,
            accepted=new_accepted,
            rejected=new_rejected,
            acceptance_rate=round(new_acceptance_rate, 3),
            rejection_breakdown=rejection_breakdown,
            f2p_results=f2p_results,
            avg_loc_per_pr=pr_analysis.avg_loc_per_pr,
            issue_linked_pr_ratio=pr_analysis.issue_linked_pr_ratio,
            feature_accepted_prs=pr_analysis.feature_accepted_prs,
            feature_accepted=pr_analysis.feature_accepted,
            feature_rejection_breakdown=pr_analysis.feature_rejection_breakdown,
        )

    def _run_pr_rubrics(
        self, pr_analysis: PRRejectionStats, language_config: dict
    ) -> PRRejectionStats:
        """LLM rubrics on accepted_prs only (trimmed six dimensions)."""
        if self.skip_pr_rubrics:
            pr_analysis.pr_rubrics = []
            return pr_analysis
        try:
            from quality_evaluator import QualityEvaluator, split_patch_by_test_files
        except ImportError:
            logger.warning("quality_evaluator module not found; skipping PR rubrics")
            pr_analysis.pr_rubrics = []
            return pr_analysis

        pr_analyzer = PRAnalyzer(
            platform_client=self.platform_client,
            language_config=language_config,
            repo_path=str(self.repo_path),
            min_test_files=self.min_test_files,
            max_non_test_files=self.max_non_test_files,
            min_code_changes=self.min_code_changes,
            start_date=self.start_date,
        )
        qe = QualityEvaluator()
        results: List[dict] = []

        for pr in pr_analysis.accepted_prs:
            pr_number = pr.get("number")
            if self.pr_number is not None and pr_number != self.pr_number:
                continue
            logger.info(
                "Running quality evaluator for %s/%s PR #%s ...",
                self.owner,
                self.repo_name,
                pr_number,
            )
            entry: dict = {
                "number": pr_number,
                "url": pr.get("url", ""),
            }
            base_sha = pr.get("baseRefOid", "") or ""
            head_sha = pr.get("headRefOid", "") or ""
            if not base_sha or not head_sha:
                entry["error"] = "missing base or head SHA"
                results.append(entry)
                continue

            full_patch = pr_analyzer._get_patch_from_git(
                base_sha, head_sha, pr_number=pr_number
            )
            if not full_patch:
                entry["error"] = "could not retrieve patch"
                results.append(entry)
                continue

            pr_files_nodes = pr.get("files", {}).get("nodes", [])
            files_changed = [f.get("path", "") for f in pr_files_nodes if f.get("path")]

            src_diff, test_diff = split_patch_by_test_files(
                full_patch,
                lambda path, cfg: is_test_file_path(path, cfg),
                language_config,
            )
            problem_statement = _problem_statement_for_pr(pr)
            commit_message = (pr.get("title") or "").strip()

            try:
                _passed, scores = qe.evaluate_candidate(
                    src_diff,
                    test_diff,
                    problem_statement=problem_statement or None,
                    hints="",
                    commit_message=commit_message,
                    files_changed=files_changed,
                )
            except Exception as e:
                logger.warning("PR rubrics failed for #%s: %s", pr_number, e)
                entry["error"] = str(e)
                results.append(entry)
                continue

            if scores is None:
                entry["error"] = qe.last_rejection_reason or "evaluation_failed"
                results.append(entry)
                continue

            entry["rubrics"] = scores.to_trimmed_rubrics_dict()
            results.append(entry)

        pr_analysis.pr_rubrics = results
        return pr_analysis


# Output functions
def print_report(report: AnalysisReport):
    """Print human-readable report to console."""
    print(f"\n{'=' * 60}")
    print("REPOSITORY EVALUATION REPORT")
    print(f"{'=' * 60}")
    print(f"Repository: {report.repo_full_name}")
    print(f"Language: {report.repo_metrics.primary_language}")

    # print(f"Overall Score: {report.overall_score}/100")
    # print(f"Recommendation: {report.recommendation}\n")
    print()

    print("--- Repository Metrics ---")
    print(f"Total files: {report.repo_metrics.total_files}")
    print(f"Source files: {report.repo_metrics.source_files}")
    print(f"Test files: {report.repo_metrics.test_files}")
    print(f"Total LoC: {report.repo_metrics.total_loc:,}")
    print(f"Source LoC: {report.repo_metrics.source_loc:,}")
    print(f"Test LoC: {report.repo_metrics.test_loc:,}")
    print(f"Open issues: {report.repo_metrics.open_issues:,}")
    print(f"Closed issues: {report.repo_metrics.closed_issues:,}")
    print(f"Total issues: {report.repo_metrics.total_issues:,}")
    print(f"CI/CD: {'✅' if report.repo_metrics.has_ci_cd else '❌'}")
    print(
        f"Test frameworks: {', '.join(report.repo_metrics.test_frameworks) if report.repo_metrics.test_frameworks else 'None'}"
    )
    if report.repo_metrics.total_commits:
        print(f"Total commits: {report.repo_metrics.total_commits:,}")
        print(
            f"Recent commits (6mo): {(report.repo_metrics.recent_commits_6mo or 0):,}"
        )
        print(
            f"Recent commits (12mo): {(report.repo_metrics.recent_commits_12mo or 0):,}"
        )
    if report.repo_metrics.median_commit_interval_hours is not None:
        print(
            f"Median commit interval (90d): {report.repo_metrics.median_commit_interval_hours:,.2f} hours"
        )
    print(f"First 5 commits LoC: {report.repo_metrics.first_5_commits_loc or []}")
    top10_loc_values = [
        c.get("loc", 0) for c in (report.repo_metrics.top_10_commits_by_loc or [])
    ]
    print(f"Top 10 commit LoC: {top10_loc_values}")
    if report.repo_metrics.commit_message_avg_len is not None:
        print(
            f"Commit message length stats (avg/median/var): "
            f"{report.repo_metrics.commit_message_avg_len:.2f}/"
            f"{(report.repo_metrics.commit_message_median_len or 0):.2f}/"
            f"{(report.repo_metrics.commit_message_variance or 0):.2f}"
        )
    if report.repo_metrics.avg_loc_per_commit is not None:
        print(
            f"Commit LoC stats (avg/median): "
            f"{report.repo_metrics.avg_loc_per_commit:.2f}/"
            f"{(report.repo_metrics.median_loc_per_commit or 0):.2f}"
        )
    if report.repo_metrics.branch_count is not None:
        print(f"Branch count: {report.repo_metrics.branch_count}")
    if report.repo_metrics.code_churn_rate is not None:
        print(f"Code churn rate: {report.repo_metrics.code_churn_rate:.4f}")
    if report.repo_metrics.comment_density is not None:
        print(f"Comment density: {report.repo_metrics.comment_density:.4f}")

    print(
        f"\nOpen-source likelihood: {report.repo_metrics.open_source_likelihood} ({report.repo_metrics.open_source_score}/100)"
    )
    if report.repo_metrics.open_source_signals:
        print(
            f"Open-source signals: {', '.join(report.repo_metrics.open_source_signals)}"
        )

    print(
        f"AI/vibe risk: {report.repo_metrics.ai_risk_level} ({report.repo_metrics.ai_risk_score}/100)"
    )
    if report.repo_metrics.ai_risk_signals:
        print(f"AI/vibe signals: {', '.join(report.repo_metrics.ai_risk_signals)}")

    # if report.repo_metrics.process_health_summary:
    #     summary = report.repo_metrics.process_health_summary
    #     print(
    #         f"\nRepository health checks: {summary.get('passed_count', 0)}/{summary.get('total_count', 0)} "
    #         f"passed ({summary.get('pass_rate', 0)*100:.1f}%)"
    #     )
    # if report.repo_metrics.process_health_checks:
    #     print("Repository health signals:")
    #     ordered_keys = sorted(report.repo_metrics.process_health_checks.keys())
    #     for key in ordered_keys:
    #         check = report.repo_metrics.process_health_checks.get(key, {})
    #         description = REPO_HEALTH_DESCRIPTIONS.get(key)
    #         label = f"{key} ({description})" if description else key
    #         print(f"  - {label}: {check.get('value')}")
    #         # if "passed" in check:
    #         #     passed = "✅" if check.get("passed") else "❌"
    #         #     print(f"  {passed} {label}: {check.get('value')}")
    #         # else:
    #         #     print(f"  - {label}: {check.get('value')}")

    print("\n--- PR Analysis ---")
    print(f"Total PRs Analyzed: {report.pr_analysis.total_prs}")
    print(f"Passed First Filters PRs: {len(report.pr_analysis.accepted_prs)}")
    for pr in report.pr_analysis.accepted_prs:
        f2p_info = ""
        if "f2p_result" in pr:
            f2p = pr["f2p_result"]
            f2p_info = (
                f" [F2P: {f2p.get('f2p_count', 0)}, P2P: {f2p.get('p2p_count', 0)}]"
            )
        print(f"  - {pr['title']} (#{pr['number']}){f2p_info}")
    print(
        f"Passed First Filters: {report.pr_analysis.accepted} ({report.pr_analysis.acceptance_rate * 100:.1f}%)"
    )
    print(
        f"Failed First Filters: {report.pr_analysis.rejected} ({(1 - report.pr_analysis.acceptance_rate) * 100:.1f}%)"
    )

    if report.pr_analysis.f2p_results:
        print("\n--- F2P Analysis Results ---")
        valid_f2p_count = sum(
            1 for r in report.pr_analysis.f2p_results if r.get("verdict") == "VALID"
        )
        total_f2p_tests = sum(
            r.get("f2p_count", 0) for r in report.pr_analysis.f2p_results
        )
        total_p2p_tests = sum(
            r.get("p2p_count", 0) for r in report.pr_analysis.f2p_results
        )
        total_analyzed = len(report.pr_analysis.f2p_results)
        valid_pct = (
            (valid_f2p_count / total_analyzed * 100) if total_analyzed > 0 else 0
        )
        print(f"PRs with valid F2P: {valid_f2p_count} ({valid_pct:.1f}%)")
        print(f"Total F2P tests found: {total_f2p_tests}")
        print(f"Total P2P tests found: {total_p2p_tests}")
    elif report.pr_analysis.f2p_skipped_reason:
        print("\n--- F2P Analysis ---")
        print(f"⚠️  Skipped: {report.pr_analysis.f2p_skipped_reason}")

    if report.pr_analysis.feature_accepted_prs:
        print("\n--- Feature PRs ---")
        print(f"Feature PRs: {report.pr_analysis.feature_accepted}")
        # for pr in report.pr_analysis.feature_accepted_prs:
        #     signals = ', '.join(pr.get('feature_signals', []))
        #     print(
        #         f"  - {pr['title']} (#{pr['number']}) [score={pr.get('feature_score', '?')}, {signals}]")
        # if report.pr_analysis.feature_rejection_breakdown:
        #     print(f"\nFeature Rejection Breakdown:")
        #     sorted_rejections = sorted(
        #         report.pr_analysis.feature_rejection_breakdown.items(),
        #         key=lambda x: x[1]['count'],
        #         reverse=True
        #     )
        #     for filter_name, stats in sorted_rejections:
        #         print(
        #             f" {filter_name}: {stats['count']} ({stats['percentage']}%)")


def to_json(report: AnalysisReport) -> dict:
    """Convert report to JSON-serializable dict."""
    accepted_prs_clean = []
    for pr in report.pr_analysis.accepted_prs:
        pr_data = {
            "number": pr.get("number"),
            "title": pr.get("title"),
            "url": pr.get("url"),
            "baseRefOid": pr.get("baseRefOid"),
            "headRefOid": pr.get("headRefOid"),
        }
        if "f2p_result" in pr:
            pr_data["f2p_result"] = pr["f2p_result"]
        accepted_prs_clean.append(pr_data)

    repo_metrics_json = asdict(report.repo_metrics)
    repo_metrics_out = {
        "repo_name": repo_metrics_json.get("repo_name"),
        "total_files": repo_metrics_json.get("total_files"),
        "open_issues": repo_metrics_json.get("open_issues"),
        "closed_issues": repo_metrics_json.get("closed_issues"),
        "total_issues": repo_metrics_json.get("total_issues"),
        "test_files": repo_metrics_json.get("test_files"),
        "source_files": repo_metrics_json.get("source_files"),
        "total_loc": repo_metrics_json.get("total_loc"),
        "source_loc": repo_metrics_json.get("source_loc"),
        "test_loc": repo_metrics_json.get("test_loc"),
        "languages": repo_metrics_json.get("languages"),
        "primary_language": repo_metrics_json.get("primary_language"),
        "has_ci_cd": repo_metrics_json.get("has_ci_cd"),
        "ci_files": repo_metrics_json.get("ci_files"),
        "test_frameworks": repo_metrics_json.get("test_frameworks"),
        "has_test_runner": repo_metrics_json.get("has_test_runner"),
        "total_commits": repo_metrics_json.get("total_commits"),
        "recent_commits_6mo": repo_metrics_json.get("recent_commits_6mo"),
        "recent_commits_12mo": repo_metrics_json.get("recent_commits_12mo"),
        "recent_commits_12m": repo_metrics_json.get("recent_commits_12mo"),
        "commits_referencing_issues": repo_metrics_json.get(
            "commits_referencing_issues"
        ),
        "repo_age_days": repo_metrics_json.get("repo_age_days"),
        "contributors_total": repo_metrics_json.get("contributors_total"),
        "commit_spread_ratio": repo_metrics_json.get("commit_spread_ratio"),
        "unique_commit_spread_days": repo_metrics_json.get("unique_commit_spread_days"),
        "distinct_commit_days": repo_metrics_json.get("distinct_commit_days"),
        "first_commit_loc": repo_metrics_json.get("first_commit_loc"),
        "single_commit_loc_share": repo_metrics_json.get("single_commit_loc_share"),
        "top10_commit_loc_share": repo_metrics_json.get("top10_commit_loc_share"),
        "commit_message_unique_ratio": repo_metrics_json.get(
            "commit_message_unique_ratio"
        ),
        "first_5_commits_loc": repo_metrics_json.get("first_5_commits_loc"),
        "top_10_commits_by_loc": repo_metrics_json.get("top_10_commits_by_loc"),
        "commit_message_avg_len": repo_metrics_json.get("commit_message_avg_len"),
        "commit_message_median_len": repo_metrics_json.get("commit_message_median_len"),
        "commit_message_variance": repo_metrics_json.get("commit_message_variance"),
        "avg_loc_per_commit": repo_metrics_json.get("avg_loc_per_commit"),
        "median_loc_per_commit": repo_metrics_json.get("median_loc_per_commit"),
        "pr_to_commit_ratio": repo_metrics_json.get("pr_to_commit_ratio"),
        "avg_loc_per_pr": repo_metrics_json.get("avg_loc_per_pr"),
        "pr_acceptance_rate": repo_metrics_json.get("pr_acceptance_rate"),
        "issue_linked_pr_ratio": repo_metrics_json.get("issue_linked_pr_ratio"),
        "branch_count": repo_metrics_json.get("branch_count"),
        "test_to_source_file_ratio": repo_metrics_json.get("test_to_source_file_ratio"),
        "test_loc_to_source_loc_ratio": repo_metrics_json.get(
            "test_loc_to_source_loc_ratio"
        ),
        "test_files_to_source_files_ratio": repo_metrics_json.get(
            "test_files_to_source_files_ratio"
        ),
        "code_churn_rate": repo_metrics_json.get("code_churn_rate"),
        "comment_density": repo_metrics_json.get("comment_density"),
        "readme_length_chars": repo_metrics_json.get("readme_length_chars"),
        "readme_has_badges": repo_metrics_json.get("readme_has_badges"),
        "readme_has_installation": repo_metrics_json.get("readme_has_installation"),
        "readme_has_usage": repo_metrics_json.get("readme_has_usage"),
        "median_commit_interval_hours": repo_metrics_json.get(
            "median_commit_interval_hours"
        ),
        "open_source_score": repo_metrics_json.get("open_source_score"),
        "open_source_likelihood": repo_metrics_json.get("open_source_likelihood"),
        "open_source_signals": repo_metrics_json.get("open_source_signals"),
        "ai_risk_score": repo_metrics_json.get("ai_risk_score"),
        "ai_risk_level": repo_metrics_json.get("ai_risk_level"),
        "ai_risk_signals": repo_metrics_json.get("ai_risk_signals"),
    }

    result = {
        "repo_name": report.repo_name,
        "repo_full_name": report.repo_full_name,
        # 'overall_score': report.overall_score,
        # 'recommendation': report.recommendation,
        "repo_metrics": repo_metrics_out,
        "pr_analysis": {
            "total_prs": report.pr_analysis.total_prs,
            "pass_first_filter": report.pr_analysis.accepted,
            "rejected": report.pr_analysis.rejected,
            "pass_first_filter_rate": report.pr_analysis.acceptance_rate,
            "pass_first_filter_prs": accepted_prs_clean,
            "avg_loc_per_pr": report.pr_analysis.avg_loc_per_pr,
            "issue_linked_pr_ratio": report.pr_analysis.issue_linked_pr_ratio,
            "pr_first_date": report.pr_analysis.pr_first_date,
            "pr_last_date": report.pr_analysis.pr_last_date,
            "pr_spread_days": report.pr_analysis.pr_spread_days,
            "pr_unique_dates_count": report.pr_analysis.pr_unique_dates_count,
            # 'rejection_breakdown': report.pr_analysis.rejection_breakdown,
        },
    }

    # Excluded from output format (kept in internal logic):
    # repo_metrics_out['test_file_ratio'] = repo_metrics_json.get('test_file_ratio')
    # repo_metrics_out['readiness_score'] = repo_metrics_json.get('readiness_score')
    # repo_metrics_out['test_coverage_percentage'] = repo_metrics_json.get('test_coverage_percentage')
    # repo_metrics_out['recommendation'] = repo_metrics_json.get('recommendation')
    # repo_metrics_out['strengths'] = repo_metrics_json.get('strengths')
    # repo_metrics_out['weaknesses'] = repo_metrics_json.get('weaknesses')
    # repo_metrics_out['commit_spread_days'] = repo_metrics_json.get('commit_spread_days')
    # repo_metrics_out['process_health_checks'] = repo_metrics_json.get('process_health_checks')
    # repo_metrics_out['process_health_summary'] = repo_metrics_json.get('process_health_summary')

    if report.pr_analysis.f2p_results:
        result["pr_analysis"]["f2p_results"] = report.pr_analysis.f2p_results
    elif report.pr_analysis.f2p_skipped_reason:
        result["pr_analysis"]["f2p_skipped_reason"] = (
            report.pr_analysis.f2p_skipped_reason
        )

    if report.pr_analysis.feature_accepted_prs:
        result["pr_analysis"]["feature_prs"] = {
            "pass_first_filter": report.pr_analysis.feature_accepted,
            "pass_first_filter_prs": [
                {
                    "number": pr.get("number"),
                    "title": pr.get("title"),
                    "url": pr.get("url"),
                    "baseRefOid": pr.get("baseRefOid"),
                    "headRefOid": pr.get("headRefOid"),
                    # 'feature_score': pr.get('feature_score'),
                    "feature_signals": pr.get("feature_signals"),
                }
                for pr in report.pr_analysis.feature_accepted_prs
            ],
            # 'rejection_breakdown': report.pr_analysis.feature_rejection_breakdown,
        }

    result["pr_rubrics"] = report.pr_analysis.pr_rubrics or []
    result.update(_pr_rubric_aggregate_counts(report.pr_analysis.pr_rubrics))

    return result


def write_json_dict_to_csv(data: dict, csv_path: Path) -> None:
    """Write one JSON report dict as a single-row CSV."""
    row = {}
    for key, value in data.items():
        if isinstance(value, dict):
            for sub_key, sub_val in value.items():
                col = f"{key}.{sub_key}"
                if key == "repo_metrics" and sub_key == "first_5_commits_loc":
                    values = sub_val if isinstance(sub_val, list) else []
                    for i in range(1, 6):
                        row[f"{sub_key}{i}"] = (
                            values[i - 1] if i <= len(values) else None
                        )
                    continue
                if key == "repo_metrics" and sub_key == "top_10_commits_by_loc":
                    values = sub_val if isinstance(sub_val, list) else []
                    loc_values = []
                    for item in values:
                        if isinstance(item, dict):
                            loc_values.append(item.get("loc"))
                        else:
                            loc_values.append(item)
                    for i in range(1, 11):
                        row[f"top_10_commits_loc{i}"] = (
                            loc_values[i - 1] if i <= len(loc_values) else None
                        )
                    continue
                if isinstance(sub_val, (dict, list)):
                    row[col] = json.dumps(sub_val, ensure_ascii=False)
                else:
                    row[col] = sub_val
        elif isinstance(value, list):
            row[key] = json.dumps(value, ensure_ascii=False)
        else:
            row[key] = value

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)


def clone_repo(
    repo_full_name: str, temp_dir: Path, token: str, platform: str = "github"
) -> Path:
    """Clone repository to temporary directory."""

    if platform == "bitbucket":
        repo_url = f"https://x-token-auth:{token}@bitbucket.org/{repo_full_name}.git"
    elif platform == "gitlab":
        repo_url = f"https://oauth2:{token}@gitlab.com/{repo_full_name}.git"
    else:  # default to github
        repo_url = f"https://{token}@github.com/{repo_full_name}.git"

    clone_path = temp_dir / repo_full_name.replace("/", "_")

    logger.info(f"Cloning {repo_full_name} to {clone_path}...")
    result = subprocess.run(
        # deep clone so we can get the total commits
        ["git", "clone", repo_url, str(clone_path)],
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to clone repository: {result.stderr}")

    logger.info("Successfully cloned repository")
    return clone_path


def parse_repo_name(repo_string: str) -> Tuple[str, str]:
    """Parse owner/repo-name format, handling platform prefixes."""
    repo_string = repo_string.strip()

    # Remove platform prefix if present
    if repo_string.startswith("bitbucket:"):
        repo_string = repo_string[10:]  # Remove 'bitbucket:'
    elif repo_string.startswith("github:"):
        repo_string = repo_string[7:]  # Remove 'github:'
    elif repo_string.startswith("gitlab:"):
        repo_string = repo_string[7:]  # Remove 'gitlab:'

    # Extract from URL if present
    if (
        "bitbucket" in repo_string
        or "github.com" in repo_string
        or "gitlab" in repo_string
    ):
        cleaned = re.sub(r"^https?://[^/]+/", "", repo_string).rstrip("/")
        cleaned = re.sub(r"\.git$", "", cleaned)
        if cleaned:
            parts = [p for p in cleaned.split("/") if p]
            if len(parts) >= 2:
                return "/".join(parts[:-1]), parts[-1]

    if "/" not in repo_string:
        raise ValueError(
            f"Invalid repo format: {repo_string}. Expected 'owner/repo-name'"
        )

    parts = [p for p in repo_string.split("/") if p]
    if len(parts) < 2:
        raise ValueError(
            f"Invalid repo format: {repo_string}. Expected 'owner/repo-name' or 'group/subgroup/repo'"
        )

    return "/".join(parts[:-1]), parts[-1]


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate repositories for training sample creation suitability"
    )
    parser.add_argument(
        "repo", help="Repository in format owner/repo-name (e.g., microsoft/vscode)"
    )
    parser.add_argument(
        "--repo-path",
        help="Path to local repository directory (if not provided, will auto-clone)",
        default=None,
    )
    parser.add_argument(
        "--github-token",
        help="GitHub token for API access (deprecated, use --token)",
        default=None,
    )
    parser.add_argument(
        "--token",
        help="API token for platform access (GitHub, Bitbucket, or GitLab)",
        default=None,
    )
    parser.add_argument(
        "--platform",
        choices=["auto", "github", "bitbucket", "gitlab"],
        default="auto",
        help="Platform to use (default: auto-detect from repo string)",
    )
    parser.add_argument(
        "--min-test-files",
        type=int,
        default=MIN_TEST_FILES,
        help=f"Minimum test files per PR (default: {MIN_TEST_FILES})",
    )
    parser.add_argument(
        "--max-non-test-files",
        type=int,
        default=MAX_NON_TEST_FILES,
        help=f"Maximum non-test files per PR (default: {MAX_NON_TEST_FILES})",
    )
    parser.add_argument(
        "--min-code-changes",
        type=int,
        default=MIN_PR_CODE_CHANGES,
        help=f"Minimum code changes per PR (default: {MIN_PR_CODE_CHANGES})",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--max-prs",
        type=int,
        default=None,
        help="Maximum number of PRs to evaluate (default: None)",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Start date for evaluating PRs (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--output", type=str, default=None, help="Output file (default: None)"
    )
    parser.add_argument(
        "--skip-f2p", action="store_true", help="Skip F2P/P2P test verification"
    )
    parser.add_argument(
        "--f2p-timeout",
        type=int,
        default=600,
        help="Timeout for F2P test execution per PR in seconds (default: 600)",
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        default=None,
        help="PR number to evaluate (default: None)",
    )
    parser.add_argument(
        "--skip-quality-checks",
        action="store_true",
        help="Skip vibecode, security, and production quality checks",
    )
    parser.add_argument(
        "--skip-quality-llm",
        action="store_true",
        help="Skip LLM analysis in quality checks (faster, static analysis only)",
    )
    parser.add_argument(
        "--skip-taxonomy", action="store_true", help="Skip xAI taxonomy classification"
    )
    parser.add_argument(
        "--skip-pr-rubrics",
        action="store_true",
        help="Skip LLM benchmark rubrics on accepted PRs (issue/patch/test clarity, FN/FP)",
    )
    parser.add_argument(
        "--taxonomy-model",
        type=str,
        default=os.environ.get("TAXONOMY_MODEL", "gpt-4o"),
        help="Model for taxonomy classification (default: gpt-4o or TAXONOMY_MODEL env)",
    )
    parser.add_argument(
        "--taxonomy-base-url",
        type=str,
        default=os.environ.get("TAXONOMY_BASE_URL", "https://api.openai.com/v1"),
        help="Base URL for taxonomy LLM API (default: OpenAI or TAXONOMY_BASE_URL env)",
    )
    parser.add_argument(
        "--taxonomy-concurrency",
        type=int,
        default=int(os.environ.get("LLM_CONCURRENCY", "4")),
        help="Max parallel taxonomy LLM calls per repo (default: 4 or LLM_CONCURRENCY env)",
    )

    args = parser.parse_args()

    _needs_openai = (
        (not args.skip_quality_checks and not args.skip_quality_llm)
        or not args.skip_taxonomy
        or not args.skip_pr_rubrics
    )
    if _needs_openai and not os.environ.get("OPENAI_API_KEY"):
        print(
            "Error: OPENAI_API_KEY is required but not set.\n"
            "\n"
            "Add it to your .env file:\n"
            "  OPENAI_API_KEY=sk-your-key-here\n"
            "\n"
            "Get a key at: https://platform.openai.com/api-keys\n"
            "\n",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.start_date:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    else:
        start_date = None

    # Detect platform
    platform = detect_platform(args.repo, args.platform)
    logger.info(f"Detected platform: {platform}")

    # Get token (prefer --token, fallback to --github-token for backward compatibility)
    token = args.token or args.github_token
    if not token:
        logger.warning("No token provided. API rate limits may apply.")

    # Parse repo name
    try:
        owner, repo_name = parse_repo_name(args.repo)
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    # Create platform client
    if platform == "bitbucket":
        platform_client = BitbucketClient(owner, repo_name, token)
    elif platform == "gitlab":
        platform_client = GitLabClient(owner, repo_name, token)
    else:
        platform_client = GitHubClient(owner, repo_name, token)

    repo_path = args.repo_path
    temp_dir = None
    should_cleanup = True

    if not repo_path:
        # Auto-clone to temp directory
        temp_dir = Path(tempfile.mkdtemp(prefix="repo_evaluator_"))
        try:
            # if not token:
            #     raise ValueError("Token is required for cloning repositories")
            repo_path = str(
                clone_repo(f"{owner}/{repo_name}", temp_dir, token, platform)
            )
        except Exception as e:
            logger.error(f"Failed to clone repository: {e}")
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            sys.exit(1)
    else:
        repo_path = str(Path(repo_path).resolve())
        if not Path(repo_path).exists():
            logger.error(f"Repository path does not exist: {repo_path}")
            sys.exit(1)

    # Run evaluation
    try:
        evaluator = RepoEvaluator(
            repo_path=repo_path,
            owner=owner,
            repo_name=repo_name,
            platform_client=platform_client,
            min_test_files=args.min_test_files,
            max_non_test_files=args.max_non_test_files,
            min_code_changes=args.min_code_changes,
            start_date=start_date,
            max_prs=args.max_prs,
            skip_f2p=args.skip_f2p,
            f2p_timeout=args.f2p_timeout,
            pr_number=args.pr_number,
            skip_pr_rubrics=args.skip_pr_rubrics,
        )

        report = evaluator.evaluate()
        report_json = to_json(report)

        if not args.skip_quality_checks:
            qc_results = run_all_quality_checks(
                owner=owner,
                repo=repo_name,
                token=token or "",
                skip_llm=args.skip_quality_llm,
                repo_path=repo_path,
            )
            report_json.update(qc_results)
        else:
            report_json.update(
                {
                    "vibe_coding_critical": "",
                    "vibe_coding_signals": "",
                    "security_check_critical": "",
                    "security_check_signals": "",
                    "production_quality_critical": "",
                    "production_quality_signals": "",
                }
            )

        if not args.skip_taxonomy:
            pr_analyzer = PRAnalyzer(
                platform_client=platform_client,
                language_config=load_language_config(),
                repo_path=repo_path,
                min_test_files=args.min_test_files,
                max_non_test_files=args.max_non_test_files,
                min_code_changes=args.min_code_changes,
                start_date=start_date,
            )

            def _taxonomy_patch(pr: dict) -> str | None:
                return pr_analyzer._get_patch_from_git(
                    pr.get("baseRefOid", "") or "",
                    pr.get("headRefOid", "") or "",
                    pr_number=pr.get("number"),
                )

            pr_taxonomy = run_taxonomy_for_accepted_prs(
                accepted_prs=report.pr_analysis.accepted_prs,
                owner=owner,
                repo=repo_name,
                primary_language=report.repo_metrics.primary_language or "",
                get_patch=_taxonomy_patch,
                model=args.taxonomy_model,
                base_url=args.taxonomy_base_url,
                pr_number=args.pr_number,
                concurrency=args.taxonomy_concurrency,
            )
            if pr_taxonomy:
                report_json["pr_taxonomy"] = pr_taxonomy

        # Output
        if args.json:
            output = json.dumps(report_json, indent=2)
            if args.output:
                Path(args.output).write_text(output)
                print(f"Results saved to {args.output}")
                output_path = Path(args.output)
                csv_path = output_path.parent / f"{repo_name}.csv"
                write_json_dict_to_csv(report_json, csv_path)
                print(f"CSV saved to {csv_path}")
            else:
                print(output)
                output_dir = Path(
                    os.getcwd()
                    + f"/output_repos_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                )
                os.makedirs(output_dir, exist_ok=True)
                json_path = output_dir / f"{args.repo.replace('/', '__')}.json"
                csv_path = output_dir / f"{repo_name}.csv"
                with open(json_path, "w") as f:
                    f.write(output)
                write_json_dict_to_csv(report_json, csv_path)
        else:
            print_report(report)
            output = json.dumps(report_json, indent=2)
            output_dir = Path(os.getcwd() + "/output")
            os.makedirs(output_dir, exist_ok=True)
            json_path = output_dir / f"{args.repo.replace('/', '__')}.json"
            csv_path = output_dir / f"{repo_name}.csv"
            with open(json_path, "w") as f:
                f.write(output)
            write_json_dict_to_csv(report_json, csv_path)

    except Exception as e:
        logger.error(f"Error evaluating repository: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        if should_cleanup and temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
