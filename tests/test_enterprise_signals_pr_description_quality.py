"""Tests for Stage E14: PR description quality collector."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.pr_description_quality import (
    PrDescriptionQualityCollector,
)
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_pr_ctx(body: str = "") -> PRContext:
    return PRContext(
        number=14,
        title="feat: something",
        body=body,
        issue_title=None,
        issue_body=None,
        commit_messages=[],
        changed_files=[],
        diff=None,
        repo_path=Path("/tmp/repo"),
        primary_language="Python",
    )


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    reset_collectors()


def test_empty_body_lowest_score():
    result = PrDescriptionQualityCollector().collect(_make_pr_ctx(""))
    assert result["pr_description_quality_score"] == 0.0
    assert result["word_count"] == 0
    assert result["has_links"] is False
    assert result["has_headers"] is False
    assert result["link_count"] == 0


def test_body_with_links_raises_score():
    body = "Fixes issue at https://github.com/org/repo/issues/5"
    result = PrDescriptionQualityCollector().collect(_make_pr_ctx(body))
    assert result["has_links"] is True
    assert result["link_count"] == 1
    assert result["pr_description_quality_score"] > 0


def test_body_with_markdown_headers():
    body = "## Summary\nThis PR does X.\n## Testing\nRan unit tests."
    result = PrDescriptionQualityCollector().collect(_make_pr_ctx(body))
    assert result["has_headers"] is True


def test_word_count_saturates_at_200():
    body = " ".join(["word"] * 300)
    result = PrDescriptionQualityCollector().collect(_make_pr_ctx(body))
    assert result["word_count"] == 300
    # word component is capped so overall score <= 0.5 from words alone
    # (links=False, headers=False) => score = 0.5 * min(300/200, 1.0) = 0.5
    assert result["pr_description_quality_score"] == 0.5


def test_perfect_score_body():
    words = " ".join(["word"] * 200)
    body = f"## Summary\n{words}\nhttps://github.com/org/repo/issues/1"
    result = PrDescriptionQualityCollector().collect(_make_pr_ctx(body))
    assert result["pr_description_quality_score"] == 1.0
