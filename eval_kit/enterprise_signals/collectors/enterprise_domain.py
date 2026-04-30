"""Stage E2: Domain complexity collector (LLM-backed)."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel

from eval_kit.enterprise_signals.base import LLMCollector, PRContext
from eval_kit.llm_client import call_llm

_SYSTEM_PROMPT = """\
You are a code-review assistant that detects whether a GitHub pull request \
touches a specialised enterprise domain that requires deep domain expertise.

Analyse the issue title, issue body, PR title, PR body, commit messages, and \
changed file paths. Return a structured response with:
  - has_enterprise_domain (bool): true if the PR clearly involves one or more \
    specialised enterprise domains listed below.
  - matched_domains (list[str]): domain labels from the list below that apply \
    (empty list when false). Use the canonical label names exactly as listed.
  - source (str): the field that contains the strongest evidence — one of: \
    "issue_title", "issue_body", "pr_title", "pr_body", "commit_messages", \
    "file_paths", "none".
  - snippet (str): a verbatim excerpt (<=120 chars) of the strongest evidence \
    (empty string when has_enterprise_domain is false).

Recognised enterprise domains and example signals:
  - fintech: payments, trading, clearing, settlement, PCI-DSS, SWIFT, FIX \
    protocol, KYC/AML, IBAN, ledger, reconciliation, margin, collateral
  - healthcare: HIPAA, HL7/FHIR, EHR, ICD codes, PHI, clinical trials, \
    medical devices, DICOM, patient data
  - government / public-sector: FISMA, FedRAMP, ITAR, security clearance, \
    procurement, civil-service, electoral, tax authority
  - legal / compliance: e-discovery, contract lifecycle, GDPR enforcement, \
    SOX, Basel, data-residency, regulatory reporting
  - insurance: policy underwriting, actuarial, claims processing, reinsurance, \
    loss modelling
  - telecommunications: SS7, SIP/VoIP, CDR billing, MVNO, spectrum management, \
    carrier interconnect
  - energy / utilities: SCADA, grid management, smart metering, oil-well \
    telemetry, energy trading, NERC-CIP
  - defence / aerospace: ITAR, MIL-SPEC, avionics, satellite telemetry, \
    classified systems

Only return true when the domain expertise is clearly central to the PR, not \
merely incidental (e.g. a generic REST API that happens to be used by a bank \
is NOT fintech).\
"""

_USER_TEMPLATE = """\
Issue title: {issue_title}
Issue body: {issue_body}

PR title: {pr_title}
PR body: {pr_body}

Commit messages:
{commit_messages}

Changed file paths:
{file_paths}
"""


class EnterpriseDomainOutput(BaseModel):
    has_enterprise_domain: bool
    matched_domains: List[str]
    source: str
    snippet: str


class EnterpriseDomainCollector(LLMCollector):
    name = "enterprise_domain"
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
                    file_paths="\n".join(pr.changed_files) or "(none)",
                ),
            },
        ]
        result: EnterpriseDomainOutput = call_llm(
            messages,
            response_format=EnterpriseDomainOutput,
            temperature=0,
        )
        return {
            "has_enterprise_domain": result.has_enterprise_domain,
            "matched_domains": result.matched_domains,
            "evidence": {
                "source": result.source,
                "snippet": result.snippet,
            },
        }
