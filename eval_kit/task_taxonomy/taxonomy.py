"""
Embedded taxonomy for coding-task classification.

Contains the default taxonomy definition and helpers for building
the LLM reference prompt.  Users can override with a custom YAML file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Default taxonomy (embedded so no external file is required)
# ---------------------------------------------------------------------------

DEFAULT_TAXONOMY: dict[str, Any] = {
    "domains": {
        "Systems & Low-Level Platform": [
            "operating systems",
            "memory management",
            "compilers & interpreters",
            "concurrency & parallelism",
            "algorithms & data structures",
            "filesystem",
        ],
        "Infrastructure, Networking & Distributed Systems": [
            "infrastructure",
            "containers & orchestration",
            "cloud computing",
            "networking protocols",
            "microservices & distributed",
            "observability & monitoring",
        ],
        "Data, Databases & Information Systems": [
            "relational databases (SQL)",
            "NoSQL & document stores",
            "data pipelines & ETL",
            "data analytics & visualization",
            "caching strategies",
        ],
        "Languages, Toolchains & Formal Methods": [
            "CLI & TUI tools",
            "test automation",
            "unit & integration testing",
            "CI/CD & build automation & package management",
            "linters & formatters",
        ],
        "Security, Privacy & Reverse Engineering": [
            "application security",
            "identity & authentication",
            "cryptography",
            "reverse engineering",
        ],
        "Compute, Performance, Numerics & Control": [
            "performance & load testing",
            "numerical computing",
            "GPU computing (CUDA, OpenCL)",
            "signal processing",
        ],
        "AI/ML, Search & Ranking": [
            "machine learning engineering",
            "MLOps",
            "natural language processing",
            "computer vision",
            "training pipelines",
            "inference serving",
        ],
        "Applications & User-Facing Platforms": [
            "API design & development",
            "web frontend",
            "desktop applications",
            "mobile applications",
            "accessibility",
        ],
    },
    "archetypes": {
        "bootstrap": "Env setup, dependency resolution, toolchain bring-up.",
        "build": "Greenfield — new system, library, or component from scratch.",
        "extend": "Feature addition, integration, spec-to-code on existing system.",
        "fix": "Bug fix, regression fix, incident remediation.",
        "improve": "Refactor, optimize, migrate, harden.",
        "understand": "Exploration, debugging, code review, root cause analysis.",
        "assure": "Tests, verification, proofs, compliance checks.",
        "operate": "CI/CD, deployment, SRE, observability.",
    },
    "horizons": {
        "local": {
            "description": "Single file or tightly scoped module.",
            "files": "1-3",
            "time": "< 30 min",
        },
        "repo": {
            "description": "Multi-file, one repo, one subsystem.",
            "files": "4-20",
            "time": "30 min - 4 hours",
        },
        "system": {
            "description": "Multi-service or external dependencies.",
            "files": "10-50+",
            "time": "4 hours - 2 days",
        },
        "long_horizon": {
            "description": "Staged work, day-scale effort.",
            "files": "20-100+",
            "time": "> 2 days",
        },
    },
    "vertical_tags": [
        "enterprise_backoffice",
        "finance_payments",
        "crypto_blockchain",
        "science_research",
        "bioinformatics",
        "healthcare",
        "ecommerce",
        "media_publishing",
        "education_edtech",
    ],
    "constraint_tags": [
        "security_critical",
        "safety_critical",
        "compliance_heavy",
        "realtime_latency_critical",
        "perf_throughput_critical",
        "legacy_interop_heavy",
        "uncommon_language",
    ],
    "ecosystem_tags": [
        "jupyter_notebook",
        "terraform_iac",
        "kubernetes",
        "aws",
        "gcp",
        "azure",
        "docker",
        "github_actions",
    ],
    "llm_capability_tags": [
        "code_search_and_exploration",
        "intent_understanding",
        "instruction_following",
        "tool_use",
        "planning",
        "output_validation",
    ],
}


# ---------------------------------------------------------------------------
# File extension → language  (used by diff analyser)
# ---------------------------------------------------------------------------

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".proto": "protobuf",
    ".md": "markdown",
    ".cu": "cuda",
    ".cuh": "cuda",
    ".tf": "terraform",
    ".dockerfile": "dockerfile",
    ".bazel": "bazel",
    ".bzl": "bazel",
}


# File path patterns → ecosystem tag
FILE_ECOSYSTEM_PATTERNS: list[tuple[str, str]] = [
    (r"\.github/workflows/.*\.ya?ml$", "github_actions"),
    (r"Dockerfile", "docker"),
    (r"docker-compose\.ya?ml$", "docker"),
    (r"k8s/|kubernetes/|helm/|Chart\.yaml$", "kubernetes"),
    (r"terraform/|\.tf$", "terraform_iac"),
    (r"\.ipynb$", "jupyter_notebook"),
    (r"package\.json$|yarn\.lock$", "nodejs"),
    (r"requirements\.txt$|pyproject\.toml$|setup\.py$", "python_packaging"),
    (r"Cargo\.toml$", "rust_cargo"),
    (r"go\.mod$", "go_modules"),
    (r"BUILD\.bazel$|MODULE\.bazel$", "bazel"),
]

# File path patterns → (domain, subdomain) hints
FILE_DOMAIN_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"\.github/workflows/|Jenkinsfile",
        "Languages, Toolchains & Formal Methods",
        "CI/CD & build automation",
    ),
    (
        r"test[s_]?/|_test\.|\.test\.",
        "Languages, Toolchains & Formal Methods",
        "unit & integration testing",
    ),
    (
        r"k8s/|kubernetes/|helm/|deploy",
        "Infrastructure, Networking & Distributed Systems",
        "containers & orchestration",
    ),
    (
        r"terraform/|pulumi/",
        "Infrastructure, Networking & Distributed Systems",
        "infrastructure",
    ),
    (
        r"migrations?/|\.sql$",
        "Data, Databases & Information Systems",
        "relational databases (SQL)",
    ),
    (
        r"ml/|model[s]?/|train|inference",
        "AI/ML, Search & Ranking",
        "machine learning engineering",
    ),
    (
        r"cuda|\.cu$|gpu",
        "Compute, Performance, Numerics & Control",
        "GPU computing (CUDA, OpenCL)",
    ),
    (
        r"frontend/|components/|pages/",
        "Applications & User-Facing Platforms",
        "web frontend",
    ),
    (
        r"api/|routes/|handlers/",
        "Applications & User-Facing Platforms",
        "API design & development",
    ),
    (
        r"security/|auth/|crypto",
        "Security, Privacy & Reverse Engineering",
        "application security",
    ),
]


# ---------------------------------------------------------------------------
# Diff analysis (pure rule-based, no LLM)
# ---------------------------------------------------------------------------


@dataclass
class DiffStats:
    """Statistics extracted from a git diff."""

    files_touched: int = 0
    files_added: int = 0
    files_modified: int = 0
    files_deleted: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    languages: set[str] = field(default_factory=set)
    file_paths: list[str] = field(default_factory=list)
    ecosystem_tags: set[str] = field(default_factory=set)
    domain_hints: list[tuple[str, str]] = field(default_factory=list)
    has_tests: bool = False
    has_config: bool = False
    has_docs: bool = False
    has_ci: bool = False


def parse_diff(diff_content: str) -> DiffStats:
    """Parse a unified git diff and extract statistics."""
    stats = DiffStats()
    if not diff_content:
        return stats

    current_file: str | None = None
    file_status: dict[str, str] = {}

    for line in diff_content.split("\n"):
        if line.startswith("diff --git"):
            match = re.search(r"diff --git a/(.+?) b/(.+?)$", line)
            if match:
                current_file = match.group(2)
                stats.file_paths.append(current_file)
                file_status[current_file] = "modified"
        elif line.startswith("new file mode"):
            if current_file:
                file_status[current_file] = "added"
        elif line.startswith("deleted file mode"):
            if current_file:
                file_status[current_file] = "deleted"
        elif line.startswith("+") and not line.startswith("+++"):
            stats.lines_added += 1
        elif line.startswith("-") and not line.startswith("---"):
            stats.lines_removed += 1

    stats.files_touched = len(file_status)
    stats.files_added = sum(1 for s in file_status.values() if s == "added")
    stats.files_modified = sum(1 for s in file_status.values() if s == "modified")
    stats.files_deleted = sum(1 for s in file_status.values() if s == "deleted")

    for fp in stats.file_paths:
        ext = Path(fp).suffix.lower()
        if ext in EXTENSION_TO_LANGUAGE:
            stats.languages.add(EXTENSION_TO_LANGUAGE[ext])

        basename = Path(fp).name.lower()
        if basename in ("dockerfile",) or basename.endswith(".dockerfile"):
            stats.languages.add("dockerfile")
            stats.ecosystem_tags.add("docker")

        for pattern, tag in FILE_ECOSYSTEM_PATTERNS:
            if re.search(pattern, fp, re.IGNORECASE):
                stats.ecosystem_tags.add(tag)

        for pattern, domain, subdomain in FILE_DOMAIN_PATTERNS:
            if re.search(pattern, fp, re.IGNORECASE):
                stats.domain_hints.append((domain, subdomain))

        if re.search(r"test[s_]?/|_test\.|\.test\.", fp, re.IGNORECASE):
            stats.has_tests = True
        if re.search(r"config|\.ya?ml$|\.json$|\.toml$", fp, re.IGNORECASE):
            stats.has_config = True
        if re.search(r"docs?/|readme|\.md$", fp, re.IGNORECASE):
            stats.has_docs = True
        if re.search(r"\.github/|\.circleci/|jenkins", fp, re.IGNORECASE):
            stats.has_ci = True

    return stats


def infer_horizon(stats: DiffStats) -> tuple[str, str]:
    """Return (horizon, reasoning) based on file counts."""
    n = stats.files_touched
    if n == 0:
        return "local", "No files in diff"
    if n <= 3:
        return "local", f"{n} file(s) touched"
    if n <= 20:
        return "repo", f"{n} files touched"
    if n <= 50:
        return "system", f"{n} files touched"
    return "long_horizon", f"{n}+ files touched"


# ---------------------------------------------------------------------------
# Taxonomy → LLM prompt
# ---------------------------------------------------------------------------


def load_taxonomy(path: str | Path | None = None) -> dict[str, Any]:
    """Load taxonomy from a YAML file, or return the built-in default."""
    if path is None:
        return DEFAULT_TAXONOMY
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Taxonomy file not found: {path}")
    with open(p) as f:
        data = yaml.safe_load(f)
    return data.get("categories", data)


def build_taxonomy_prompt(taxonomy: dict[str, Any]) -> str:
    """Build a concise reference string from the taxonomy for the LLM prompt."""
    lines: list[str] = []

    # Domains
    lines.append("## DOMAINS (pick primary and secondary):")
    for domain, subs in taxonomy.get("domains", {}).items():
        preview = ", ".join(subs[:5])
        if len(subs) > 5:
            preview += ", ..."
        lines.append(f"  - {domain}: [{preview}]")

    # Archetypes
    lines.append("\n## ARCHETYPES (pick exactly one):")
    for key, desc in taxonomy.get("archetypes", {}).items():
        if isinstance(desc, dict):
            desc = desc.get("description", "")
        lines.append(f"  - {key}: {str(desc).strip()[:120]}")

    # Horizons
    lines.append("\n## HORIZONS (pick exactly one):")
    for key, val in taxonomy.get("horizons", {}).items():
        if isinstance(val, dict):
            lines.append(
                f"  - {key}: {val.get('description', '')} (files: {val.get('files', '?')}, time: {val.get('time', '?')})"
            )
        else:
            lines.append(f"  - {key}: {val}")

    # Tags
    for tag_name in (
        "vertical_tags",
        "constraint_tags",
        "ecosystem_tags",
        "llm_capability_tags",
    ):
        label = tag_name.replace("_", " ").upper()
        tags = taxonomy.get(tag_name, [])
        if tags:
            lines.append(f"\n## {label} (pick any that apply):")
            lines.append(f"  {tags}")

    return "\n".join(lines)
