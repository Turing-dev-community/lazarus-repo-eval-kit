"""Stage E14: PR description quality collector (Programmatic, per-PR)."""

from __future__ import annotations

import re
from typing import Any, Dict

from eval_kit.enterprise_signals.base import PRCollector, PRContext

_URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)
_HEADER_RE = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)


def _score(word_count: int, has_links: bool, has_headers: bool) -> float:
    """Return a 0.0–1.0 quality score.

    Weights: word_count 50%, has_links 25%, has_headers 25%.
    Word count saturates at 200 words (score 1.0 for that component).
    """
    word_score = min(word_count / 200, 1.0)
    link_score = 1.0 if has_links else 0.0
    header_score = 1.0 if has_headers else 0.0
    return round(0.5 * word_score + 0.25 * link_score + 0.25 * header_score, 4)


class PrDescriptionQualityCollector(PRCollector):
    name = "pr_description_quality"
    requires_diff = False

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        body = pr.body or ""
        words = body.split()
        word_count = len(words)
        links = _URL_RE.findall(body)
        link_count = len(links)
        has_links = link_count > 0
        has_headers = bool(_HEADER_RE.search(body))
        quality_score = _score(word_count, has_links, has_headers)
        return {
            "pr_description_quality_score": quality_score,
            "word_count": word_count,
            "has_links": has_links,
            "has_headers": has_headers,
            "link_count": link_count,
        }
