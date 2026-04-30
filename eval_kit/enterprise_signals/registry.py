"""Collector registry.

Collectors register themselves here so the framework loop can discover them
without the call sites needing to import each collector individually.

Stage E0: registry is empty. No behavior change until E1 registers a collector.
"""

from __future__ import annotations

from typing import List

from eval_kit.enterprise_signals.base import PRCollector, RepoCollector

_PR_COLLECTORS: List[PRCollector] = []
_REPO_COLLECTORS: List[RepoCollector] = []


def register_pr_collector(collector: PRCollector) -> None:
    _PR_COLLECTORS.append(collector)


def register_repo_collector(collector: RepoCollector) -> None:
    _REPO_COLLECTORS.append(collector)


def get_pr_collectors() -> List[PRCollector]:
    return list(_PR_COLLECTORS)


def get_repo_collectors() -> List[RepoCollector]:
    return list(_REPO_COLLECTORS)


def reset_collectors() -> None:
    """Clear all registered collectors. Intended for test isolation only."""
    _PR_COLLECTORS.clear()
    _REPO_COLLECTORS.clear()
