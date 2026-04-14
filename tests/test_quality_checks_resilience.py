"""Tests for per-check resilience in quality_checks.run_all_quality_checks.

When one quality check fails, the others must still run and their results
must be preserved.
"""

from unittest.mock import patch

import pytest

from eval_kit import quality_checks
from eval_kit.usage_tracker import CostLimitAborted


def _call(**kwargs):
    return quality_checks.run_all_quality_checks(
        owner="o",
        repo="r",
        token="t",
        skip_llm=True,
        repo_path="/nonexistent",
        **kwargs,
    )


@patch("eval_kit.quality_checks.run_production_quality_check")
@patch("eval_kit.quality_checks.run_security_check")
@patch("eval_kit.quality_checks.run_vibe_coding_check")
def test_all_three_succeed(mock_vibe, mock_sec, mock_prod):
    mock_vibe.return_value = ("vc", "vs")
    mock_sec.return_value = ("sc", "ss")
    mock_prod.return_value = ("pc", "ps")

    result = _call()

    assert result == {
        "vibe_coding_critical": "vc",
        "vibe_coding_signals": "vs",
        "vibe_coding_status": "",
        "security_check_critical": "sc",
        "security_check_signals": "ss",
        "security_check_status": "",
        "production_quality_critical": "pc",
        "production_quality_signals": "ps",
        "production_quality_status": "",
    }


@patch("eval_kit.quality_checks.run_production_quality_check")
@patch("eval_kit.quality_checks.run_security_check")
@patch("eval_kit.quality_checks.run_vibe_coding_check")
def test_vibe_succeeds_security_fails_production_succeeds(
    mock_vibe, mock_sec, mock_prod
):
    """A middle-of-pipeline failure must NOT discard adjacent results."""
    mock_vibe.return_value = ("vc", "vs")
    mock_sec.side_effect = RuntimeError("no source files")
    mock_prod.return_value = ("pc", "ps")

    result = _call()

    # Vibe and production results preserved; only security is blank
    assert result["vibe_coding_critical"] == "vc"
    assert result["vibe_coding_signals"] == "vs"
    assert result["vibe_coding_status"] == ""
    assert result["security_check_critical"] == ""
    assert result["security_check_signals"] == ""
    # Status distinguishes "failed" from "ok with no findings"
    assert result["security_check_status"] == "failed: no source files"
    assert result["production_quality_critical"] == "pc"
    assert result["production_quality_signals"] == "ps"
    assert result["production_quality_status"] == ""


@patch("eval_kit.quality_checks.run_production_quality_check")
@patch("eval_kit.quality_checks.run_security_check")
@patch("eval_kit.quality_checks.run_vibe_coding_check")
def test_vibe_fails_others_still_run(mock_vibe, mock_sec, mock_prod):
    """A failure in the first check must not short-circuit later checks."""
    mock_vibe.side_effect = RuntimeError("boom")
    mock_sec.return_value = ("sc", "ss")
    mock_prod.return_value = ("pc", "ps")

    result = _call()

    assert result["vibe_coding_critical"] == ""
    assert result["vibe_coding_status"].startswith("failed:")
    assert result["security_check_critical"] == "sc"
    assert result["security_check_status"] == ""
    assert result["production_quality_critical"] == "pc"
    assert result["production_quality_status"] == ""
    assert mock_sec.called and mock_prod.called


@patch("eval_kit.quality_checks.run_production_quality_check")
@patch("eval_kit.quality_checks.run_security_check")
@patch("eval_kit.quality_checks.run_vibe_coding_check")
def test_all_three_fail(mock_vibe, mock_sec, mock_prod):
    mock_vibe.side_effect = RuntimeError("a")
    mock_sec.side_effect = RuntimeError("b")
    mock_prod.side_effect = RuntimeError("c")

    result = _call()

    assert result["vibe_coding_critical"] == ""
    assert result["security_check_critical"] == ""
    assert result["production_quality_critical"] == ""
    # All three status fields should explain the failure
    assert result["vibe_coding_status"] == "failed: a"
    assert result["security_check_status"] == "failed: b"
    assert result["production_quality_status"] == "failed: c"


@patch("eval_kit.quality_checks.run_production_quality_check")
@patch("eval_kit.quality_checks.run_security_check")
@patch("eval_kit.quality_checks.run_vibe_coding_check")
def test_clean_repo_distinguishable_from_failed_check(mock_vibe, mock_sec, mock_prod):
    """Empty critical/signals with status='ok' means genuinely clean;
    empty with status='failed' means the check didn't complete."""
    mock_vibe.return_value = ("", "")  # clean
    mock_sec.side_effect = RuntimeError("crashed")  # failed
    mock_prod.return_value = ("", "")  # clean

    result = _call()

    # Both vibe and security have empty strings — but status differentiates them
    assert result["vibe_coding_critical"] == ""
    assert result["security_check_critical"] == ""
    assert result["vibe_coding_status"] == ""
    assert result["security_check_status"].startswith("failed:")


@patch("eval_kit.quality_checks.run_production_quality_check")
@patch("eval_kit.quality_checks.run_security_check")
@patch("eval_kit.quality_checks.run_vibe_coding_check")
def test_cost_limit_aborted_propagates(mock_vibe, mock_sec, mock_prod):
    """CostLimitAborted must NOT be swallowed — it short-circuits the run."""
    mock_vibe.side_effect = CostLimitAborted()

    with pytest.raises(CostLimitAborted):
        _call()
