"""Tests for Stage E16: Resiliency patterns collector."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.resiliency_patterns import (
    ResiliencyPatternsCollector,
)
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_pr_ctx(**kwargs) -> PRContext:
    defaults = dict(
        number=16,
        title="feat: add retry logic",
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


def test_tenacity_detected():
    diff = "+from tenacity import retry, stop_after_attempt\n"
    result = ResiliencyPatternsCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_resiliency_patterns"] is True
    assert "tenacity" in result["matched_libraries"]


def test_polly_detected():
    diff = "+Policy.Handle<HttpRequestException>().WaitAndRetry(3, ...);\n"
    result = ResiliencyPatternsCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_resiliency_patterns"] is True
    assert "polly" in result["matched_libraries"]


def test_circuit_breaker_detected():
    diff = "+from circuitbreaker import circuit\n"
    result = ResiliencyPatternsCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_resiliency_patterns"] is True
    assert "circuitbreaker" in result["matched_libraries"]


def test_removed_lines_not_counted():
    diff = "-from tenacity import retry\n+pass\n"
    result = ResiliencyPatternsCollector().collect(_make_pr_ctx(diff=diff))
    assert result["has_resiliency_patterns"] is False


def test_no_diff_returns_false():
    result = ResiliencyPatternsCollector().collect(_make_pr_ctx(diff=None))
    assert result["has_resiliency_patterns"] is False
    assert result["matched_libraries"] == []
