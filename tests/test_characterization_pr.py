"""PR-level characterization tests.

Exercises PRAnalyzer.analyze_prs() in-process for each fixture with a
PlatformClient mock returning canned cassette data — no network, no LLM.

The snapshots under tests/fixtures/snapshots/pr_*.json are generated once with
    UPDATE_SNAPSHOTS=1 pytest tests/test_characterization_pr.py
and then serve as the immutable baseline for stages 1-7.

When analyze_prs() starts returning BatchResult (Stage 5b) the assertion
becomes ``result.stats`` instead of ``result`` — the snapshot stays unchanged.
"""

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import pytest

from eval_kit.platform_clients import PlatformClient
from eval_kit.repo_evaluator_helpers import load_language_config
from tests._snapshot import assert_matches_snapshot

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PLATFORM_DIR = FIXTURES_DIR / "platform"
REPOS_DIR = FIXTURES_DIR / "repos"
SNAPSHOTS_DIR = FIXTURES_DIR / "snapshots"


def _load_cassette(fixture_name: str) -> dict:
    return json.loads((PLATFORM_DIR / f"{fixture_name}_prs.json").read_text())


def _make_client(fixture_name: str, cassette: dict) -> Mock:
    client = Mock(spec=PlatformClient)
    client.owner = "fake"
    client.repo_name = fixture_name
    client.repo_full_name = f"fake/{fixture_name}"
    client.token = None
    client.fetch_prs.return_value = cassette
    client.extract_issue_number_from_text.return_value = []
    client.fetch_issue.return_value = None
    return client


def _run_pr_analysis(fixture_name: str, cassette_name: str) -> dict:
    from repo_evaluator import PRAnalyzer

    cassette = _load_cassette(cassette_name)
    client = _make_client(fixture_name, cassette)

    analyzer = PRAnalyzer(
        platform_client=client,
        language_config=load_language_config(),
        repo_path=str(REPOS_DIR / fixture_name),
    )
    stats = analyzer.analyze_prs(start_cursor=None, batch_limit=10)
    return asdict(stats)


@pytest.mark.parametrize(
    "fixture_name,cassette_name",
    [
        ("tiny_python", "tiny_python"),
        ("no_prs", "no_prs"),
        ("multi_lang_ci", "multi_lang"),
    ],
)
def test_pr_characterization(fixture_name, cassette_name, fixture_repos):
    result = _run_pr_analysis(fixture_name, cassette_name)
    snapshot_path = SNAPSHOTS_DIR / f"pr_{fixture_name}.json"
    assert_matches_snapshot(result, snapshot_path)
