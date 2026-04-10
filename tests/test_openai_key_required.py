"""Tests that OPENAI_API_KEY is enforced — no silent skips, no empty columns."""

import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = str(Path(__file__).parent.parent)
PYTHON = ["python"]

_openai_stub = types.ModuleType("openai")
_openai_stub.RateLimitError = Exception
_openai_stub.APITimeoutError = Exception
_openai_stub.APIConnectionError = Exception
_openai_stub.InternalServerError = Exception
_openai_stub.OpenAI = MagicMock()
sys.modules.setdefault("openai", _openai_stub)

_pydantic_stub = types.ModuleType("pydantic")
_pydantic_stub.BaseModel = object
_pydantic_stub.Field = lambda *a, **kw: None
sys.modules.setdefault("pydantic", _pydantic_stub)

import eval_kit.llm_client  # noqa: E402


@pytest.fixture(autouse=True)
def clear_cache():
    eval_kit.llm_client.clear_llm_client()


def _run_main(*args):
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    env["OPENAI_API_KEY"] = ""
    return subprocess.run(
        [*PYTHON, "repo_evaluator.py", "owner/repo", "--token", "tok", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=PROJECT_ROOT,
    )


def test_main_exits_when_key_missing():
    result = _run_main()
    assert result.returncode == 1
    assert "OPENAI_API_KEY" in result.stderr


def test_main_message_includes_setup_hint():
    result = _run_main()
    assert ".env" in result.stderr
    assert "openai.com/api-keys" in result.stderr


def test_main_exits_with_skip_quality_llm_only():
    """Skipping only LLM quality checks is not enough — taxonomy still needs key."""
    result = _run_main("--skip-quality-llm")
    assert result.returncode == 1
    assert "OPENAI_API_KEY" in result.stderr


def test_main_no_exit_when_all_openai_features_skipped():
    """With all OpenAI-dependent features skipped the key guard must not fire."""
    result = _run_main("--skip-quality-llm", "--skip-taxonomy", "--skip-pr-rubrics")
    assert "OPENAI_API_KEY" not in result.stderr


def test_get_openai_client_raises_without_key():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENAI_API_KEY", None)
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            eval_kit.llm_client._get_openai_client()


def test_call_llm_raises_without_api_key():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENAI_API_KEY", None)
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            eval_kit.llm_client.call_llm(
                [{"role": "user", "content": "test"}],
                model="gpt-4o",
            )


def test_run_taxonomy_for_accepted_prs_raises_without_key():
    from eval_kit.taxonomy_check import run_taxonomy_for_accepted_prs

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENAI_API_KEY", None)
        results = run_taxonomy_for_accepted_prs(
            accepted_prs=[{"number": 1}],
            owner="o",
            repo="r",
            primary_language="Python",
            get_patch=lambda pr: None,
        )
        assert len(results) == 1
        assert "error" in results[0]
        assert "OPENAI_API_KEY" in results[0]["error"]


def test_run_taxonomy_classification_raises_without_key():
    from eval_kit.taxonomy_check import run_taxonomy_classification

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENAI_API_KEY", None)
        result = run_taxonomy_classification(owner="o", repo="r", repo_path="/tmp")
        assert result == {}
