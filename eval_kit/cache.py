from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

import diskcache

from eval_kit import __version__ as _EVAL_KIT_VERSION

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "lazarus-eval-kit"
DEFAULT_TTL = 2 * 24 * 60 * 60  # 2 days

logger = logging.getLogger(__name__)
T = TypeVar("T")


class EvalCache:
    """Disk-backed cache for the evaluation pipeline.

    read_enabled=False (--no-cache): get() always returns None, set() still writes.
    All methods are safe to call unconditionally — no guards needed at call sites.
    """

    def __init__(
        self,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        ttl: int = DEFAULT_TTL,
        read_enabled: bool = True,
        enabled: bool = True,
        version: str = _EVAL_KIT_VERSION,
    ) -> None:
        # enabled=False: complete no-op — no disk access, no reads, no writes.
        # read_enabled=False (--no-cache): writes still happen, reads always miss.
        self.enabled = enabled
        self.read_enabled = read_enabled and enabled
        self.ttl = ttl
        self._version = version
        self._cache = None
        if enabled:
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache = diskcache.Cache(str(cache_dir))

    def _prefix(self, key: str) -> str:
        return f"v{self._version}:{key}"

    # ── Key builders ──────────────────────────────────────────────────────

    @staticmethod
    def repo_key(platform: str, owner: str, repo_name: str) -> str:
        return f"repo:{platform}:{owner}/{repo_name}"

    @staticmethod
    def pr_page_key(
        platform: str,
        owner: str,
        repo_name: str,
        cursor: Optional[str],
        page_size: int,
        start_date: Optional[str] = None,
    ) -> str:
        return (
            f"pr_page:{platform}:{owner}/{repo_name}"
            f":{cursor or 'start'}:{page_size}:{start_date or 'none'}"
        )

    @staticmethod
    def enterprise_pr_key(
        platform: str, owner: str, repo_name: str, pr_number: int
    ) -> str:
        return f"enterprise_pr:{platform}:{owner}/{repo_name}:{pr_number}"

    @staticmethod
    def pr_patch_key(platform: str, owner: str, repo_name: str, pr_number: int) -> str:
        return f"pr_patch:{platform}:{owner}/{repo_name}:{pr_number}"

    @staticmethod
    def f2p_key(
        platform: str,
        owner: str,
        repo_name: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> str:
        return f"f2p:{platform}:{owner}/{repo_name}:{pr_number}:{base_sha}:{head_sha}"

    @staticmethod
    def rubric_key(
        platform: str,
        owner: str,
        repo_name: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> str:
        return (
            f"rubric:{platform}:{owner}/{repo_name}:{pr_number}:{base_sha}:{head_sha}"
        )

    @staticmethod
    def quality_checks_key(
        platform: str, owner: str, repo_name: str, skip_llm: bool, head_sha: str = ""
    ) -> str:
        return f"quality:{platform}:{owner}/{repo_name}:{'nollm' if skip_llm else 'llm'}:{head_sha}"

    @staticmethod
    def taxonomy_key(
        platform: str,
        owner: str,
        repo_name: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> str:
        return (
            f"taxonomy:{platform}:{owner}/{repo_name}:{pr_number}:{base_sha}:{head_sha}"
        )

    @staticmethod
    def fairness_key(
        platform: str,
        owner: str,
        repo_name: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> str:
        return (
            f"fairness:{platform}:{owner}/{repo_name}:{pr_number}:{base_sha}:{head_sha}"
        )

    # ── Core API ──────────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        """Return cached value or None. Always returns None when read_enabled=False."""
        if not self.read_enabled or self._cache is None:
            return None
        try:
            value = self._cache.get(self._prefix(key), default=None)
            if value is None:
                return None
            logger.debug("Cache HIT: %s", key)
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    pass
            return value
        except Exception:
            logger.debug("Cache read error for %s", key, exc_info=True)
            return None

    def set(self, key: str, value: Any) -> None:
        """Write value to cache. Active only when enabled=True."""
        if not self.enabled or self._cache is None:
            return
        try:
            stored = (
                json.dumps(value, default=str)
                if isinstance(value, (dict, list))
                else value
            )
            self._cache.set(self._prefix(key), stored, expire=self.ttl)
            logger.debug("Cache SET: %s", key)
        except Exception:
            logger.debug("Cache write error for %s", key, exc_info=True)

    def get_or_compute(self, key: str, fn: Callable[[], T]) -> T:
        """Return cached value if present, otherwise call fn(), cache the result, return it.

        fn() must return a JSON-serialisable value (dict, list, or str).
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        result = fn()
        self.set(key, result)
        return result

    def invalidate(self, key: str) -> None:
        if self._cache is not None:
            self._cache.delete(self._prefix(key))

    def close(self) -> None:
        if self._cache is not None:
            self._cache.close()
