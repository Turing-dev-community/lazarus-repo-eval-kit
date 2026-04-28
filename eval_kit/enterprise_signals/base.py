from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class PRContext:
    number: int
    title: str
    body: str
    issue_title: Optional[str]
    issue_body: Optional[str]
    commit_messages: List[str]
    changed_files: List[str]
    # Populated from already-fetched full_patch only when a registered collector
    # has requires_diff=True; None otherwise.
    diff: Optional[str]
    repo_path: Path
    primary_language: Optional[str]


@dataclass(frozen=True)
class RepoContext:
    repo_path: Path
    owner: Optional[str]
    repo_name: Optional[str]
    primary_language: Optional[str]


class PRCollector:
    """Protocol for per-PR data collectors."""

    name: str
    requires_diff: bool

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        raise NotImplementedError


class RepoCollector:
    """Protocol for repo-level data collectors."""

    name: str

    def collect(self, repo: RepoContext) -> Dict[str, Any]:
        raise NotImplementedError
