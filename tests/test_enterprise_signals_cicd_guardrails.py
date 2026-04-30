"""Tests for Stage E13: CI/CD guardrails collector."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_kit.enterprise_signals.base import RepoContext
from eval_kit.enterprise_signals.collectors.cicd_guardrails import (
    CicdGuardrailsCollector,
)
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_repo_ctx(repo_path: Path) -> RepoContext:
    return RepoContext(
        repo_path=repo_path,
        owner="fake",
        repo_name="test-repo",
        primary_language="Python",
    )


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    reset_collectors()


def test_github_actions_detected(tmp_path):
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "ci.yml").write_text("steps:\n  - run: pytest\n")
    result = CicdGuardrailsCollector().collect(_make_repo_ctx(tmp_path))
    assert result["has_cicd_guardrails"] is True
    assert ".github/workflows/ci.yml" in result["ci_files"]
    assert "automated_tests" in result["detected_features"]


def test_gitlab_ci_detected(tmp_path):
    (tmp_path / ".gitlab-ci.yml").write_text("test:\n  script: pytest\n")
    result = CicdGuardrailsCollector().collect(_make_repo_ctx(tmp_path))
    assert result["has_cicd_guardrails"] is True


def test_linting_feature_detected(tmp_path):
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "lint.yml").write_text("steps:\n  - run: ruff check .\n")
    result = CicdGuardrailsCollector().collect(_make_repo_ctx(tmp_path))
    assert "linting" in result["detected_features"]


def test_no_ci_files_returns_false(tmp_path):
    result = CicdGuardrailsCollector().collect(_make_repo_ctx(tmp_path))
    assert result["has_cicd_guardrails"] is False
    assert result["ci_files"] == []
    assert result["detected_features"] == []
