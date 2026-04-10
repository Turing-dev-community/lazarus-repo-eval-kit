"""
Wrapper for running vibecode, security, and production quality checks.

With ``repo_path``, analyzes that checkout in place. Otherwise clones the repo
into a temp directory, runs the check, and returns (critical_text, signals_text).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from eval_kit.production_quality_check import _check_repo as _check_repo_production
from eval_kit.security_check import _check_repo as _check_repo_security
from eval_kit.vibecode_check import _check_repo as _check_repo_vibecode

logger = logging.getLogger(__name__)


def run_vibe_coding_check(
    owner: str,
    repo: str,
    token: str,
    skip_llm: bool = False,
    repo_path: str | Path | None = None,
) -> tuple[str, str]:
    """Run vibecode check. Returns (critical_text, signals_text)."""
    existing = str(Path(repo_path).resolve()) if repo_path else None
    clone_base = ""
    if not existing:
        clone_base = tempfile.mkdtemp(prefix="vibe_qc_")

    try:
        result = _check_repo_vibecode(
            owner=owner,
            repo=repo,
            token=token,
            clone_base=clone_base or ".",
            verbose_log=None,
            skip_llm=skip_llm,
            existing_repo_path=existing,
        )
        if result.get("error"):
            logger.warning(
                "Vibecode check error for %s/%s: %s", owner, repo, result["error"]
            )
            return f"Error: {result['error']}", ""
        critical = result.get("final_details_critical", [])
        signals = result.get("final_details_signals", [])
        return "\n".join(critical), "\n".join(signals)
    except Exception as e:
        logger.warning("Vibecode check exception for %s/%s: %s", owner, repo, e)
        return f"Error: {e}", ""
    finally:
        if clone_base and os.path.exists(clone_base):
            shutil.rmtree(clone_base, ignore_errors=True)


def run_security_check(
    owner: str,
    repo: str,
    token: str,
    skip_llm: bool = False,
    repo_path: str | Path | None = None,
) -> tuple[str, str]:
    """Run security check. Returns (critical_text, signals_text)."""
    existing = str(Path(repo_path).resolve()) if repo_path else None
    clone_base = ""
    if not existing:
        clone_base = tempfile.mkdtemp(prefix="security_qc_")

    try:
        result = _check_repo_security(
            owner=owner,
            repo=repo,
            token=token,
            clone_base=clone_base or ".",
            verbose_log=None,
            skip_llm=skip_llm,
            existing_repo_path=existing,
        )
        if result.get("error"):
            logger.warning(
                "Security check error for %s/%s: %s", owner, repo, result["error"]
            )
            return f"Error: {result['error']}", ""
        critical = result.get("final_details_critical", [])
        signals = result.get("final_details_signals", [])
        return "\n".join(critical), "\n".join(signals)
    except Exception as e:
        logger.warning("Security check exception for %s/%s: %s", owner, repo, e)
        return f"Error: {e}", ""
    finally:
        if clone_base and os.path.exists(clone_base):
            shutil.rmtree(clone_base, ignore_errors=True)


def run_production_quality_check(
    owner: str,
    repo: str,
    token: str,
    skip_llm: bool = False,
    repo_path: str | Path | None = None,
) -> tuple[str, str]:
    """Run production quality check. Returns (critical_text, signals_text)."""
    existing = str(Path(repo_path).resolve()) if repo_path else None
    clone_base = ""
    if not existing:
        clone_base = tempfile.mkdtemp(prefix="prodq_qc_")

    try:
        result = _check_repo_production(
            owner=owner,
            repo=repo,
            token=token,
            clone_base=clone_base or ".",
            verbose_log=None,
            skip_llm=skip_llm,
            existing_repo_path=existing,
        )
        if result.get("error"):
            logger.warning(
                "Production quality error for %s/%s: %s", owner, repo, result["error"]
            )
            return f"Error: {result['error']}", ""
        critical = result.get("final_details_critical", [])
        signals = result.get("final_details_signals", [])
        return "\n".join(critical), "\n".join(signals)
    except Exception as e:
        logger.warning("Production quality exception for %s/%s: %s", owner, repo, e)
        return f"Error: {e}", ""
    finally:
        if clone_base and os.path.exists(clone_base):
            shutil.rmtree(clone_base, ignore_errors=True)


def run_all_quality_checks(
    owner: str,
    repo: str,
    token: str,
    skip_llm: bool = False,
    repo_path: str | Path | None = None,
) -> dict[str, str]:
    """
    Run all three quality checks and return a dict with the 6 column values:
      vibe_coding_critical, vibe_coding_signals,
      security_check_critical, security_check_signals,
      production_quality_critical, production_quality_signals
    """
    logger.info("Running vibecode check for %s/%s ...", owner, repo)
    vibe_crit, vibe_sig = run_vibe_coding_check(
        owner, repo, token, skip_llm, repo_path=repo_path
    )

    logger.info("Running security check for %s/%s ...", owner, repo)
    sec_crit, sec_sig = run_security_check(
        owner, repo, token, skip_llm, repo_path=repo_path
    )

    logger.info("Running production quality check for %s/%s ...", owner, repo)
    prod_crit, prod_sig = run_production_quality_check(
        owner, repo, token, skip_llm, repo_path=repo_path
    )

    return {
        "vibe_coding_critical": vibe_crit,
        "vibe_coding_signals": vibe_sig,
        "security_check_critical": sec_crit,
        "security_check_signals": sec_sig,
        "production_quality_critical": prod_crit,
        "production_quality_signals": prod_sig,
    }
