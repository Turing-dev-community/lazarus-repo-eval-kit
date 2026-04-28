"""Tests for Stage E2: Domain complexity collector."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.enterprise_domain import (
    EnterpriseDomainCollector,
    EnterpriseDomainOutput,
)
from eval_kit.enterprise_signals.framework import collect_for_pr
from eval_kit.enterprise_signals.registry import reset_collectors

import pytest


def _make_pr_ctx(**kwargs) -> PRContext:
    defaults = dict(
        number=7,
        title="feat: add SWIFT payment integration",
        body="Implements SWIFT MT103 message parsing for cross-border payments.",
        issue_title="Support cross-border SWIFT payments",
        issue_body=(
            "We need to integrate with the SWIFT network to enable cross-border "
            "wire transfers. This involves MT103 message parsing, BIC validation, "
            "and PCI-DSS compliant storage of transaction records."
        ),
        commit_messages=[
            "Add SWIFT MT103 parser",
            "Validate BIC codes against SWIFT registry",
        ],
        changed_files=[
            "src/payments/swift_parser.py",
            "src/payments/bic_validator.py",
        ],
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


def test_enterprise_domain_collector_emits_true_with_evidence_when_llm_says_yes():
    mock_output = EnterpriseDomainOutput(
        has_enterprise_domain=True,
        matched_domains=["fintech"],
        source="issue_body",
        snippet="MT103 message parsing, BIC validation, and PCI-DSS compliant storage",
    )
    with patch(
        "eval_kit.enterprise_signals.collectors.enterprise_domain.call_llm",
        return_value=mock_output,
    ):
        collector = EnterpriseDomainCollector()
        result = collector.collect(_make_pr_ctx())

    assert result["has_enterprise_domain"] is True
    assert result["matched_domains"] == ["fintech"]
    assert result["evidence"]["source"] == "issue_body"
    assert "MT103" in result["evidence"]["snippet"]


def test_enterprise_domain_collector_emits_false_when_llm_says_no():
    mock_output = EnterpriseDomainOutput(
        has_enterprise_domain=False,
        matched_domains=[],
        source="none",
        snippet="",
    )
    with patch(
        "eval_kit.enterprise_signals.collectors.enterprise_domain.call_llm",
        return_value=mock_output,
    ):
        collector = EnterpriseDomainCollector()
        result = collector.collect(_make_pr_ctx())

    assert result["has_enterprise_domain"] is False
    assert result["matched_domains"] == []
    assert result["evidence"]["source"] == "none"
    assert result["evidence"]["snippet"] == ""


def test_enterprise_domain_collector_records_error_on_llm_exception():
    with patch(
        "eval_kit.enterprise_signals.collectors.enterprise_domain.call_llm",
        side_effect=RuntimeError("LLM timeout"),
    ):
        collector = EnterpriseDomainCollector()
        result = collect_for_pr(_make_pr_ctx(), [collector])

    assert "error" in result["enterprise_domain"]
    assert "LLM timeout" in result["enterprise_domain"]["error"]


def test_enterprise_domain_collector_skipped_when_skip_quality_llm():
    collector = EnterpriseDomainCollector(skip_llm=True)
    result = collector.collect(_make_pr_ctx())
    assert result == {"skipped": True}


def test_enterprise_domain_collector_passes_file_paths_to_llm():
    """Verify that changed_files are forwarded to the LLM prompt."""
    captured_messages = []

    def _capture_call_llm(messages, **kwargs):
        captured_messages.extend(messages)
        return EnterpriseDomainOutput(
            has_enterprise_domain=False,
            matched_domains=[],
            source="none",
            snippet="",
        )

    with patch(
        "eval_kit.enterprise_signals.collectors.enterprise_domain.call_llm",
        side_effect=_capture_call_llm,
    ):
        collector = EnterpriseDomainCollector()
        collector.collect(_make_pr_ctx())

    user_content = captured_messages[1]["content"]
    assert "swift_parser.py" in user_content
    assert "bic_validator.py" in user_content


def test_enterprise_domain_collector_multiple_matched_domains():
    mock_output = EnterpriseDomainOutput(
        has_enterprise_domain=True,
        matched_domains=["healthcare", "government / public-sector"],
        source="pr_body",
        snippet="HIPAA-compliant PHI export for federal agency reporting",
    )
    with patch(
        "eval_kit.enterprise_signals.collectors.enterprise_domain.call_llm",
        return_value=mock_output,
    ):
        collector = EnterpriseDomainCollector()
        result = collector.collect(_make_pr_ctx())

    assert result["has_enterprise_domain"] is True
    assert "healthcare" in result["matched_domains"]
    assert "government / public-sector" in result["matched_domains"]
