"""Tests for Stage E15: Feature flagging collector."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.feature_flags import FeatureFlagsCollector
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_pr_ctx(**kwargs) -> PRContext:
    defaults = dict(
        number=15,
        title="feat: add feature flag",
        body="",
        issue_title=None,
        issue_body=None,
        commit_messages=[],
        changed_files=[],
        diff=None,
        repo_path=Path("/tmp/repo"),
        primary_language="Python",
    )
    defaults.update(kwargs)
    return PRContext(**defaults)


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    reset_collectors()


def test_launchdarkly_detected():
    diff = "+import ldclient\n+client = ldclient.get()\n"
    result = FeatureFlagsCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_feature_flags"] is True
    assert "launchdarkly" in result["matched_sdks"]


def test_unleash_detected():
    diff = "+from unleash import UnleashClient\n"
    result = FeatureFlagsCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_feature_flags"] is True
    assert "unleash" in result["matched_sdks"]


def test_generic_feature_flag_detected():
    diff = "+if feature_flag('new_checkout'):\n+    do_new_thing()\n"
    result = FeatureFlagsCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_feature_flags"] is True
    assert "generic_feature_flag" in result["matched_sdks"]


def test_removed_lines_not_counted():
    diff = "-import ldclient\n+import os\n"
    result = FeatureFlagsCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_feature_flags"] is False


def test_no_diff_returns_false():
    result = FeatureFlagsCollector().collect(_make_pr_ctx(diff=None))
    assert result["has_feature_flags"] is False
    assert result["matched_sdks"] == []
