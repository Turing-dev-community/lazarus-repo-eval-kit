"""Tests for Stage E8: Monorepo cross-package collector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.cross_package import (
    CrossPackageCollector,
)
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_pr_ctx(repo_path: Path, changed_files) -> PRContext:
    return PRContext(
        number=8,
        title="feat: cross-package change",
        body="",
        issue_title=None,
        issue_body=None,
        commit_messages=[],
        changed_files=changed_files,
        diff=None,
        repo_path=repo_path,
        primary_language="TypeScript",
    )


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    reset_collectors()


def test_cross_package_detected_with_lerna(tmp_path):
    lerna = {"packages": ["packages/*"]}
    (tmp_path / "lerna.json").write_text(json.dumps(lerna))
    result = CrossPackageCollector().collect(
        _make_pr_ctx(
            tmp_path, ["packages/api/src/index.ts", "packages/web/src/App.tsx"]
        )
    )
    assert result["is_cross_package_pr"] is True
    assert len(result["packages_touched"]) == 2


def test_single_package_not_cross(tmp_path):
    lerna = {"packages": ["packages/*"]}
    (tmp_path / "lerna.json").write_text(json.dumps(lerna))
    result = CrossPackageCollector().collect(
        _make_pr_ctx(tmp_path, ["packages/api/src/a.ts", "packages/api/src/b.ts"])
    )
    assert result["is_cross_package_pr"] is False
    assert result["packages_touched"] == ["packages/api"]


def test_non_monorepo_returns_false(tmp_path):
    result = CrossPackageCollector().collect(
        _make_pr_ctx(tmp_path, ["src/a.py", "src/b.py"])
    )
    assert result["is_cross_package_pr"] is False
    assert result["packages_touched"] == []


def test_npm_workspaces_detected(tmp_path):
    pkg = {"name": "root", "workspaces": ["apps/*", "packages/*"]}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    result = CrossPackageCollector().collect(
        _make_pr_ctx(tmp_path, ["apps/dashboard/index.ts", "packages/utils/index.ts"])
    )
    assert result["is_cross_package_pr"] is True
