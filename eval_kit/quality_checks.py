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
            raise RuntimeError(result["error"])
        critical = result.get("final_details_critical", [])
        signals = result.get("final_details_signals", [])
        return "\n".join(critical), "\n".join(signals)
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
            raise RuntimeError(result["error"])
        critical = result.get("final_details_critical", [])
        signals = result.get("final_details_signals", [])
        return "\n".join(critical), "\n".join(signals)
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
            raise RuntimeError(result["error"])
        critical = result.get("final_details_critical", [])
        signals = result.get("final_details_signals", [])
        return "\n".join(critical), "\n".join(signals)
    finally:
        if clone_base and os.path.exists(clone_base):
            shutil.rmtree(clone_base, ignore_errors=True)


def _run_one(label: str, fn, *args, **kwargs) -> tuple[str, str, str]:
    """Invoke one check, returning (critical, signals, status).

    status is "" on success (same default downstream consumers see for a
    missing column — keeps back-compat for existing CSV readers), or
    "failed: <error>" on exception, so callers can distinguish a genuinely
    clean result from a skipped check. CostLimitAborted is re-raised so
    cost limits short-circuit correctly.
    """
    from eval_kit.usage_tracker import CostLimitAborted

    try:
        crit, sig = fn(*args, **kwargs)
        return crit, sig, ""
    except CostLimitAborted:
        raise
    except Exception as e:
        logger.warning("%s check failed: %s — continuing without it", label, e)
        return "", "", f"failed: {e}"


def run_all_quality_checks(
    owner: str,
    repo: str,
    token: str,
    skip_llm: bool = False,
    repo_path: str | Path | None = None,
) -> dict[str, str]:
    """
    Run all three quality checks and return a dict with the 9 column values:
      vibe_coding_critical, vibe_coding_signals, vibe_coding_status,
      security_check_critical, security_check_signals, security_check_status,
      production_quality_critical, production_quality_signals, production_quality_status

    Each check runs independently — a failure in one does not discard
    results from the others. Status is "" on success or "failed: <reason>"
    on exception, so consumers can distinguish "clean repo" from
    "check did not run". Blank-on-success keeps the column harmless for
    downstream CSV readers that don't know about the new field.
    """
    logger.info("Running vibecode check for %s/%s ...", owner, repo)
    vibe_crit, vibe_sig, vibe_status = _run_one(
        "vibecode",
        run_vibe_coding_check,
        owner,
        repo,
        token,
        skip_llm,
        repo_path=repo_path,
    )

    logger.info("Running security check for %s/%s ...", owner, repo)
    sec_crit, sec_sig, sec_status = _run_one(
        "security",
        run_security_check,
        owner,
        repo,
        token,
        skip_llm,
        repo_path=repo_path,
    )

    logger.info("Running production quality check for %s/%s ...", owner, repo)
    prod_crit, prod_sig, prod_status = _run_one(
        "production quality",
        run_production_quality_check,
        owner,
        repo,
        token,
        skip_llm,
        repo_path=repo_path,
    )

    return {
        "vibe_coding_critical": vibe_crit,
        "vibe_coding_signals": vibe_sig,
        "vibe_coding_status": vibe_status,
        "security_check_critical": sec_crit,
        "security_check_signals": sec_sig,
        "security_check_status": sec_status,
        "production_quality_critical": prod_crit,
        "production_quality_signals": prod_sig,
        "production_quality_status": prod_status,
    }
