"""Stage E13: CI/CD guardrails collector (Programmatic, repo-level)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from eval_kit.enterprise_signals.base import RepoCollector, RepoContext

_CI_FILE_GLOBS = [
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    "Jenkinsfile",
    ".circleci/config.yml",
    ".circleci/config.yaml",
    ".travis.yml",
    ".travis.yaml",
    "azure-pipelines.yml",
    "azure-pipelines.yaml",
    ".buildkite/pipeline.yml",
    ".buildkite/pipeline.yaml",
    "bitbucket-pipelines.yml",
    "bitbucket-pipelines.yaml",
    "Makefile",
    ".drone.yml",
    ".drone.yaml",
    "cloudbuild.yaml",
    "cloudbuild.yml",
    "appveyor.yml",
    "appveyor.yaml",
    "tox.ini",
    "circle.yml",
]

# Patterns in CI file content that indicate guardrail features
_FEATURE_PATTERNS = {
    "automated_tests": re.compile(
        r"\b(pytest|jest|mocha|rspec|go test|cargo test|mvn test|gradle test|unittest|vitest|phpunit)\b",
        re.IGNORECASE,
    ),
    "linting": re.compile(
        r"\b(ruff|flake8|pylint|eslint|tslint|prettier|golangci|rubocop|stylelint|shellcheck|hadolint|mypy|pyright)\b",
        re.IGNORECASE,
    ),
    "security_scan": re.compile(
        r"\b(bandit|semgrep|snyk|trivy|grype|codeql|sonarqube|sonarcloud|dependabot|gitleaks|trufflehog|checkov)\b",
        re.IGNORECASE,
    ),
    "code_coverage": re.compile(
        r"\b(coverage|codecov|coveralls|istanbul|nyc|lcov|gcov)\b", re.IGNORECASE
    ),
    "deployment": re.compile(
        r"\b(deploy|helm|kubectl|terraform|pulumi|serverless|release|publish|push.*image|docker.*push)\b",
        re.IGNORECASE,
    ),
    "containerization": re.compile(
        r"\b(docker build|docker push|kaniko|buildx|podman build)\b", re.IGNORECASE
    ),
    "notifications": re.compile(
        r"\b(slack|email|pagerduty|opsgenie|notify|webhook)\b", re.IGNORECASE
    ),
    "branch_protection": re.compile(
        r"\b(required_status_checks|branch-protection|protected.branch|required.review)\b",
        re.IGNORECASE,
    ),
}


def _collect_ci_files(root: Path) -> List[str]:
    found = []
    for glob in _CI_FILE_GLOBS:
        for p in root.glob(glob):
            rel = str(p.relative_to(root))
            found.append(rel)
    return sorted(set(found))


def _detect_features(ci_files: List[str], root: Path) -> List[str]:
    combined_content = ""
    for rel in ci_files:
        p = root / rel
        try:
            combined_content += p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            pass
    detected = []
    for feature, pat in _FEATURE_PATTERNS.items():
        if pat.search(combined_content):
            detected.append(feature)
    return sorted(detected)


class CicdGuardrailsCollector(RepoCollector):
    name = "cicd_guardrails"

    def collect(self, repo: RepoContext) -> Dict[str, Any]:
        ci_files = _collect_ci_files(repo.repo_path)
        detected_features = (
            _detect_features(ci_files, repo.repo_path) if ci_files else []
        )
        return {
            "has_cicd_guardrails": bool(ci_files),
            "ci_files": ci_files,
            "detected_features": detected_features,
        }
