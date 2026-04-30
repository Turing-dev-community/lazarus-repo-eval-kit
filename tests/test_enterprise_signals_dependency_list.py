"""Tests for Stage E4: Dependency list collector."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from eval_kit.enterprise_signals.base import RepoContext
from eval_kit.enterprise_signals.collectors.dependency_list import (
    DependencyListCollector,
)
from eval_kit.enterprise_signals.registry import reset_collectors


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    reset_collectors()


def _make_repo_ctx(repo_path: Path) -> RepoContext:
    return RepoContext(
        repo_path=repo_path,
        owner="fake",
        repo_name="test-repo",
        primary_language="Python",
    )


def test_dependency_list_from_package_json(tmp_path):
    pkg = {
        "name": "my-app",
        "dependencies": {"react": "^18.2.0", "axios": "1.4.0"},
        "devDependencies": {"jest": "^29.0.0"},
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    result = DependencyListCollector().collect(_make_repo_ctx(tmp_path))
    deps = {d["name"]: d for d in result["dependencies_list"]}
    assert "react" in deps
    assert deps["react"]["direct_dependency"] is True
    assert "18" in deps["react"]["version"]
    assert "jest" in deps
    assert deps["jest"]["direct_dependency"] is False


def test_dependency_list_from_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        textwrap.dedent(
            """\
            requests>=2.28.0
            flask==2.3.2
            # comment
            -r other.txt
            """
        )
    )
    result = DependencyListCollector().collect(_make_repo_ctx(tmp_path))
    names = {d["name"] for d in result["dependencies_list"]}
    assert "requests" in names
    assert "flask" in names


def test_dependency_list_empty_when_no_manifests(tmp_path):
    result = DependencyListCollector().collect(_make_repo_ctx(tmp_path))
    assert result["dependencies_list"] == []


def test_dependency_list_all_requirements_direct(tmp_path):
    (tmp_path / "requirements.txt").write_text("boto3==1.26.0\n")
    result = DependencyListCollector().collect(_make_repo_ctx(tmp_path))
    dep = result["dependencies_list"][0]
    assert dep["direct_dependency"] is True
    assert dep["name"] == "boto3"
    assert "1.26.0" in dep["version"]
