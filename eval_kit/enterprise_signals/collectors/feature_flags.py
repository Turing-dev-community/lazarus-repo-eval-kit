"""Stage E15: Feature flagging collector (Programmatic, per-PR, requires_diff=True)."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from eval_kit.enterprise_signals.base import PRCollector, PRContext

_SDK_PATTERNS: List[tuple] = [
    (
        "launchdarkly",
        re.compile(r"\blaunchdarkly\b|ldclient|ld-client|@launchdarkly", re.IGNORECASE),
    ),
    (
        "unleash",
        re.compile(r"\bunleash\b|unleash-client|getUnleashInstance", re.IGNORECASE),
    ),
    ("flagsmith", re.compile(r"\bflagsmith\b", re.IGNORECASE)),
    ("optimizely", re.compile(r"\boptimizely\b|@optimizely", re.IGNORECASE)),
    ("growthbook", re.compile(r"\bgrowthbook\b|@growthbook", re.IGNORECASE)),
    (
        "split_io",
        re.compile(r"\bsplit\.io\b|\bSplitFactory\b|@splitsoftware", re.IGNORECASE),
    ),
    ("statsig", re.compile(r"\bstatsig\b", re.IGNORECASE)),
    (
        "posthog",
        re.compile(
            r"\bposthog\b|posthog\.feature_enabled|posthog\.isFeatureEnabled",
            re.IGNORECASE,
        ),
    ),
    ("configcat", re.compile(r"\bconfigcat\b", re.IGNORECASE)),
    ("flipt", re.compile(r"\bflipt\b", re.IGNORECASE)),
    ("flipper", re.compile(r"\bFlipper\b|flipper-ruby", re.IGNORECASE)),
    ("rollout", re.compile(r"\brollout\b|rox\.server", re.IGNORECASE)),
    # Generic feature flag patterns
    (
        "generic_feature_flag",
        re.compile(
            r"\bfeature_flag\b|\bfeatureFlag\b|\bfeature_enabled\b|\bisFeatureEnabled\b"
            r"|\bgetFeature\b|\bfeature_toggle\b|\bfeatureToggle\b"
            r"|\bFEATURE_FLAG\b|\bFF_\w+\b",
            re.IGNORECASE,
        ),
    ),
]


def _scan_diff(diff: str) -> List[str]:
    matched: List[str] = []
    seen: set = set()
    for line in diff.splitlines():
        if not line.startswith("+"):
            continue
        for sdk_name, pat in _SDK_PATTERNS:
            if pat.search(line) and sdk_name not in seen:
                seen.add(sdk_name)
                matched.append(sdk_name)
    return matched


class FeatureFlagsCollector(PRCollector):
    name = "feature_flags"
    requires_diff = True

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        matched: List[str] = []
        if pr.diff:
            matched = _scan_diff(pr.diff)
        return {
            "has_feature_flags": bool(matched),
            "matched_sdks": matched,
        }
