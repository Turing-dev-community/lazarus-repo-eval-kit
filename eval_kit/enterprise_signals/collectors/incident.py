"""Stage E1: Production incident signal collector (LLM-backed)."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel

from eval_kit.enterprise_signals.base import LLMCollector, PRContext
from eval_kit.llm_client import call_llm

_SYSTEM_PROMPT = """\
You are a code-review assistant that detects whether a GitHub pull request was \
created to resolve a production incident, outage, or urgent on-call escalation.

Analyze the supplied issue title, issue body, PR title, PR body, and commit \
messages. Return a structured response with:
  - has_incident_signal (bool): true if the PR addresses a production incident, \
    hotfix, outage, or urgent on-call escalation.
  - keywords_matched (list[str]): up to 5 terms or phrases from the text that \
    most strongly support your decision (empty list when false).
  - source (str): the field where the strongest evidence was found — one of: \
    "issue_title", "issue_body", "pr_title", "pr_body", "commit_messages", "none".
  - snippet (str): a verbatim excerpt (<=120 chars) of the strongest evidence \
    (empty string when has_incident_signal is false).

Keywords include but are not limited to: incident, outage, hotfix, on-call, \
p0, p1, sev-0, sev-1, rollback, revert, emergency, escalation, downtime, \
degraded, production bug, critical fix. Disambiguate intent — \
"incident response training" is NOT a production incident.\
"""

_USER_TEMPLATE = """\
Issue title: {issue_title}
Issue body: {issue_body}

PR title: {pr_title}
PR body: {pr_body}

Commit messages:
{commit_messages}
"""


class IncidentSignalOutput(BaseModel):
    has_incident_signal: bool
    keywords_matched: List[str]
    source: str
    snippet: str


class IncidentSignalCollector(LLMCollector):
    name = "incident_signal"
    requires_diff = False

    def _run(self, pr: PRContext) -> Dict[str, Any]:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _USER_TEMPLATE.format(
                    issue_title=pr.issue_title or "(none)",
                    issue_body=pr.issue_body or "(none)",
                    pr_title=pr.title,
                    pr_body=pr.body or "(none)",
                    commit_messages="\n".join(pr.commit_messages) or "(none)",
                ),
            },
        ]
        result: IncidentSignalOutput = call_llm(
            messages,
            response_format=IncidentSignalOutput,
            temperature=0,
        )
        return {
            "has_incident_signal": result.has_incident_signal,
            "evidence": {
                "keywords_matched": result.keywords_matched,
                "source": result.source,
                "snippet": result.snippet,
            },
        }
