"""Stage E3: Cross-service boundary collector (Programmatic)."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from eval_kit.enterprise_signals.base import PRCollector, PRContext

# File paths that suggest external-service interaction
_FILE_PATTERNS: List[re.Pattern] = [
    re.compile(r, re.IGNORECASE)
    for r in [
        r"client[s]?[_/]",
        r"[_/]client\.",
        r"adapter[s]?[_/]",
        r"[_/]adapter\.",
        r"connector[s]?[_/]",
        r"[_/]connector\.",
        r"gateway[s]?[_/]",
        r"[_/]gateway\.",
        r"integrat",
        r"webhook",
        r"api[_/]",
        r"[_/]api\.",
        r"grpc",
        r"proto",
        r"thrift",
        r"graphql",
        r"third.?party",
        r"vendor[s]?[_/]",
        r"external",
    ]
]

# Import / call patterns that indicate outbound network calls
_IMPORT_PATTERNS: List[re.Pattern] = [
    re.compile(r, re.IGNORECASE)
    for r in [
        # Python HTTP
        r"\brequests\b",
        r"\bhttpx\b",
        r"\baiohttp\b",
        r"\burllib\b",
        r"\burllib3\b",
        r"\bpycurl\b",
        # Python gRPC / messaging
        r"\bgrpc\b",
        r"\bpika\b",
        r"\bcelery\b",
        r"\bkombu\b",
        r"\bkafka\b",
        r"\bconfluent_kafka\b",
        # Python cloud SDKs
        r"\bboto3\b",
        r"\bbotocore\b",
        r"\bgoogle\.cloud\b",
        r"\bazure\b",
        # JS / TS HTTP
        r"\baxios\b",
        r"\bfetch\b",
        r"\bnode-fetch\b",
        r"\bgot\b",
        r"\bsuperagent\b",
        r"\bky\b",
        r"\bundici\b",
        # JS / TS messaging
        r"\bamqplib\b",
        r"\bws\b",
        r"\bsocket\.io\b",
        r"\b@grpc/",
        # General REST / RPC markers
        r"HttpClient",
        r"RestTemplate",
        r"WebClient",
        r"OkHttpClient",
        r"Retrofit",
        r"Feign",
        r"\brestsharp\b",
        r"\bHttpClient\b",
    ]
]


def _match_file_patterns(changed_files: List[str]) -> List[str]:
    matched = []
    for path in changed_files:
        for pat in _FILE_PATTERNS:
            if pat.search(path):
                matched.append(path)
                break
    return matched


def _match_import_patterns(diff: str) -> List[str]:
    """Return deduplicated import tokens found in added diff lines."""
    found: List[str] = []
    seen: set = set()
    for line in diff.splitlines():
        if not line.startswith("+"):
            continue
        for pat in _IMPORT_PATTERNS:
            m = pat.search(line)
            if m:
                token = m.group(0)
                key = token.lower()
                if key not in seen:
                    seen.add(key)
                    found.append(token)
    return found


class ExternalConnectionCollector(PRCollector):
    name = "external_connection"
    requires_diff = True

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        matched_files = _match_file_patterns(pr.changed_files)
        matched_imports: List[str] = []
        if pr.diff:
            matched_imports = _match_import_patterns(pr.diff)
        has_external = bool(matched_files or matched_imports)
        return {
            "has_external_connection": has_external,
            "matched_files": matched_files,
            "matched_imports": matched_imports,
        }
