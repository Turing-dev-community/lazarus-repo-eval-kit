"""Tests for Stage E9: Environment sensitivity collector."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.environment_sensitivity import (
    EnvironmentSensitivityCollector,
)
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_pr_ctx(**kwargs) -> PRContext:
    defaults = dict(
        number=9,
        title="fix: flaky test",
        body="",
        issue_title=None,
        issue_body=None,
        commit_messages=[],
        changed_files=["tests/test_utils.py"],
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


def test_time_sleep_detected():
    diff = "+    time.sleep(0.5)\n"
    result = EnvironmentSensitivityCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_environment_sensitivity"] is True
    assert any("time.sleep" in p for p in result["matched_patterns"])


def test_datetime_now_detected():
    diff = "+    ts = datetime.now()\n"
    result = EnvironmentSensitivityCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_environment_sensitivity"] is True


def test_freeze_time_detected():
    diff = "+@freeze_time('2024-01-01')\n"
    result = EnvironmentSensitivityCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_environment_sensitivity"] is True


def test_removed_lines_not_counted():
    diff = "-    time.sleep(1)\n+    pass\n"
    result = EnvironmentSensitivityCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_environment_sensitivity"] is False


def test_no_sensitivity_in_clean_diff():
    diff = "+def add(a, b):\n+    return a + b\n"
    result = EnvironmentSensitivityCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_environment_sensitivity"] is False


def test_no_diff_returns_false():
    result = EnvironmentSensitivityCollector().collect(_make_pr_ctx(diff=None))
    assert result["has_environment_sensitivity"] is False
    assert result["matched_patterns"] == []


def test_pytest_mark_order_detected():
    diff = "+@pytest.mark.order(1)\n"
    result = EnvironmentSensitivityCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_environment_sensitivity"] is True
