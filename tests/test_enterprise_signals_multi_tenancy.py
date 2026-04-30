"""Tests for Stage E6: Multi-tenancy & permission logic collector."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.multi_tenancy import (
    MultiTenancyCollector,
    MultiTenancyOutput,
)
from eval_kit.enterprise_signals.framework import collect_for_pr
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_pr_ctx(**kwargs) -> PRContext:
    defaults = dict(
        number=6,
        title="feat: add tenant scoping to queries",
        body="Adds tenant_id filter to all DB queries.",
        issue_title=None,
        issue_body=None,
        commit_messages=["Scope queries by tenant_id"],
        changed_files=["src/db/queries.py"],
        diff="+results = db.query(Model).filter_by(tenant_id=current_tenant.id)\n",
        repo_path=Path("/tmp/repo"),
        primary_language="Python",
    )
    defaults.update(kwargs)
    return PRContext(**defaults)


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    reset_collectors()


def test_multi_tenancy_emits_true_when_llm_says_yes():
    mock_output = MultiTenancyOutput(
        has_multi_tenancy_logic=True,
        evidence=["filter_by(tenant_id=current_tenant.id)"],
    )
    with patch(
        "eval_kit.enterprise_signals.collectors.multi_tenancy.call_llm",
        return_value=mock_output,
    ):
        result = MultiTenancyCollector().collect(_make_pr_ctx())

    assert result["has_multi_tenancy_logic"] is True
    assert len(result["evidence"]) > 0


def test_multi_tenancy_emits_false_when_llm_says_no():
    mock_output = MultiTenancyOutput(has_multi_tenancy_logic=False, evidence=[])
    with patch(
        "eval_kit.enterprise_signals.collectors.multi_tenancy.call_llm",
        return_value=mock_output,
    ):
        result = MultiTenancyCollector().collect(_make_pr_ctx())

    assert result["has_multi_tenancy_logic"] is False
    assert result["evidence"] == []


def test_multi_tenancy_records_error_on_llm_exception():
    with patch(
        "eval_kit.enterprise_signals.collectors.multi_tenancy.call_llm",
        side_effect=RuntimeError("timeout"),
    ):
        result = collect_for_pr(_make_pr_ctx(), [MultiTenancyCollector()])

    assert "error" in result["multi_tenancy"]
    assert "timeout" in result["multi_tenancy"]["error"]


def test_multi_tenancy_skipped_when_skip_llm():
    result = MultiTenancyCollector(skip_llm=True).collect(_make_pr_ctx())
    assert result == {"skipped": True}
