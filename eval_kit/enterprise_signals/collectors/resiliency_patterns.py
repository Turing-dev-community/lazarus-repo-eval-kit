"""Stage E16: Resiliency patterns collector (Programmatic, per-PR, requires_diff=True)."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from eval_kit.enterprise_signals.base import PRCollector, PRContext

_LIB_PATTERNS: List[tuple] = [
    # Python
    ("tenacity", re.compile(r"\btenacity\b|@retry|retry_with_backoff", re.IGNORECASE)),
    (
        "circuitbreaker",
        re.compile(r"\bcircuitbreaker\b|circuit_breaker|CircuitBreaker", re.IGNORECASE),
    ),
    ("pybreaker", re.compile(r"\bpybreaker\b", re.IGNORECASE)),
    ("stamina", re.compile(r"\bstamina\b", re.IGNORECASE)),
    ("backoff", re.compile(r"\bbackoff\b|@backoff\.", re.IGNORECASE)),
    # JVM
    (
        "resilience4j",
        re.compile(r"\bresilience4j\b|Retry\.of|CircuitBreaker\.of", re.IGNORECASE),
    ),
    ("hystrix", re.compile(r"\bhystrix\b|@HystrixCommand", re.IGNORECASE)),
    ("failsafe", re.compile(r"\bfailsafe\b|Failsafe\.with", re.IGNORECASE)),
    ("sentinel", re.compile(r"\bsentinel\b|SentinelGatewayFilter", re.IGNORECASE)),
    # .NET
    ("polly", re.compile(r"\bPolly\b|Policy\.Handle|WaitAndRetry", re.IGNORECASE)),
    # Go
    (
        "go_retry",
        re.compile(
            r"\bavast/retry-go\b|cenkalti/backoff|sethvargo/go-retry", re.IGNORECASE
        ),
    ),
    # JS / TS
    ("async_retry", re.compile(r"\basync-retry\b|p-retry\b|retry\.js", re.IGNORECASE)),
    ("cockatiel", re.compile(r"\bcockatiel\b", re.IGNORECASE)),
    # Generic patterns
    (
        "generic_retry",
        re.compile(
            r"\bretry\s*\(\s*\d+|\bmax_retries\b|\bmaxRetries\b"
            r"|\bexponential.backoff\b|\bbackoff_factor\b",
            re.IGNORECASE,
        ),
    ),
    ("bulkhead", re.compile(r"\bbulkhead\b|BulkheadConfig", re.IGNORECASE)),
    (
        "timeout_pattern",
        re.compile(
            r"\bReadTimeout\b|\bConnectTimeout\b|\bsocket_timeout\b|\brequests\.Timeout\b",
            re.IGNORECASE,
        ),
    ),
    (
        "dead_letter",
        re.compile(r"\bdead.letter\b|\bDLQ\b|\bdead_queue\b", re.IGNORECASE),
    ),
]


def _scan_diff(diff: str) -> List[str]:
    matched: List[str] = []
    seen: set = set()
    for line in diff.splitlines():
        if not line.startswith("+"):
            continue
        for lib_name, pat in _LIB_PATTERNS:
            if pat.search(line) and lib_name not in seen:
                seen.add(lib_name)
                matched.append(lib_name)
    return matched


class ResiliencyPatternsCollector(PRCollector):
    name = "resiliency_patterns"
    requires_diff = True

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        matched: List[str] = []
        if pr.diff:
            matched = _scan_diff(pr.diff)
        return {
            "has_resiliency_patterns": bool(matched),
            "matched_libraries": matched,
        }
