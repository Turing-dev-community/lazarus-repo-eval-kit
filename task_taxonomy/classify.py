"""
Taxonomy classifier for coding tasks.

Uses an LLM (via any OpenAI-compatible API) with structured output to
classify coding tasks along three axes — domain, archetype, and horizon —
plus optional tags.  A lightweight rule-based pre-pass extracts signals
from git diffs so the LLM prompt is grounded in concrete data.

Typical usage::

    vendor-eval classify tasks.jsonl -o classified.jsonl
    vendor-eval classify tasks.csv   -o classified.csv --model grok-3

Default column names match the standard input format:

- ``problem_statement`` — task description (--query-col)
- ``repo`` — repository identifier (--repo-col)
- ``gold_patch`` — git diff / solution patch (--diff-col)
- ``language`` — primary programming language (--language-col)
"""

from __future__ import annotations

import csv
import json
import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Literal

from openai import OpenAI
from pydantic import BaseModel, Field

from .taxonomy import (
    DiffStats,
    build_taxonomy_prompt,
    infer_horizon,
    load_taxonomy,
    parse_diff,
)
from llm_client import call_llm

# ── Pydantic models for structured LLM output ──────────────────────────────


class DomainResult(BaseModel):
    """Primary and secondary domain."""

    primary: str = Field(description="Primary domain from the taxonomy")
    secondary: str = Field(description="Secondary domain (next-best fit)")
    subdomain_tags: list[str] = Field(default_factory=list, description="Subdomain tags")


class ArchetypeResult(BaseModel):
    """Task archetype."""

    archetype: Literal[
        "bootstrap", "build", "extend", "fix", "improve", "understand", "assure", "operate"
    ] = Field(description="Primary task archetype")
    confidence: Literal["high", "medium", "low"] = Field(description="Classification confidence")
    reasoning: str = Field(description="Brief explanation")


class HorizonResult(BaseModel):
    """Scope / horizon."""

    horizon: Literal["local", "repo", "system", "long_horizon"] = Field(description="Task scope")
    estimated_files: str = Field(description="Estimated files to touch")
    reasoning: str = Field(description="Brief explanation")


class LLMClassification(BaseModel):
    """Full structured output expected from the LLM."""

    domain: DomainResult
    archetype: ArchetypeResult
    horizon: HorizonResult
    vertical_tags: list[str] = Field(default_factory=list)
    constraint_tags: list[str] = Field(default_factory=list)
    ecosystem_tags: list[str] = Field(default_factory=list)
    llm_capability_tags: list[str] = Field(default_factory=list)
    summary: str = Field(description="One-sentence task summary")


# ── Classifier ──────────────────────────────────────────────────────────────


class TaxonomyClassifier:
    """Classify coding tasks using rule-based analysis + LLM.

    Parameters
    ----------
    api_key : str | None
        API key.  Falls back to ``$OPENAI_API_KEY``.
    base_url : str
        OpenAI-compatible base URL (default: OpenAI).
    model : str
        Model name to use for classification.
    taxonomy_path : str | Path | None
        Path to a custom taxonomy YAML.  ``None`` uses the built-in default.
    concurrency : int
        Max parallel LLM calls for batch classification.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
        taxonomy_path: str | Path | None = None,
        concurrency: int = 32,
    ):
        self.client = OpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY"),
            base_url=base_url,
        )
        self.model = model
        self.taxonomy = load_taxonomy(taxonomy_path)
        self.taxonomy_prompt = build_taxonomy_prompt(self.taxonomy)
        self.concurrency = concurrency

    # ── Single-item classification ──────────────────────────────────────

    def classify(
        self,
        query: str,
        repo: str = "",
        diff: str = "",
        problem_statement: str = "",
        language: str = "",
    ) -> dict[str, Any]:
        """Classify one coding task.

        Parameters
        ----------
        query : str
            The user's request / task description.
        repo : str
            Repository identifier (e.g. ``owner/repo``).
        diff : str
            Git diff / patch content.
        problem_statement : str
            Extended problem description.
        language : str
            Primary programming language hint.

        Returns
        -------
        dict
            JSON-serialisable classification result.
        """
        # 1. Rule-based pre-pass
        diff_stats: DiffStats | None = parse_diff(diff) if diff else None

        # 2. Build prompts
        system_prompt = self._system_prompt()
        user_prompt = self._user_prompt(
            query=query,
            repo=repo,
            diff=diff,
            problem_statement=problem_statement,
            language=language,
            diff_stats=diff_stats,
        )

        llm: LLMClassification = call_llm(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=self.model,
            client=self.client,
            temperature=0.1,
            response_format=LLMClassification,
        )

        # 4. Merge rule-based signals into LLM output
        return self._merge(llm, diff_stats)

    # ── Batch classification ────────────────────────────────────────────

    def classify_batch(
        self,
        items: list[dict[str, Any]],
        *,
        query_col: str = "problem_statement",
        repo_col: str = "repo",
        diff_col: str = "gold_patch",
        problem_col: str | None = None,
        language_col: str | None = "language",
    ) -> list[dict[str, Any]]:
        """Classify a list of items concurrently.

        Each item is a dict whose keys map to the column names above.
        Returns a list of classification dicts in the same order.
        """
        results: list[dict[str, Any] | None] = [None] * len(items)

        def _do(idx: int, item: dict) -> tuple[int, dict]:
            try:
                return idx, self.classify(
                    query=str(item.get(query_col, "") or ""),
                    repo=str(item.get(repo_col, "") or ""),
                    diff=str(item.get(diff_col, "") or ""),
                    problem_statement=str(item.get(problem_col, "") or "") if problem_col else "",
                    language=str(item.get(language_col, "") or "") if language_col else "",
                )
            except Exception as exc:
                print(f"[ERROR] Item {idx}: {exc}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                return idx, {"error": str(exc), "summary": f"Classification failed: {exc}"}

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(_do, i, item): i for i, item in enumerate(items)}
            for fut in as_completed(futures):
                idx, res = fut.result()
                results[idx] = res

        return results  # type: ignore[return-value]

    # ── Private helpers ─────────────────────────────────────────────────

    def _system_prompt(self) -> str:
        return f"""You are an expert software engineering task classifier.

Analyse the coding task and classify it according to this taxonomy:

{self.taxonomy_prompt}

GUIDELINES:
1. Domain — pick a PRIMARY and SECONDARY domain.  Include subdomain tags.
2. Archetype — pick exactly ONE (bootstrap / build / extend / fix / improve / understand / assure / operate).
3. Horizon — estimate scope (local / repo / system / long_horizon).
4. Tags — only include tags that clearly apply.
5. Summary — one sentence describing the task.

Be precise.  When uncertain, indicate lower confidence."""

    def _user_prompt(
        self,
        query: str,
        repo: str,
        diff: str,
        problem_statement: str,
        language: str,
        diff_stats: DiffStats | None,
    ) -> str:
        parts = [f"## Task Description\n{query}"]

        if problem_statement and problem_statement != query:
            preview = problem_statement[:4000]
            if len(problem_statement) > 4000:
                preview += f"\n... [{len(problem_statement)} chars total]"
            parts.append(f"\n## Problem Statement\n{preview}")

        if repo or language:
            ctx = ["## Context"]
            if repo:
                ctx.append(f"**Repo:** {repo}")
            if language:
                ctx.append(f"**Language:** {language}")
            parts.append("\n".join(ctx))

        # Rule-based signals (placed before the diff so the LLM sees them first)
        if diff_stats and diff_stats.files_touched > 0:
            horizon, reason = infer_horizon(diff_stats)
            sig = [
                "## Pre-computed Signals (from git diff)",
                f"- Files: {diff_stats.files_touched} ({diff_stats.files_added} added, "
                f"{diff_stats.files_modified} modified, {diff_stats.files_deleted} deleted)",
                f"- Lines: +{diff_stats.lines_added} / -{diff_stats.lines_removed}",
                f"- Languages: {', '.join(sorted(diff_stats.languages)) or 'unknown'}",
                f"- Inferred horizon: {horizon} ({reason})",
            ]
            if diff_stats.ecosystem_tags:
                sig.append(f"- Ecosystems: {', '.join(sorted(diff_stats.ecosystem_tags))}")
            if diff_stats.domain_hints:
                unique = list(set(diff_stats.domain_hints))[:5]
                sig.append(f"- Domain signals: {'; '.join(f'{d} > {s}' for d, s in unique)}")
            flags = []
            if diff_stats.has_tests:
                flags.append("tests")
            if diff_stats.has_ci:
                flags.append("CI/CD")
            if diff_stats.has_config:
                flags.append("config")
            if diff_stats.has_docs:
                flags.append("docs")
            if flags:
                sig.append(f"- Touches: {', '.join(flags)}")
            parts.append("\n".join(sig))

        if diff:
            diff_preview = diff[:8000]
            if len(diff) > 8000:
                diff_preview += f"\n... [truncated, {len(diff)} chars total]"
            parts.append(f"\n## Git Diff\n```diff\n{diff_preview}\n```")

        parts.append(
            "\nClassify this task.  Use the pre-computed signals as ground truth "
            "for file counts, languages, and ecosystem detection."
        )
        return "\n\n".join(parts)

    @staticmethod
    def _merge(llm: LLMClassification, diff_stats: DiffStats | None) -> dict[str, Any]:
        """Merge LLM output with rule-based signals into a flat dict."""
        eco = set(llm.ecosystem_tags)
        complexity: dict[str, Any] = {}
        rule_signals: dict[str, Any] = {}

        if diff_stats:
            eco.update(diff_stats.ecosystem_tags)
            complexity = {
                "files_touched": diff_stats.files_touched,
                "files_added": diff_stats.files_added,
                "files_modified": diff_stats.files_modified,
                "files_deleted": diff_stats.files_deleted,
                "lines_added": diff_stats.lines_added,
                "lines_removed": diff_stats.lines_removed,
                "languages": sorted(diff_stats.languages),
            }
            rule_signals = {
                "languages": sorted(diff_stats.languages),
                "has_tests": diff_stats.has_tests,
                "has_ci": diff_stats.has_ci,
                "has_config": diff_stats.has_config,
                "has_docs": diff_stats.has_docs,
            }

        # Ensure secondary is always set
        secondary = llm.domain.secondary or llm.domain.primary

        return {
            "domain_primary": llm.domain.primary,
            "domain_secondary": secondary,
            "subdomain_tags": llm.domain.subdomain_tags,
            "archetype": llm.archetype.archetype,
            "archetype_confidence": llm.archetype.confidence,
            "archetype_reasoning": llm.archetype.reasoning,
            "horizon": llm.horizon.horizon,
            "horizon_estimated_files": llm.horizon.estimated_files,
            "horizon_reasoning": llm.horizon.reasoning,
            "vertical_tags": llm.vertical_tags,
            "constraint_tags": llm.constraint_tags,
            "ecosystem_tags": sorted(eco),
            "llm_capability_tags": llm.llm_capability_tags,
            "complexity": complexity,
            "rule_based_signals": rule_signals,
            "summary": llm.summary,
        }


# ── File I/O helpers ────────────────────────────────────────────────────────


def read_input(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL or CSV file into a list of dicts.

    The format is auto-detected from the file extension:

    * ``.jsonl`` / ``.ndjson`` — one JSON object per line
    * ``.csv`` — comma-separated with a header row
    """
    suffix = path.suffix.lower()
    if suffix in (".jsonl", ".ndjson"):
        rows: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
    elif suffix == ".csv":
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            return list(reader)
    else:
        raise ValueError(f"Unsupported file format '{suffix}'. Use .jsonl, .ndjson, or .csv")


def write_output(rows: list[dict[str, Any]], path: Path) -> None:
    """Write classified results to JSONL or CSV (auto-detected by extension)."""
    suffix = path.suffix.lower()
    path.parent.mkdir(parents=True, exist_ok=True)

    if suffix in (".jsonl", ".ndjson"):
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, default=str) + "\n")
    elif suffix == ".csv":
        if not rows:
            path.write_text("")
            return
        fieldnames = list(rows[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                # Serialise nested structures to JSON strings for CSV cells
                flat = {}
                for k, v in row.items():
                    flat[k] = json.dumps(v, default=str) if isinstance(v, (dict, list)) else v
                writer.writerow(flat)
    else:
        raise ValueError(f"Unsupported output format '{suffix}'. Use .jsonl, .ndjson, or .csv")
