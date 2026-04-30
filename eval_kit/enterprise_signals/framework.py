from __future__ import annotations

import logging
from typing import Any, Dict, Sequence

from eval_kit.enterprise_signals.base import (
    PRCollector,
    PRContext,
    RepoCollector,
    RepoContext,
)

logger = logging.getLogger(__name__)


def collect_for_pr(
    pr: PRContext, collectors: Sequence[PRCollector]
) -> Dict[str, Dict[str, Any]]:
    """Dispatch each collector against the given PRContext and merge results.

    Returns a dict keyed by collector.name. Exceptions from any single collector
    are caught, recorded as {"error": "<message>"}, and never abort the others.
    """
    result: Dict[str, Dict[str, Any]] = {}
    for collector in collectors:
        try:
            result[collector.name] = collector.collect(pr)
        except Exception as exc:
            logger.warning("PRCollector %r failed: %s", collector.name, exc)
            result[collector.name] = {"error": str(exc)}
    return result


def collect_for_repo(
    repo: RepoContext, collectors: Sequence[RepoCollector]
) -> Dict[str, Dict[str, Any]]:
    """Dispatch each repo-level collector and merge results.

    Same error-isolation contract as collect_for_pr.
    """
    result: Dict[str, Dict[str, Any]] = {}
    for collector in collectors:
        try:
            result[collector.name] = collector.collect(repo)
        except Exception as exc:
            logger.warning("RepoCollector %r failed: %s", collector.name, exc)
            result[collector.name] = {"error": str(exc)}
    return result
