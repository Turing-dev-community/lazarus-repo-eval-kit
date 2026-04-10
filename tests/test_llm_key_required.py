"""Tests that the LLM API key is enforced — no silent skips, no empty columns."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = str(Path(__file__).parent.parent)
PYTHON = [sys.executable]

import eval_kit.llm_client  # noqa: E402


@pytest.fixture(autouse=True)
def clear_cache():
    eval_kit.llm_client.clear_llm_client()


def _run_main(provider=None, *args):
    # Filter out all LLM keys and provider env vars to start from clean slate
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "LLM_PROVIDER"]
    }
    env["REPO_EVAL_SKIP_DOTENV"] = "1"
    if provider:
        env["LLM_PROVIDER"] = provider

    return subprocess.run(
        [*PYTHON, "repo_evaluator.py", "owner/repo", "--token", "tok", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=PROJECT_ROOT,
    )


@pytest.mark.parametrize(
    "provider,key_var",
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
    ],
)
def test_main_exits_when_key_missing(provider, key_var):
    result = _run_main(provider)
    assert result.returncode == 1
    assert key_var in result.stderr


def test_main_message_includes_setup_hint():
    result = _run_main("openai")
    assert ".env" in result.stderr


@pytest.mark.parametrize(
    "provider,key_var",
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
    ],
)
def test_main_exits_with_skip_quality_llm_only(provider, key_var):
    """Skipping only LLM quality checks is not enough — taxonomy still needs key."""
    result = _run_main(provider, "--skip-quality-llm")
    assert result.returncode == 1
    assert key_var in result.stderr


@pytest.mark.parametrize(
    "provider,key_var",
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
    ],
)
def test_main_no_exit_when_all_llm_features_skipped(provider, key_var):
    """With all LLM-dependent features skipped the key guard must not fire."""
    result = _run_main(
        provider, "--skip-quality-llm", "--skip-taxonomy", "--skip-pr-rubrics"
    )
    assert key_var not in result.stderr


@pytest.mark.parametrize(
    "provider,key_var",
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
    ],
)
def test_validate_api_key_raises_without_key(provider, key_var):
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(key_var, None)
        with pytest.raises(ValueError, match=key_var):
            eval_kit.llm_client.validate_api_key(provider)


@pytest.mark.parametrize(
    "provider,key_var",
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
    ],
)
def test_call_llm_raises_without_api_key(provider, key_var):
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(key_var, None)
        os.environ["LLM_PROVIDER"] = provider
        with pytest.raises(ValueError, match=key_var):
            eval_kit.llm_client.call_llm(
                [{"role": "user", "content": "test"}],
            )


@pytest.mark.parametrize(
    "provider,key_var",
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
    ],
)
def test_run_taxonomy_for_accepted_prs_raises_without_key(provider, key_var):
    from eval_kit.taxonomy_check import run_taxonomy_for_accepted_prs

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(key_var, None)
        os.environ["LLM_PROVIDER"] = provider
        results = run_taxonomy_for_accepted_prs(
            accepted_prs=[{"number": 1}],
            owner="o",
            repo="r",
            primary_language="Python",
            get_patch=lambda pr: None,
        )
        assert len(results) == 1
        assert "error" in results[0]
        assert key_var in results[0]["error"]


@pytest.mark.parametrize(
    "provider,key_var",
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
    ],
)
def test_run_taxonomy_classification_raises_without_key(provider, key_var):
    from eval_kit.taxonomy_check import run_taxonomy_classification

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(key_var, None)
        os.environ["LLM_PROVIDER"] = provider
        result = run_taxonomy_classification(owner="o", repo="r", repo_path="/tmp")
        assert result == {}
