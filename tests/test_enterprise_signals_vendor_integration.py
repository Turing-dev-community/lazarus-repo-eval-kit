"""Tests for Stage E12: Vendor integration collector."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.vendor_integration import (
    VendorIntegrationCollector,
    VendorIntegrationOutput,
)
from eval_kit.enterprise_signals.framework import collect_for_pr
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_pr_ctx(**kwargs) -> PRContext:
    defaults = dict(
        number=12,
        title="feat: add Twilio SMS integration",
        body="Wraps the Twilio REST API for sending SMS notifications.",
        issue_title=None,
        issue_body=None,
        commit_messages=["Add Twilio SMS client wrapper"],
        changed_files=["src/integrations/twilio_client.py"],
        diff="+import twilio\n+client = twilio.rest.Client(account_sid, auth_token)\n",
        repo_path=Path("/tmp/repo"),
        primary_language="Python",
    )
    defaults.update(kwargs)
    return PRContext(**defaults)


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    reset_collectors()


def test_vendor_integration_emits_true_when_llm_says_yes():
    mock_output = VendorIntegrationOutput(
        has_vendor_integration=True,
        evidence=["twilio.rest.Client(account_sid, auth_token)"],
    )
    with patch(
        "eval_kit.enterprise_signals.collectors.vendor_integration.call_llm",
        return_value=mock_output,
    ):
        result = VendorIntegrationCollector().collect(_make_pr_ctx())

    assert result["has_vendor_integration"] is True
    assert len(result["evidence"]) > 0


def test_vendor_integration_emits_false_when_llm_says_no():
    mock_output = VendorIntegrationOutput(has_vendor_integration=False, evidence=[])
    with patch(
        "eval_kit.enterprise_signals.collectors.vendor_integration.call_llm",
        return_value=mock_output,
    ):
        result = VendorIntegrationCollector().collect(_make_pr_ctx())

    assert result["has_vendor_integration"] is False


def test_vendor_integration_records_error_on_llm_exception():
    with patch(
        "eval_kit.enterprise_signals.collectors.vendor_integration.call_llm",
        side_effect=RuntimeError("model overloaded"),
    ):
        result = collect_for_pr(_make_pr_ctx(), [VendorIntegrationCollector()])

    assert "error" in result["vendor_integration"]


def test_vendor_integration_skipped_when_skip_llm():
    result = VendorIntegrationCollector(skip_llm=True).collect(_make_pr_ctx())
    assert result == {"skipped": True}
