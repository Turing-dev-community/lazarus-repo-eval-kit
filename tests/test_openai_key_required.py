"""Tests that OPENAI_API_KEY is enforced — no silent skips, no empty columns."""

import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = str(Path(__file__).parent.parent)
PYTHON = ["python"]

# ── Stub out openai before any module import touches it ──────────────────────

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


# ── main() early-exit guard ───────────────────────────────────────────────────


def _run_main(*args):
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
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


def test_main_no_exit_when_pr_rubrics_uses_gemini():
    """Gemini provider for PR rubrics doesn't need OPENAI_API_KEY."""
    result = _run_main(
        "--skip-quality-llm", "--skip-taxonomy", "--pr-rubrics-provider", "gemini"
    )
    assert "OPENAI_API_KEY" not in result.stderr


# ── quality_checks._get_openai_client ────────────────────────────────────────


def test_get_openai_client_raises_without_key():
    import quality_checks

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        quality_checks._get_openai_client()


# ── quality_evaluator.QualityEvaluator._call_llm ─────────────────────────────


def test_call_llm_raises_without_api_key():
    from quality_evaluator import QualityEvaluator

    evaluator = QualityEvaluator(llm_provider="openai", api_key="")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        evaluator._call_llm("some prompt")


# ── taxonomy_check functions ──────────────────────────────────────────────────


def test_run_taxonomy_for_accepted_prs_raises_without_key():
    from taxonomy_check import run_taxonomy_for_accepted_prs

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        run_taxonomy_for_accepted_prs(
            accepted_prs=[{"number": 1}],
            owner="o",
            repo="r",
            primary_language="Python",
            get_patch=lambda pr: None,
        )


def test_run_taxonomy_classification_raises_without_key():
    from taxonomy_check import run_taxonomy_classification

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        run_taxonomy_classification(owner="o", repo="r", repo_path="/tmp")
