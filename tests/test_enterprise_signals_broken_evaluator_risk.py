"""Tests for Stage E10: Broken evaluator risk collector."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.broken_evaluator_risk import (
    BrokenEvaluatorOutput,
    BrokenEvaluatorRiskCollector,
)
from eval_kit.enterprise_signals.framework import collect_for_pr
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_pr_ctx(**kwargs) -> PRContext:
    defaults = dict(
        number=10,
        title="revert: Revert 'feat: add caching'",
        body="This reverts commit abc123.",
        issue_title=None,
        issue_body=None,
        commit_messages=["Revert feat: add caching"],
        changed_files=["src/cache.py"],
        diff="-import redis\n-cache = redis.Redis()\n",
        repo_path=Path("/tmp/repo"),
        primary_language="Python",
    )
    defaults.update(kwargs)
    return PRContext(**defaults)


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    reset_collectors()


def test_broken_evaluator_emits_true_when_llm_says_yes():
    mock_output = BrokenEvaluatorOutput(
        has_broken_evaluator_risk=True,
        evidence=["This reverts commit abc123 — pure deletion, rubric may penalise."],
    )
    with patch(
        "eval_kit.enterprise_signals.collectors.broken_evaluator_risk.call_llm",
        return_value=mock_output,
    ):
        result = BrokenEvaluatorRiskCollector().collect(_make_pr_ctx())

    assert result["has_broken_evaluator_risk"] is True
    assert len(result["evidence"]) > 0


def test_broken_evaluator_emits_false_when_llm_says_no():
    mock_output = BrokenEvaluatorOutput(has_broken_evaluator_risk=False, evidence=[])
    with patch(
        "eval_kit.enterprise_signals.collectors.broken_evaluator_risk.call_llm",
        return_value=mock_output,
    ):
        result = BrokenEvaluatorRiskCollector().collect(_make_pr_ctx())

    assert result["has_broken_evaluator_risk"] is False
    assert result["evidence"] == []


def test_broken_evaluator_records_error_on_llm_exception():
    with patch(
        "eval_kit.enterprise_signals.collectors.broken_evaluator_risk.call_llm",
        side_effect=RuntimeError("LLM error"),
    ):
        result = collect_for_pr(_make_pr_ctx(), [BrokenEvaluatorRiskCollector()])

    assert "error" in result["broken_evaluator_risk"]


def test_broken_evaluator_skipped_when_skip_llm():
    result = BrokenEvaluatorRiskCollector(skip_llm=True).collect(_make_pr_ctx())
    assert result == {"skipped": True}
