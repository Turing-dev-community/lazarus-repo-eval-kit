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

from eval_kit.agent_check import (
    run_production_agent,
    run_security_agent,
    run_vibe_agent,
)
from eval_kit.repo_evaluator_helpers import clone_repo

logger = logging.getLogger(__name__)


def resolve_repo_root(
    owner: str,
    repo: str,
    token: str,
    repo_path: str | Path | None,
    tmp_prefix: str,
) -> tuple[str, str]:
    """Return (root_path, clone_base_to_cleanup).

    If repo_path is given, resolve and return it with no clone.
    Otherwise clone into a temp dir and return both root and the temp base.
    """
    if repo_path:
        return str(Path(repo_path).resolve()), ""
    clone_base = Path(tempfile.mkdtemp(prefix=tmp_prefix))
    try:
        root = clone_repo(f"{owner}/{repo}", clone_base, token, depth=200)
    except Exception:
        shutil.rmtree(clone_base, ignore_errors=True)
        raise
    return str(root), str(clone_base)


def run_vibe_coding_check(
    owner: str,
    repo: str,
    token: str,
    skip_llm: bool = False,
    repo_path: str | Path | None = None,
) -> tuple[str, str]:
    """Run vibecode check. Returns (critical_text, signals_text)."""
    if skip_llm:
        return "", ""
    root, clone_base = resolve_repo_root(owner, repo, token, repo_path, "vibe_qc_")
    try:
        critical, signals = run_vibe_agent(root)
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
    if skip_llm:
        return "", ""
    root, clone_base = resolve_repo_root(owner, repo, token, repo_path, "security_qc_")
    try:
        critical, signals = run_security_agent(root)
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
    if skip_llm:
        return "", ""
    root, clone_base = resolve_repo_root(owner, repo, token, repo_path, "prodq_qc_")
    try:
        critical, signals = run_production_agent(root)
        return "\n".join(critical), "\n".join(signals)
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
