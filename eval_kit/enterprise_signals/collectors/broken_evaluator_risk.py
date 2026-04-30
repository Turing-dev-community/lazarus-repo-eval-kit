"""Stage E10: Broken evaluators risk collector (LLM-backed, requires_diff=True)."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel

from eval_kit.enterprise_signals.base import LLMCollector, PRContext
from eval_kit.llm_client import call_llm

_SYSTEM_PROMPT = """\
You are a code-review assistant that detects whether a pull request is \
difficult or impossible to evaluate correctly by an automated code-quality \
rubric.

Analyse the diff (added/changed lines), PR title, and PR body. \
Return a structured response with:
  - has_broken_evaluator_risk (bool): true if this PR has characteristics \
    that would confuse or break a typical automated code-quality evaluator.
  - evidence (list[str]): up to 5 verbatim snippets (<=120 chars) or \
    observations that most strongly support the decision \
    (empty list when false).

A PR has broken-evaluator risk when it:
  - Is a revert / rollback of a previous commit (automated rubrics often \
    score deletions poorly even when the revert is correct).
  - Is a pure dependency bump with no logic change (version number change in \
    a lockfile / manifest is trivial but can look like a large diff).
  - Is a bulk rename / refactor with no semantic change (mass file renames, \
    s/OldName/NewName/ across the codebase).
  - Is a generated file update (protobuf, OpenAPI spec, migration, ORM schema).
  - Mixes unrelated concerns in a single PR making it hard to assess cohesion.
  - Contains intentionally broken or commented-out code that will be fixed in \
    a follow-up (WIP / stacked PR pattern).

A PR does NOT have this risk when it introduces genuine feature or bug-fix \
logic that a rubric can assess normally.\
"""

_USER_TEMPLATE = """\
PR title: {pr_title}
PR body: {pr_body}

Diff excerpt (first 8000 chars of added/removed lines):
{diff_excerpt}
"""

_MAX_DIFF_CHARS = 8000


class BrokenEvaluatorOutput(BaseModel):
    has_broken_evaluator_risk: bool
    evidence: List[str]


class BrokenEvaluatorRiskCollector(LLMCollector):
    name = "broken_evaluator_risk"
    requires_diff = True

    def _run(self, pr: PRContext) -> Dict[str, Any]:
        diff_excerpt = ""
        if pr.diff:
            diff_excerpt = pr.diff[:_MAX_DIFF_CHARS]

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
        result: BrokenEvaluatorOutput = call_llm(
            messages,
            response_format=BrokenEvaluatorOutput,
            temperature=0,
        )
        return {
            "has_broken_evaluator_risk": result.has_broken_evaluator_risk,
            "evidence": result.evidence,
        }
