"""Tests for Stage E7: Adjacent artifacts collector."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_kit.enterprise_signals.base import PRContext
from eval_kit.enterprise_signals.collectors.adjacent_artifacts import (
    AdjacentArtifactsCollector,
)
from eval_kit.enterprise_signals.registry import reset_collectors


def _make_pr_ctx(**kwargs) -> PRContext:
    defaults = dict(
        number=7,
        title="feat: add payment flow",
        body="",
        issue_title=None,
        issue_body=None,
        commit_messages=[],
        changed_files=[],
        diff=None,
        repo_path=Path("/tmp/repo"),
        primary_language="Python",
        owner="myorg",
        repo_name="myrepo",
    )
    defaults.update(kwargs)
    return PRContext(**defaults)


@pytest.fixture(autouse=True)
def _reset_registry():
    yield
    reset_collectors()


def test_jira_link_in_body():
    result = AdjacentArtifactsCollector().collect(
        _make_pr_ctx(body="Fixes https://mycompany.atlassian.net/browse/PROJ-123")
    )
    assert result["has_external_artifacts"] is True
    assert any(link["type"] == "jira" for link in result["links"])


def test_figma_link_detected():
    result = AdjacentArtifactsCollector().collect(
        _make_pr_ctx(body="Design: https://www.figma.com/file/abc123/My-Design")
    )
    assert result["has_external_artifacts"] is True
    assert any(link["type"] == "figma" for link in result["links"])


def test_linear_link_detected():
    result = AdjacentArtifactsCollector().collect(
        _make_pr_ctx(body="Closes https://linear.app/myorg/issue/ENG-42")
    )
    assert result["has_external_artifacts"] is True
    assert any(link["type"] == "linear" for link in result["links"])


def test_no_artifacts_when_plain_text():
    result = AdjacentArtifactsCollector().collect(
        _make_pr_ctx(body="This PR refactors the utils module.")
    )
    assert result["has_external_artifacts"] is False
    assert result["links"] == []


def test_links_deduplicated():
    url = "https://mycompany.atlassian.net/browse/PROJ-1"
    result = AdjacentArtifactsCollector().collect(
        _make_pr_ctx(body=url, issue_body=url)
    )
    jira_links = [link for link in result["links"] if link["type"] == "jira"]
    assert len(jira_links) == 1


def test_issue_body_also_scanned():
    result = AdjacentArtifactsCollector().collect(
        _make_pr_ctx(
            body="No links here",
            issue_body="See https://mycompany.atlassian.net/browse/PROJ-99 for context",
        )
    )
    assert result["has_external_artifacts"] is True


def test_github_issue_same_repo_ignored():
    # A link to an issue in the PR's own repo is routine workflow — not an artifact.
    result = AdjacentArtifactsCollector().collect(
        _make_pr_ctx(
            owner="myorg",
            repo_name="myrepo",
            body="Fixes https://github.com/myorg/myrepo/issues/42",
        )
    )
    assert result["has_external_artifacts"] is False
    assert result["links"] == []


def test_github_issue_cross_repo_detected():
    # A link to an issue in a *different* repo signals cross-team coordination.
    result = AdjacentArtifactsCollector().collect(
        _make_pr_ctx(
            owner="myorg",
            repo_name="myrepo",
            body="Tracked in https://github.com/otherorg/otherrepo/issues/7",
        )
    )
    assert result["has_external_artifacts"] is True
    assert any(link["type"] == "github_issue" for link in result["links"])


def test_github_pr_same_repo_ignored():
    result = AdjacentArtifactsCollector().collect(
        _make_pr_ctx(
            owner="myorg",
            repo_name="myrepo",
            body="Blocked by https://github.com/myorg/myrepo/pull/100",
        )
    )
    assert result["has_external_artifacts"] is False
    assert result["links"] == []


def test_github_pr_cross_repo_detected():
    result = AdjacentArtifactsCollector().collect(
        _make_pr_ctx(
            owner="myorg",
            repo_name="myrepo",
            body="Depends on https://github.com/otherorg/sharedlib/pull/55",
        )
    )
    assert result["has_external_artifacts"] is True
    assert any(link["type"] == "github_pr" for link in result["links"])


def test_github_same_repo_case_insensitive():
    # Owner/repo comparison must be case-insensitive.
    result = AdjacentArtifactsCollector().collect(
        _make_pr_ctx(
            owner="MyOrg",
            repo_name="MyRepo",
            body="Fixes https://github.com/myorg/myrepo/issues/1",
        )
    )
    assert result["has_external_artifacts"] is False


def test_github_link_without_owner_context_included():
    # When owner/repo_name are unknown (None), GitHub links are kept rather
    # than silently dropped, erring on the side of recall.
    result = AdjacentArtifactsCollector().collect(
        _make_pr_ctx(
            owner=None,
            repo_name=None,
            body="See https://github.com/someorg/somerepo/issues/3",
        )
    )
    assert result["has_external_artifacts"] is True
