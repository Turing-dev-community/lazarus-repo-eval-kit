"""Repo-level characterization tests.

Exercises the full RepoEvaluator pipeline (filesystem, git, scoring) for each
fixture repo with a mocked PlatformClient — no network, no LLM.

The snapshots under tests/fixtures/snapshots/repo_*.json are generated once with
    UPDATE_SNAPSHOTS=1 pytest tests/test_characterization_repo.py
and then serve as the immutable baseline for stages 1-7.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from eval_kit.platform_clients import PlatformClient
from tests._snapshot import assert_matches_snapshot

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REPOS_DIR = FIXTURES_DIR / "repos"
SNAPSHOTS_DIR = FIXTURES_DIR / "snapshots"

_EMPTY_PR_RESPONSE = {
    "data": {
        "repository": {
            "primaryLanguage": {"name": "Python"},
            "pullRequests": {
                "pageInfo": {"endCursor": None, "hasNextPage": False},
                "nodes": [],
            },
        }
    }
}


def _make_platform_client(fixture_name: str) -> Mock:
    """Return a PlatformClient mock that returns empty PR data and zero issue counts."""
    client = Mock(spec=PlatformClient)
    client.owner = "fake"
    client.repo_name = fixture_name
    client.repo_full_name = f"fake/{fixture_name}"
    client.token = None
    client.fetch_issue_count.return_value = {"open": 0, "closed": 0, "total": 0}
    client.fetch_repo_languages.return_value = None
    client.fetch_prs.return_value = _EMPTY_PR_RESPONSE
    client.extract_issue_number_from_text.return_value = []
    return client


def _run_evaluator(fixture_name: str) -> dict:
    from repo_evaluator import RepoEvaluator, to_json

    repo_path = str(REPOS_DIR / fixture_name)
    client = _make_platform_client(fixture_name)

    evaluator = RepoEvaluator(
        repo_path=repo_path,
        owner="fake",
        repo_name=fixture_name,
        platform_client=client,
        skip_f2p=True,
        skip_pr_rubrics=True,
    )
    report = evaluator.evaluate()
    return to_json(report)


@pytest.mark.parametrize(
    "fixture_name",
    ["tiny_python", "no_prs", "multi_lang_ci"],
)
def test_repo_characterization(fixture_name, fixture_repos):
    result = _run_evaluator(fixture_name)
    snapshot_path = SNAPSHOTS_DIR / f"repo_{fixture_name}.json"
    assert_matches_snapshot(result, snapshot_path)
