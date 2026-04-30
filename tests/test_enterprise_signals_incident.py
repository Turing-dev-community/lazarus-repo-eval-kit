"""Tests for Stage E1: Production incident signal collector."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.incident import (
    IncidentSignalCollector,
    IncidentSignalOutput,
)
from eval_kit.enterprise_signals.framework import collect_for_pr
from eval_kit.enterprise_signals.registry import register_pr_collector, reset_collectors

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PLATFORM_DIR = FIXTURES_DIR / "platform"
REPOS_DIR = FIXTURES_DIR / "repos"
SNAPSHOTS_DIR = FIXTURES_DIR / "snapshots"


def _make_pr_ctx(**kwargs) -> PRContext:
    defaults = dict(
        number=5,
        title="feat: extend calculator with division and add statistics module",
        body="Adds divide/subtract to calculator, new statistics.py and constants.py.",
        issue_title=None,
        issue_body=(
            "The calculator module needs to support division and subtraction operations. "
            "We should also add a statistics module with mean and median functions, and a "
            "constants module for shared values. All new functions must have corresponding tests."
        ),
        commit_messages=[
            "Add division and subtraction to calculator",
            "Add statistics module",
        ],
        changed_files=["src/calculator.py", "src/statistics.py"],
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


def test_incident_collector_emits_true_with_evidence_when_llm_says_yes():
    mock_output = IncidentSignalOutput(
        has_incident_signal=True,
        keywords_matched=["outage", "p95"],
        source="issue_body",
        snippet="P95 latency spiked...",
    )
    with patch(
        "eval_kit.enterprise_signals.collectors.incident.call_llm",
        return_value=mock_output,
    ):
        collector = IncidentSignalCollector()
        result = collector.collect(_make_pr_ctx())

    assert result["has_incident_signal"] is True
    assert result["evidence"]["keywords_matched"] == ["outage", "p95"]
    assert result["evidence"]["source"] == "issue_body"
    assert result["evidence"]["snippet"] == "P95 latency spiked..."


def test_incident_collector_emits_false_when_llm_says_no():
    mock_output = IncidentSignalOutput(
        has_incident_signal=False,
        keywords_matched=[],
        source="none",
        snippet="",
    )
    with patch(
        "eval_kit.enterprise_signals.collectors.incident.call_llm",
        return_value=mock_output,
    ):
        collector = IncidentSignalCollector()
        result = collector.collect(_make_pr_ctx())

    assert result["has_incident_signal"] is False
    assert result["evidence"]["keywords_matched"] == []
    assert result["evidence"]["source"] == "none"
    assert result["evidence"]["snippet"] == ""


def test_incident_collector_records_error_on_llm_exception():
    with patch(
        "eval_kit.enterprise_signals.collectors.incident.call_llm",
        side_effect=RuntimeError("LLM timeout"),
    ):
        collector = IncidentSignalCollector()
        result = collect_for_pr(_make_pr_ctx(), [collector])

    assert "error" in result["incident_signal"]
    assert "LLM timeout" in result["incident_signal"]["error"]


def test_incident_collector_skipped_when_skip_quality_llm():
    collector = IncidentSignalCollector(skip_llm=True)
    result = collector.collect(_make_pr_ctx())
    assert result == {"skipped": True}


def test_pr_characterization_with_incident_collector_enabled(fixture_repos):
    from repo_evaluator import PRAnalyzer
    from eval_kit.platform_clients import PlatformClient
    from eval_kit.repo_evaluator_helpers import load_language_config
    from tests._snapshot import assert_matches_snapshot

    cassette = json.loads((PLATFORM_DIR / "multi_lang_prs.json").read_text())

    client = Mock(spec=PlatformClient)
    client.owner = "fake"
    client.repo_name = "multi_lang_ci"
    client.repo_full_name = "fake/multi_lang_ci"
    client.token = None
    client.fetch_prs.return_value = cassette
    client.extract_issue_number_from_text.return_value = []
    client.fetch_issue.return_value = None

    mock_output = IncidentSignalOutput(
        has_incident_signal=False,
        keywords_matched=[],
        source="none",
        snippet="",
    )

    register_pr_collector(IncidentSignalCollector())

    with patch(
        "eval_kit.enterprise_signals.collectors.incident.call_llm",
        return_value=mock_output,
    ):
        analyzer = PRAnalyzer(
            platform_client=client,
            language_config=load_language_config(),
            repo_path=str(REPOS_DIR / "multi_lang_ci"),
        )
        stats = analyzer.analyze_prs(start_cursor=None, batch_limit=10)

    result = asdict(stats)
    snapshot_path = SNAPSHOTS_DIR / "pr_multi_lang_ci_with_incident.json"
    assert_matches_snapshot(result, snapshot_path)
