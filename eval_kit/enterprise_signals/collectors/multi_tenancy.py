"""Stage E6: Multi-tenancy & permission logic collector (LLM-backed, requires_diff=True)."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel

from eval_kit.enterprise_signals.base import LLMCollector, PRContext
from eval_kit.llm_client import call_llm

_SYSTEM_PROMPT = """\
You are a code-review assistant that detects whether a pull request introduces \
or modifies multi-tenancy or permission / authorisation logic.

Analyse the supplied diff (added lines only), PR title, and PR body. \
Return a structured response with:
  - has_multi_tenancy_logic (bool): true if the diff contains code that \
    implements or changes any of: tenant isolation, per-tenant data scoping, \
    row-level security, RBAC/ABAC logic, permission checks, access-control \
    lists, capability flags, organisation-scoped queries, or similar patterns.
  - evidence (list[str]): up to 5 verbatim code snippets (<=120 chars each) \
    from added diff lines that most strongly support the decision \
    (empty list when false).

Examples that should be true:
  - Filtering queries by `tenant_id` or `org_id`
  - Checking `user.has_permission("admin:write")` or `can?(:edit, resource)`
  - Implementing a `require_permission` decorator / middleware
  - Adding role checks like `if user.role in (ADMIN, MANAGER):`
  - Row-level security policies in SQL

Examples that should be false:
  - Generic CRUD with no tenant/permission context
  - Adding a user model with no role/permission fields
  - Logging, metrics, or UI-only changes\
"""

_USER_TEMPLATE = """\
PR title: {pr_title}
PR body: {pr_body}

Diff (added lines):
{diff_excerpt}
"""

_MAX_DIFF_CHARS = 8000


class MultiTenancyOutput(BaseModel):
    has_multi_tenancy_logic: bool
    evidence: List[str]


class MultiTenancyCollector(LLMCollector):
    name = "multi_tenancy"
    requires_diff = True

    def _run(self, pr: PRContext) -> Dict[str, Any]:
        diff_excerpt = ""
        if pr.diff:
            added_lines = [
                line for line in pr.diff.splitlines() if line.startswith("+")
            ]
            diff_excerpt = "\n".join(added_lines)[:_MAX_DIFF_CHARS]

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
        result: MultiTenancyOutput = call_llm(
            messages,
            response_format=MultiTenancyOutput,
            temperature=0,
        )
        return {
            "has_multi_tenancy_logic": result.has_multi_tenancy_logic,
            "evidence": result.evidence,
        }
