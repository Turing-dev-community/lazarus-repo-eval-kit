from __future__ import annotations

import json
import logging
import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Sequence

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)
from task_taxonomy.classify import TaxonomyClassifier

logger = logging.getLogger(__name__)

TAXONOMY_COLUMNS = [
    "domain_primary",
    "domain_secondary",
    "subdomain_tags",
    "archetype",
    "archetype_confidence",
    "archetype_reasoning",
    "horizon",
    "horizon_estimated_files",
    "horizon_reasoning",
    "vertical_tags",
    "constraint_tags",
    "ecosystem_tags",
    "llm_capability_tags",
    "complexity",
    "rule_based_signals",
    "summary",
]

_EMPTY_RESULT: dict[str, str] = {col: "" for col in TAXONOMY_COLUMNS}

_CONF_ORDER: dict[str, int] = {"high": 3, "medium": 2, "low": 1}
_CONF_REVERSE: dict[int, str] = {3: "high", 2: "medium", 1: "low"}

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
    for col in TAXONOMY_COLUMNS:
        val = raw.get(col, "")
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


def _as_list(val: Any) -> list[Any]:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val.strip().startswith("["):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _as_dict(val: Any) -> dict[str, Any]:
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip().startswith("{"):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _plurality_first_order(values: Sequence[str]) -> str:
    values = [v.strip() for v in values if v and str(v).strip()]
    if not values:
        return ""
    counts = Counter(values)
    best = max(counts.values())
    for v in values:
        if counts[v] == best:
            return v
    return values[0]


def _median_confidence(confs: Sequence[str]) -> str:
    levels: list[int] = []
    for c in confs:
        key = (c or "").lower().strip()
        levels.append(_CONF_ORDER.get(key, 2))
    if not levels:
        return ""
    if max(levels) - min(levels) >= 2:
        return "mixed"
    levels.sort()
    mid = levels[len(levels) // 2]
    return _CONF_REVERSE.get(mid, "medium")


def _union_string_tags(rows: Sequence[dict[str, Any]], key: str) -> list[str]:
    seen: set[str] = set()
    for r in rows:
        for x in _as_list(r.get(key)):
            s = str(x).strip()
            if s:
                seen.add(s)
    return sorted(seen)


def _dedup_join_scalars(values: Sequence[str], sep: str = " | ") -> str:
    """Sorted unique non-empty strings (domain_primary / domain_secondary aggregates)."""
    unique = sorted({(v or "").strip() for v in values if (v or "").strip()})
    return sep.join(unique)


def _cap_join_reasoning(
    pr_numbers: Sequence[Any],
    texts: Sequence[str],
    *,
    max_len: int | None = 1800,
    max_segment: int | None = 350,
) -> str:
    """Join reasoning as PR#n: text.

    Keyword-only limits avoid mixing up ``max_len`` / ``max_segment`` with text lists.
    ``max_len=None``: do not truncate the full joined string (every PR kept).
    ``max_segment=None``: keep each PR's full reasoning (no per-PR cut).
    """
    parts: list[str] = []
    for num, text in zip(pr_numbers, texts):
        t = (text or "").strip().replace("\n", " ")
        if max_segment is not None and len(t) > max_segment:
            t = t[: max_segment - 3] + "..."
        parts.append(f"PR#{num}: {t}")
    s = " | ".join(parts)
    if max_len is not None and len(s) > max_len:
        return s[:max_len]
    return s


def _pr_tagged_join(
    pr_numbers: Sequence[Any],
    texts: Sequence[str],
    *,
    max_segment: int,
    max_total: int,
) -> str:
    """Join per-PR values as PR#n: text (archetype, horizon_estimated_files, complexity)."""
    parts: list[str] = []
    for num, text in zip(pr_numbers, texts):
        t = (text or "").strip().replace("\n", " ")
        if len(t) > max_segment:
            t = t[: max_segment - 3] + "..."
        parts.append(f"PR#{num}: {t}")
    s = " | ".join(parts)
    return s[:max_total] if len(s) > max_total else s


def _complexity_one_liner(raw: Any) -> str:
    """Short per-PR complexity text (like archetype), not JSON with nested lists."""
    d = _as_dict(raw)
    if not d:
        return ""
    ft = d.get("files_touched")
    la = d.get("lines_added")
    lr = d.get("lines_removed")
    langs = d.get("languages")
    if isinstance(langs, str):
        try:
            langs = json.loads(langs)
        except Exception:
            langs = [langs] if langs else []
    if not isinstance(langs, list):
        langs = []
    lang_s = ",".join(str(x) for x in langs[:10] if x)
    chunks: list[str] = []
    if ft is not None:
        try:
            chunks.append(f"{int(ft)} files")
        except (TypeError, ValueError):
            chunks.append(f"{ft} files")
    if la is not None or lr is not None:
        try:
            la_i = int(la) if la is not None else 0
            lr_i = int(lr) if lr is not None else 0
            chunks.append(f"+{la_i}/-{lr_i}")
        except (TypeError, ValueError):
            chunks.append(f"+{la}/-{lr}")
    if lang_s:
        chunks.append(lang_s)
    return " ".join(chunks) if chunks else ""


def aggregate_pr_taxonomy(
    rows: Sequence[dict[str, Any]],
    pr_numbers: Sequence[Any],
) -> dict[str, Any]:
    """Merge successful per-PR classifier dicts into one repo-level result (pre-serialize)."""
    if not rows:
        return {col: "" for col in TAXONOMY_COLUMNS}

    nums = list(pr_numbers)
    dom_p_agg = _dedup_join_scalars([str(r.get("domain_primary", "")) for r in rows])
    dom_s_agg = _dedup_join_scalars([str(r.get("domain_secondary", "")) for r in rows])
    hz = _plurality_first_order([str(r.get("horizon", "")) for r in rows])
    conf = _median_confidence([str(r.get("archetype_confidence", "")) for r in rows])

    subdomain_agg = _union_string_tags(rows, "subdomain_tags")

    arch_pr = _pr_tagged_join(
        nums,
        [str(r.get("archetype", "")) for r in rows],
        max_segment=120,
        max_total=6000,
    )
    arch_r = _cap_join_reasoning(
        nums,
        [str(r.get("archetype_reasoning", "")) for r in rows],
        max_len=None,
        max_segment=None,
    )
    hz_r = _cap_join_reasoning(
        nums,
        [str(r.get("horizon_reasoning", "")) for r in rows],
        max_len=None,
        max_segment=None,
    )

    hz_est = _pr_tagged_join(
        nums,
        [str(r.get("horizon_estimated_files", "")) for r in rows],
        max_segment=400,
        max_total=8000,
    )

    comp_segments = [_complexity_one_liner(r.get("complexity")) for r in rows]
    complexity_pr = _pr_tagged_join(
        nums, comp_segments, max_segment=200, max_total=12000
    )

    agg_summary = _cap_join_reasoning(
        nums,
        [str(r.get("summary", "")) for r in rows],
        max_len=None,
        max_segment=None,
    )

    rule_based_per_pr: list[dict[str, Any]] = [
        {
            "number": num,
            "rule_based_signals": dict(_as_dict(r.get("rule_based_signals"))),
        }
        for num, r in zip(nums, rows)
    ]

    return {
        "domain_primary": dom_p_agg,
        "domain_secondary": dom_s_agg,
        "subdomain_tags": subdomain_agg,
        "archetype": arch_pr,
        "archetype_confidence": conf,
        "archetype_reasoning": arch_r,
        "horizon": hz,
        "horizon_estimated_files": hz_est,
        "horizon_reasoning": hz_r,
        "vertical_tags": _union_string_tags(rows, "vertical_tags"),
        "constraint_tags": _union_string_tags(rows, "constraint_tags"),
        "ecosystem_tags": _union_string_tags(rows, "ecosystem_tags"),
        "llm_capability_tags": _union_string_tags(rows, "llm_capability_tags"),
        "complexity": complexity_pr,
        "rule_based_signals": rule_based_per_pr,
        "summary": agg_summary,
    }


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
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Classify each accepted PR; aggregate successful rows into repo-level columns.

    Returns (serialised TAXONOMY_COLUMNS, per_pr_records). Each per-PR record has
    ``number``, ``instance_id``, ``repo``, then TAXONOMY_COLUMNS (serialised), or
    ``error`` / ``summary`` on failure.
    """
    empty_per_pr: list[dict[str, Any]] = []
    if skip_taxonomy:
        return dict(_EMPTY_RESULT), empty_per_pr

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        logger.warning(
            "No OPENAI_API_KEY found — taxonomy classification will be skipped"
        )
        return dict(_EMPTY_RESULT), empty_per_pr

    prs: list[dict[str, Any]] = []
    for p in accepted_prs:
        num = p.get("number")
        if pr_number is not None and num != pr_number:
            continue
        prs.append(p)

    if not prs:
        logger.info("No accepted PRs to run taxonomy on.")
        return dict(_EMPTY_RESULT), empty_per_pr

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
        return dict(_EMPTY_RESULT), empty_per_pr

    per_pr_out: list[dict[str, Any]] = []
    good_rows: list[dict[str, Any]] = []
    good_nums: list[Any] = []

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
            good_rows.append(res)
            good_nums.append(num)
        per_pr_out.append(entry)

    if not good_rows:
        logger.warning("All per-PR taxonomy calls failed for %s/%s", owner, repo)
        return dict(_EMPTY_RESULT), per_pr_out

    merged = aggregate_pr_taxonomy(good_rows, good_nums)
    return _serialise_result(merged), per_pr_out


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

    Returns a dict with the 16 taxonomy column values.
    On error or skip, returns empty strings for all columns.
    """
    if skip_taxonomy:
        return dict(_EMPTY_RESULT)

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        logger.warning(
            "No OPENAI_API_KEY found — taxonomy classification will be skipped"
        )
        return dict(_EMPTY_RESULT)

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
            return dict(_EMPTY_RESULT)

        return _serialise_result(result)

    except Exception as e:
        logger.warning(
            "Taxonomy classification exception for %s/%s: %s", owner, repo, e
        )
        return dict(_EMPTY_RESULT)
