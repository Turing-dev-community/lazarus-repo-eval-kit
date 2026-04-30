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
from eval_kit.production_quality_check import _check_repo as _check_repo_production
from eval_kit.repo_evaluator_helpers import clone_repo
from eval_kit.security_check import _check_repo as _check_repo_security
from eval_kit.vibecode_check import _check_repo as _check_repo_vibecode

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


def _run_static_check(
    check_fn,
    owner: str,
    repo: str,
    token: str,
    skip_llm: bool,
    repo_path: str | Path | None,
    tmp_prefix: str,
) -> tuple[str, str]:
    existing = str(Path(repo_path).resolve()) if repo_path else None
    clone_base = ""
    if not existing:
        clone_base = tempfile.mkdtemp(prefix=tmp_prefix)
    try:
        result = check_fn(
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


def run_static_vibe_coding_check(
    owner: str,
    repo: str,
    token: str,
    skip_llm: bool = False,
    repo_path: str | Path | None = None,
) -> tuple[str, str]:
    """Run static (non-agent) vibecode check. Returns (critical_text, signals_text)."""
    return _run_static_check(
        _check_repo_vibecode, owner, repo, token, skip_llm, repo_path, "static_vibe_qc_"
    )


def run_static_security_check(
    owner: str,
    repo: str,
    token: str,
    skip_llm: bool = False,
    repo_path: str | Path | None = None,
) -> tuple[str, str]:
    """Run static (non-agent) security check. Returns (critical_text, signals_text)."""
    return _run_static_check(
        _check_repo_security,
        owner,
        repo,
        token,
        skip_llm,
        repo_path,
        "static_security_qc_",
    )


def run_static_production_quality_check(
    owner: str,
    repo: str,
    token: str,
    skip_llm: bool = False,
    repo_path: str | Path | None = None,
) -> tuple[str, str]:
    """Run static (non-agent) production quality check. Returns (critical_text, signals_text)."""
    return _run_static_check(
        _check_repo_production,
        owner,
        repo,
        token,
        skip_llm,
        repo_path,
        "static_prodq_qc_",
    )


def run_all_quality_checks(
    owner: str,
    repo: str,
    token: str,
    skip_llm: bool = False,
    repo_path: str | Path | None = None,
) -> dict[str, str]:
    """
    Run all quality checks (agent-based and static) and return a dict with 12 column values:
      vibe_coding_critical, vibe_coding_signals,
      security_check_critical, security_check_signals,
      production_quality_critical, production_quality_signals,
      static_vibe_coding_critical, static_vibe_coding_signals,
      static_security_check_critical, static_security_check_signals,
      static_production_quality_critical, static_production_quality_signals
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

    logger.info("Running static vibecode check for %s/%s ...", owner, repo)
    static_vibe_crit, static_vibe_sig = run_static_vibe_coding_check(
        owner, repo, token, skip_llm, repo_path=repo_path
    )

    logger.info("Running static security check for %s/%s ...", owner, repo)
    static_sec_crit, static_sec_sig = run_static_security_check(
        owner, repo, token, skip_llm, repo_path=repo_path
    )

    logger.info("Running static production quality check for %s/%s ...", owner, repo)
    static_prod_crit, static_prod_sig = run_static_production_quality_check(
        owner, repo, token, skip_llm, repo_path=repo_path
    )

    return {
        "vibe_coding_critical": vibe_crit,
        "vibe_coding_signals": vibe_sig,
        "security_check_critical": sec_crit,
        "security_check_signals": sec_sig,
        "production_quality_critical": prod_crit,
        "production_quality_signals": prod_sig,
        "static_vibe_coding_critical": static_vibe_crit,
        "static_vibe_coding_signals": static_vibe_sig,
        "static_security_check_critical": static_sec_crit,
        "static_security_check_signals": static_sec_sig,
        "static_production_quality_critical": static_prod_crit,
        "static_production_quality_signals": static_prod_sig,
    }
