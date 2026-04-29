"""Stage E7: Adjacent artifacts collector (Programmatic, per-PR).

Detects links to external project-management and design artifacts in PR
body and issue body: Jira, Linear, Notion, Confluence, Figma, Miro, Asana,
GitHub Issues cross-repo, Trello, etc.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from eval_kit.enterprise_signals.base import PRCollector, PRContext

# (type_label, url_regex)
_ARTIFACT_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (
        "jira",
        re.compile(
            r"https?://[a-zA-Z0-9._-]+\.atlassian\.net/browse/[A-Z]+-\d+", re.IGNORECASE
        ),
    ),
    (
        "confluence",
        re.compile(r"https?://[a-zA-Z0-9._-]+\.atlassian\.net/wiki/", re.IGNORECASE),
    ),
    (
        "linear",
        re.compile(r"https?://linear\.app/[^/\s]+/issue/[A-Z]+-\d+", re.IGNORECASE),
    ),
    (
        "notion",
        re.compile(r"https?://(?:www\.)?notion\.(?:so|site)/[^\s]+", re.IGNORECASE),
    ),
    (
        "figma",
        re.compile(
            r"https?://(?:www\.)?figma\.com/(?:file|design|proto)/[^\s]+", re.IGNORECASE
        ),
    ),
    ("miro", re.compile(r"https?://miro\.com/app/board/[^\s]+", re.IGNORECASE)),
    ("asana", re.compile(r"https?://app\.asana\.com/[^\s]+", re.IGNORECASE)),
    ("trello", re.compile(r"https?://trello\.com/c/[^\s]+", re.IGNORECASE)),
    (
        "github_issue",
        re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/issues/\d+", re.IGNORECASE),
    ),
    (
        "github_pr",
        re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE),
    ),
    (
        "gitlab_issue",
        re.compile(r"https?://gitlab\.com/[^\s]+/-/issues/\d+", re.IGNORECASE),
    ),
    (
        "shortcut",
        re.compile(r"https?://app\.shortcut\.com/[^/\s]+/story/\d+", re.IGNORECASE),
    ),
    (
        "monday",
        re.compile(r"https?://[a-zA-Z0-9._-]+\.monday\.com/boards/\d+", re.IGNORECASE),
    ),
    (
        "pagerduty",
        re.compile(
            r"https?://[a-zA-Z0-9._-]+\.pagerduty\.com/incidents/[^\s]+", re.IGNORECASE
        ),
    ),
]


def _extract_links(text: Optional[str]) -> List[Dict[str, str]]:
    if not text:
        return []
    links = []
    seen_urls: set = set()
    for artifact_type, pattern in _ARTIFACT_PATTERNS:
        for m in pattern.finditer(text):
            url = m.group(0)
            if url not in seen_urls:
                seen_urls.add(url)
                links.append({"type": artifact_type, "url": url})
    return links


class AdjacentArtifactsCollector(PRCollector):
    name = "adjacent_artifacts"
    requires_diff = False

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        links: List[Dict[str, str]] = []
        for text in [pr.body, pr.issue_body]:
            links.extend(_extract_links(text))

        seen_urls: set = set()
        deduped = []
        for link in links:
            if link["url"] not in seen_urls:
                seen_urls.add(link["url"])
                deduped.append(link)

        return {
            "has_external_artifacts": bool(deduped),
            "links": deduped,
        }
