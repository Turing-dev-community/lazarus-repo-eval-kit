"""Stage E7: Adjacent artifacts collector (Programmatic, per-PR).

Detects links to external project-management and design artifacts in PR
body and issue body: Jira, Linear, Notion, Confluence, Figma, Miro, Asana,
GitHub Issues cross-repo, Trello, etc.

For GitHub issue/PR links, only cross-repo links are counted.  A link that
points back to the same ``owner/repo`` as the PR being analysed is routine
GitHub workflow (e.g. ``Fixes #123`` auto-expanded) and carries no enterprise
signal.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from eval_kit.enterprise_signals.base import PRCollector, PRContext

# (type_label, url_regex)
# GitHub patterns capture owner and repo as named groups so the collector can
# filter out same-repo references.
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
        re.compile(
            r"https?://github\.com/(?P<gh_owner>[^/\s]+)/(?P<gh_repo>[^/\s]+)/issues/\d+",
            re.IGNORECASE,
        ),
    ),
    (
        "github_pr",
        re.compile(
            r"https?://github\.com/(?P<gh_owner>[^/\s]+)/(?P<gh_repo>[^/\s]+)/pull/\d+",
            re.IGNORECASE,
        ),
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

# Artifact types whose URLs embed owner/repo and must be cross-repo to count.
_GITHUB_TYPES = {"github_issue", "github_pr"}


def _is_same_repo(m: re.Match, owner: Optional[str], repo_name: Optional[str]) -> bool:
    """Return True when a GitHub URL points to the PR's own repo."""
    if not owner or not repo_name:
        return False
    try:
        return (
            m.group("gh_owner").lower() == owner.lower()
            and m.group("gh_repo").lower() == repo_name.lower()
        )
    except IndexError:
        return False


def _extract_links(
    text: Optional[str],
    owner: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> List[Dict[str, str]]:
    if not text:
        return []
    links = []
    seen_urls: set = set()
    for artifact_type, pattern in _ARTIFACT_PATTERNS:
        for m in pattern.finditer(text):
            if artifact_type in _GITHUB_TYPES and _is_same_repo(m, owner, repo_name):
                continue
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
            links.extend(_extract_links(text, owner=pr.owner, repo_name=pr.repo_name))

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
