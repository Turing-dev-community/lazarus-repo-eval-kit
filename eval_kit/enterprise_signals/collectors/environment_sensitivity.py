"""Stage E9: Environment sensitivity collector (Programmatic, per-PR).

Re-classified from LLM to Programmatic. Scans added diff lines for
patterns that indicate environment-dependent or non-deterministic behaviour.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from eval_kit.enterprise_signals.base import PRCollector, PRContext

_PATTERNS: List[tuple] = [
    # (label, compiled_regex)
    # Time / clock
    ("time.sleep", re.compile(r"\btime\.sleep\s*\(", re.IGNORECASE)),
    ("datetime.now", re.compile(r"\bdatetime\.(?:utc)?now\s*\(", re.IGNORECASE)),
    ("Date.now", re.compile(r"\bDate\.now\s*\(", re.IGNORECASE)),
    ("new Date()", re.compile(r"\bnew\s+Date\s*\(", re.IGNORECASE)),
    ("time.time()", re.compile(r"\btime\.time\s*\(", re.IGNORECASE)),
    ("SystemClock", re.compile(r"\bSystemClock\b", re.IGNORECASE)),
    ("LocalDateTime.now", re.compile(r"\bLocalDateTime\.now\s*\(", re.IGNORECASE)),
    ("Instant.now", re.compile(r"\bInstant\.now\s*\(", re.IGNORECASE)),
    # Test ordering / isolation
    ("pytest.mark.order", re.compile(r"@pytest\.mark\.order\b", re.IGNORECASE)),
    (
        "pytest.mark.run",
        re.compile(r"@pytest\.mark\.(?:run|first|last|dependency)\b", re.IGNORECASE),
    ),
    ("@TestMethodOrder", re.compile(r"@TestMethodOrder\b", re.IGNORECASE)),
    ("@Order", re.compile(r"@Order\s*\(", re.IGNORECASE)),
    # Clock freezing / mocking
    ("freeze_time", re.compile(r"\bfreeze_time\b", re.IGNORECASE)),
    ("freezegun", re.compile(r"\bfreezegun\b", re.IGNORECASE)),
    (
        "faketime",
        re.compile(
            r"\bfaketime\b|\bfake_clock\b|\bMockClock\b|\bTestClock\b", re.IGNORECASE
        ),
    ),
    (
        "useFakeTimers",
        re.compile(
            r"\buseFakeTimers\b|\bjest\.setSystemTime\b|\bvi\.setSystemTime\b",
            re.IGNORECASE,
        ),
    ),
    # Random without seed
    (
        "random.random",
        re.compile(r"\brandom\.random\s*\(|\brandom\.randint\s*\(", re.IGNORECASE),
    ),
    ("Math.random", re.compile(r"\bMath\.random\s*\(", re.IGNORECASE)),
    ("np.random", re.compile(r"\bnp\.random\b", re.IGNORECASE)),
    # Platform / env checks
    ("sys.platform", re.compile(r"\bsys\.platform\b", re.IGNORECASE)),
    ("os.environ", re.compile(r"\bos\.environ\b|os\.getenv\s*\(", re.IGNORECASE)),
    ("process.env", re.compile(r"process\.env\.", re.IGNORECASE)),
    ("platform.system", re.compile(r"\bplatform\.system\s*\(", re.IGNORECASE)),
]


def _scan_diff(diff: str) -> List[str]:
    matched: List[str] = []
    seen: set = set()
    for line in diff.splitlines():
        if not line.startswith("+"):
            continue
        for label, pat in _PATTERNS:
            if pat.search(line) and label not in seen:
                seen.add(label)
                matched.append(label)
    return matched


class EnvironmentSensitivityCollector(PRCollector):
    name = "environment_sensitivity"
    requires_diff = True

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        matched: List[str] = []
        if pr.diff:
            matched = _scan_diff(pr.diff)
        return {
            "has_environment_sensitivity": bool(matched),
            "matched_patterns": matched,
        }
