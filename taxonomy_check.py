from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)
from task_taxonomy.classify import TaxonomyClassifier  # noqa: E402

logger = logging.getLogger(__name__)


_README_CANDIDATES = [
    "README.md",
    "README.MD",
    "readme.md",
    "Readme.md",
    "README.rst",
    "README",
]


def _read_readme(repo_path: str | Path) -> str:
    """Read the first README found in the repo root (up to 4000 chars)."""
    root = Path(repo_path)
    for name in _README_CANDIDATES:
        path = root / name
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                return text[:4000]
            except Exception:
                return ""
    return ""


def _get_file_tree_summary(repo_path: str | Path, max_entries: int = 80) -> str:
    """Return a compact summary of the repo's top-level file structure."""
    root = Path(repo_path)
    entries: list[str] = []
    try:
        for item in sorted(root.iterdir()):
            if item.name.startswith("."):
                continue
            prefix = "dir " if item.is_dir() else "file"
            entries.append(f"  {prefix}  {item.name}")
            if len(entries) >= max_entries:
                entries.append(
                    f"  ... ({sum(1 for _ in root.iterdir())} total entries)"
                )
                break
    except Exception:
        pass
    return "\n".join(entries)


def _get_recent_git_log(repo_path: str | Path, max_commits: int = 20) -> str:
    """Return `git log --stat` for recent commits as a lightweight activity proxy."""
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={max_commits}", "--stat", "--oneline"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            output = result.stdout.strip()
            return output[:6000] if output else ""
    except Exception:
        pass
    return ""


def _build_repo_query(
    owner: str,
    repo: str,
    repo_path: str | Path,
    primary_language: str,
) -> str:
    """Build a descriptive query string for the taxonomy classifier from repo metadata."""
    readme = _read_readme(repo_path)
    file_tree = _get_file_tree_summary(repo_path)

    parts = [
        f"# Repository: {owner}/{repo}",
        f"Primary language: {primary_language}" if primary_language else "",
    ]

    if readme:
        parts.append(f"\n## README\n{readme}")

    if file_tree:
        parts.append(f"\n## File structure (root)\n{file_tree}")

    parts.append(
        "\nClassify this repository's primary purpose and domain. "
        "Determine what archetype of work this project represents, "
        "its scope/horizon, and applicable tags."
    )

    return "\n".join(p for p in parts if p)


def _serialise_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Ensure list/dict values are JSON-serialised strings for CSV compatibility."""
    out: dict[str, Any] = {}
    for col, val in raw.items():
        if isinstance(val, (dict, list)):
            out[col] = json.dumps(val, default=str)
        else:
            out[col] = val
    return out


def _instance_id(owner: str, repo_name: str, pr_number: Any) -> str:
    safe_repo = str(repo_name).replace("/", "__")
    return f"{owner}__{safe_repo}-{pr_number}"


def _problem_statement_from_pr(pr: dict[str, Any], max_chars: int = 12000) -> str:
    """PR title/body plus linked issues (Turing-style task text)."""
    parts: list[str] = []
    title = (pr.get("title") or "").strip()
    body = (pr.get("body") or "").strip()
    label_nodes = pr.get("labels", {}).get("nodes", []) or []
    label_names = [n.get("name") for n in label_nodes if n.get("name")]
    if label_names:
        parts.append("PR labels: " + ", ".join(label_names))
    if title:
        parts.append(f"# PR\n{title}")
    if body:
        parts.append(body)
    for issue in pr.get("closingIssuesReferences", {}).get("nodes", []) or []:
        if issue.get("__typename") == "PullRequest":
            continue
        num = issue.get("number")
        it = (issue.get("title") or "").strip()
        ib = (issue.get("body") or "").strip()
        if not (it or ib):
            continue
        header = f"## Linked issue #{num}" if num is not None else "## Linked issue"
        chunk = header
        if it:
            chunk += f"\n{it}"
        if ib:
            chunk += f"\n\n{ib}"
        parts.append(chunk.strip())
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        return text[: max_chars - 40] + f"\n... [{len(text)} chars total]"
    return text


def run_taxonomy_for_accepted_prs(
    accepted_prs: list[dict[str, Any]],
    owner: str,
    repo: str,
    primary_language: str,
    get_patch: Callable[[dict[str, Any]], str | None],
    *,
    model: str = "gpt-4o",
    base_url: str = "https://api.openai.com/v1",
    skip_taxonomy: bool = False,
    pr_number: int | None = None,
    concurrency: int = 8,
) -> list[dict[str, Any]]:
    """
    Classify each accepted PR and return per-PR records.

    Each record has ``number``, ``instance_id``, ``repo``, then the classifier
    fields (serialised), or ``error`` / ``summary`` on failure.
    """
    if skip_taxonomy:
        return []

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY is not set. Set it in your .env file or environment."
        )

    prs: list[dict[str, Any]] = []
    for p in accepted_prs:
        num = p.get("number")
        if pr_number is not None and num != pr_number:
            continue
        prs.append(p)

    if not prs:
        logger.info("No accepted PRs to run taxonomy on.")
        return []

    items: list[dict[str, Any]] = []
    meta_nums: list[Any] = []
    for pr in prs:
        num = pr.get("number")
        iid = _instance_id(owner, repo, num)
        patch = get_patch(pr)
        if not patch:
            logger.warning(
                "No patch for PR #%s — taxonomy will use description only", num
            )
            patch = ""
        prob = _problem_statement_from_pr(pr)
        items.append(
            {
                "instance_id": iid,
                "repo": f"{owner}/{repo}",
                "problem_statement": prob,
                "gold_patch": patch or "",
                "language": primary_language or "",
            }
        )
        meta_nums.append(num)

    logger.info(
        "Running PR-level taxonomy for %s/%s: %d PR(s), model=%s concurrency=%s",
        owner,
        repo,
        len(items),
        model,
        concurrency,
    )

    try:
        classifier = TaxonomyClassifier(
            api_key=api_key,
            base_url=base_url,
            model=model,
            concurrency=max(1, int(concurrency)),
        )
        raw_results = classifier.classify_batch(items)
    except Exception as e:
        logger.warning(
            "Taxonomy batch classification failed for %s/%s: %s", owner, repo, e
        )
        return []

    per_pr_out: list[dict[str, Any]] = []

    for num, iid, item, res in zip(
        meta_nums,
        [it["instance_id"] for it in items],
        items,
        raw_results,
    ):
        entry: dict[str, Any] = {
            "number": num,
            "instance_id": iid,
            "repo": item["repo"],
        }
        if res.get("error"):
            entry["error"] = res["error"]
            entry["summary"] = res.get("summary", "")
            logger.warning("Taxonomy error for PR #%s: %s", num, res["error"])
        else:
            entry.update(_serialise_result(res))
        per_pr_out.append(entry)

    return per_pr_out


def run_taxonomy_classification(
    owner: str,
    repo: str,
    repo_path: str | Path,
    primary_language: str = "",
    model: str = "gpt-4o",
    base_url: str = "https://api.openai.com/v1",
    skip_taxonomy: bool = False,
) -> dict[str, Any]:
    """Run taxonomy classification on a repository (legacy: README + git log, not PR-based).

    Returns a serialised classifier result dict, or empty dict on error or skip.
    """
    if skip_taxonomy:
        return {}

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY is not set. Set it in your .env file or environment."
        )

    query = _build_repo_query(owner, repo, repo_path, primary_language)
    git_log = _get_recent_git_log(repo_path)

    logger.info(
        "Running legacy repo-level taxonomy for %s/%s (model=%s) ...",
        owner,
        repo,
        model,
    )

    try:
        classifier = TaxonomyClassifier(
            api_key=api_key,
            base_url=base_url,
            model=model,
            concurrency=1,
        )

        result = classifier.classify(
            query=query,
            repo=f"{owner}/{repo}",
            diff=git_log,
            language=primary_language,
        )

        if result.get("error"):
            logger.warning(
                "Taxonomy classification error for %s/%s: %s",
                owner,
                repo,
                result["error"],
            )
            return {}

        return _serialise_result(result)

    except Exception as e:
        logger.warning(
            "Taxonomy classification exception for %s/%s: %s", owner, repo, e
        )
        return {}
