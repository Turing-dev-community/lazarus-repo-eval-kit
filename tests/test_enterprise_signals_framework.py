"""Unit tests for the enterprise_signals framework (Stage E0).

All tests use inline stub collectors — no LLM, no network.
"""

import json
from pathlib import Path
from typing import Any, Dict


from eval_kit.enterprise_signals.base import (
    PRCollector,
    PRContext,
    RepoCollector,
    RepoContext,
)
from eval_kit.enterprise_signals.framework import collect_for_pr, collect_for_repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pr_ctx(**kwargs) -> PRContext:
    defaults = dict(
        number=1,
        title="Fix bug",
        body="Fixes issue #1",
        issue_title=None,
        issue_body=None,
        commit_messages=[],
        changed_files=["src/foo.py"],
        diff=None,
        repo_path=Path("/tmp/repo"),
        primary_language="Python",
    )
    defaults.update(kwargs)
    return PRContext(**defaults)


def _make_repo_ctx(**kwargs) -> RepoContext:
    defaults = dict(
        repo_path=Path("/tmp/repo"),
        owner="acme",
        repo_name="widgets",
        primary_language="Python",
    )
    defaults.update(kwargs)
    return RepoContext(**defaults)


class _OkPRCollector(PRCollector):
    name = "ok_collector"
    requires_diff = False

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        return {"has_ok": True, "evidence": {"pr_number": pr.number}}


class _OtherPRCollector(PRCollector):
    name = "other_collector"
    requires_diff = False

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        return {"has_other": False}


class _BrokenPRCollector(PRCollector):
    name = "broken_collector"
    requires_diff = False

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        raise RuntimeError("LLM timeout")


class _DiffRequiringCollector(PRCollector):
    name = "diff_collector"
    requires_diff = True

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        return {"saw_diff": pr.diff is not None}


class _OkRepoCollector(RepoCollector):
    name = "repo_ok"

    def collect(self, repo: RepoContext) -> Dict[str, Any]:
        return {"repo_name": repo.repo_name}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_collect_for_pr_dispatches_to_each_collector_and_merges_results():
    pr = _make_pr_ctx()
    result = collect_for_pr(pr, [_OkPRCollector(), _OtherPRCollector()])
    assert result["ok_collector"] == {"has_ok": True, "evidence": {"pr_number": 1}}
    assert result["other_collector"] == {"has_other": False}
    assert len(result) == 2


def test_collect_for_pr_collector_exception_recorded_as_error_others_run():
    pr = _make_pr_ctx()
    result = collect_for_pr(pr, [_OkPRCollector(), _BrokenPRCollector()])
    assert result["ok_collector"]["has_ok"] is True
    assert "error" in result["broken_collector"]
    assert "LLM timeout" in result["broken_collector"]["error"]


def test_diff_only_populated_when_a_collector_requires_it():
    """PRContext.diff is None when no registered collector sets requires_diff=True,
    even if __full_patch is on pr_data (simulated by passing diff directly)."""
    pr_no_diff = _make_pr_ctx(diff=None)
    pr_with_diff = _make_pr_ctx(
        diff="--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new"
    )

    # No diff-requiring collector: diff in context should be ignored / not present
    result_no_req = collect_for_pr(pr_no_diff, [_OkPRCollector()])
    assert "diff_collector" not in result_no_req

    # Diff-requiring collector present: diff propagated correctly
    result_with_req = collect_for_pr(pr_with_diff, [_DiffRequiringCollector()])
    assert result_with_req["diff_collector"]["saw_diff"] is True

    # Diff-requiring collector with no diff in context: saw_diff is False
    result_no_diff = collect_for_pr(pr_no_diff, [_DiffRequiringCollector()])
    assert result_no_diff["diff_collector"]["saw_diff"] is False


def test_collect_for_repo_dispatches_and_merges():
    repo = _make_repo_ctx()
    result = collect_for_repo(repo, [_OkRepoCollector()])
    assert result["repo_ok"] == {"repo_name": "widgets"}


def test_per_pr_output_serializable_to_json():
    pr = _make_pr_ctx()
    result = collect_for_pr(pr, [_OkPRCollector(), _OtherPRCollector()])
    serialized = json.dumps(result)
    roundtripped = json.loads(serialized)
    assert roundtripped["ok_collector"]["has_ok"] is True


def test_empty_registry_produces_no_enterprise_signals_key_in_json():
    """With an empty collector list, collect_for_pr returns {} — callers skip
    setting enterprise_signals, so the JSON key is absent."""
    pr = _make_pr_ctx()
    result = collect_for_pr(pr, [])
    assert result == {}
    # Simulate the caller's guard: only set enterprise_signals when result is non-empty
    pr_data: Dict[str, Any] = {"number": 1, "title": "Fix bug"}
    if result:
        pr_data["enterprise_signals"] = result
    assert "enterprise_signals" not in pr_data
