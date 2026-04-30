"""Stage E12: Vendor integration / adapter shims collector (LLM-backed, requires_diff=True)."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel

from eval_kit.enterprise_signals.base import LLMCollector, PRContext
from eval_kit.llm_client import call_llm

_SYSTEM_PROMPT = """\
You are a code-review assistant that detects whether a pull request adds or \
modifies vendor integration code — adapter shims, third-party SDK wrappers, \
API client configurations, or bespoke glue code that bridges internal systems \
to external vendor services.

Analyse the diff (added lines), PR title, and PR body. Return a structured \
response with:
  - has_vendor_integration (bool): true if the diff contains code that \
    integrates with, wraps, or adapts an external vendor's SDK, API, or \
    protocol in a way that requires understanding the vendor's specifics.
  - evidence (list[str]): up to 5 verbatim code snippets (<=120 chars each) \
    from added diff lines that most strongly support the decision \
    (empty list when false).

Return true when the PR:
  - Adds an import of a named third-party SDK and calls vendor-specific \
    methods (not just generic HTTP).
  - Implements a wrapper/adapter class around a vendor API.
  - Configures authentication, endpoint URLs, or API keys for a named vendor.
  - Handles vendor-specific response formats, error codes, or retry logic.
  - Translates between internal models and a vendor's data schema.

Return false when:
  - The change is purely internal with no external vendor coupling.
  - The only vendor reference is a generic HTTP call with no named SDK.
  - The change only updates a version number in a manifest.\
"""

_USER_TEMPLATE = """\
PR title: {pr_title}
PR body: {pr_body}

Diff (added lines, up to 8000 chars):
{diff_excerpt}
"""

_MAX_DIFF_CHARS = 8000


class VendorIntegrationOutput(BaseModel):
    has_vendor_integration: bool
    evidence: List[str]


class VendorIntegrationCollector(LLMCollector):
    name = "vendor_integration"
    requires_diff = True

    def _run(self, pr: PRContext) -> Dict[str, Any]:
        diff_excerpt = ""
        if pr.diff:
            added = [line for line in pr.diff.splitlines() if line.startswith("+")]
            diff_excerpt = "\n".join(added)[:_MAX_DIFF_CHARS]

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _USER_TEMPLATE.format(
                    pr_title=pr.title,
                    pr_body=pr.body or "(none)",
                    diff_excerpt=diff_excerpt or "(none)",
                ),
            },
        ]
        result: VendorIntegrationOutput = call_llm(
            messages,
            response_format=VendorIntegrationOutput,
            temperature=0,
        )
        return {
            "has_vendor_integration": result.has_vendor_integration,
            "evidence": result.evidence,
        }
