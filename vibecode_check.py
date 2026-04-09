import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)

DEFAULT_MODEL = "gpt-4o"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JS_EXTS = [".ts", ".tsx", ".js", ".jsx"]
PY_EXTS = [".py"]
ALL_SOURCE_EXTS = JS_EXTS + PY_EXTS

EXCLUDE_DIRS = {
    "node_modules",
    ".git",
    ".next",
    "dist",
    "build",
    "__pycache__",
    ".venv",
    "env",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "target",
    ".gradle",
    ".mvn",
    ".idea",
    "bin",
    "out",
    ".settings",
    "site-packages",
    "egg-info",
}

TOOLGEN_DIRS = {
    "migrations",
    "generated",
    "generated-sources",
    "generated-test-sources",
    "__generated__",
    "typechain",
    "typechain-types",
    ".prisma",
}

TOOLGEN_FILE_PATTERNS = [
    r"/migrations/",
    r"\.g\.dart$",
    r"_pb2\.py$",
    r"_pb2_grpc\.py$",
    r"\.generated\.",
    r"/flyway/.*\.sql$",
    r"schema\.prisma$",
    r"package-lock\.json$",
    r"pnpm-lock\.yaml$",
    r"yarn\.lock$",
    r"poetry\.lock$",
    r"Pipfile\.lock$",
    r"Cargo\.lock$",
]

# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def _is_toolgen(filepath: str) -> bool:
    normalized = filepath.replace("\\", "/")
    for part in normalized.split("/"):
        if part in TOOLGEN_DIRS:
            return True
    return any(re.search(pat, normalized) for pat in TOOLGEN_FILE_PATTERNS)


def _find_files(
    root: str, extensions: list[str], skip_toolgen: bool = True
) -> list[str]:
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for f in filenames:
            if any(f.endswith(ext) for ext in extensions):
                full = os.path.join(dirpath, f)
                if skip_toolgen and _is_toolgen(os.path.relpath(full, root)):
                    continue
                results.append(full)
    return results


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


def _rel(path: str, root: str) -> str:
    return os.path.relpath(path, root)


def _is_test(path: str, root: str) -> bool:
    rel = _rel(path, root).lower()
    return any(p in rel for p in ["test", "spec", "__tests__"])


def _detect_language(files: list[str]) -> str:
    """Return dominant language: 'python' or 'js'."""
    py = sum(1 for f in files if os.path.splitext(f)[1] in PY_EXTS)
    js = sum(1 for f in files if os.path.splitext(f)[1] in JS_EXTS)
    return "python" if py >= js else "js"


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------


def _clone_repo(owner: str, repo: str, dest: str, token: str) -> tuple[bool, str]:
    url = (
        f"https://{token}@github.com/{owner}/{repo}.git"
        if token
        else f"https://github.com/{owner}/{repo}.git"
    )
    r = subprocess.run(
        ["git", "clone", "--depth", "200", url, dest],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return r.returncode == 0, r.stderr.strip() if r.returncode != 0 else ""


def _run_git(args: list[str], cwd: str) -> str:
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=30
        )
        return r.stdout.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Criterion 1: Documentation
# ---------------------------------------------------------------------------

GENERIC_DOC_PHRASES = [
    "this module provides",
    "for more information",
    "this directory contains",
    "this ensures that",
    "this function handles",
    "getting started",
    "learn more",
    "check out",
    "decision tree",
    "this package provides",
    "this library provides",
    "this project provides",
    "table of contents",
    "overview of",
    "comprehensive guide",
]

GENERIC_DOCKERFILE_COMMENTS = [
    "# use ",
    "# set working directory",
    "# copy package",
    "# install dependencies",
    "# copy source code",
    "# build the application",
    "# expose port",
    "# set environment",
    "# start the application",
    "# create ",
    "# run the application",
]

AGENT_PLANNING_DOCS = {
    "architecture.md",
    "implementation_plan.md",
    "implementation-plan.md",
    "backlog.md",
    "task_list.md",
    "task-list.md",
    "roadmap.md",
    "design.md",
    "planning.md",
    "project_plan.md",
    "project-plan.md",
    "technical_design.md",
    "technical-design.md",
    "design_doc.md",
    "design-doc.md",
    "agents.md",
    "implementation.md",
}

_TODO_RE = re.compile(
    r"\b(TODO|FIXME|TBD|WIP|coming soon|to be added|to be determined|"
    r"not yet implemented|placeholder)\b",
    re.I,
)

_EMOJI_RE = re.compile(
    r"[\U0001f300-\U0001f9ff\u2600-\u26ff\u2700-\u27bf\u2b50\u2705"
    r"\u274c\u26a0\u2139\u2611\u2622\u2623]"
)


def _criterion_documentation(root: str, lang: str) -> tuple[int, list[str]]:
    score = 1
    evidence = []

    try:
        root_entries = [
            f for f in os.listdir(root) if os.path.isfile(os.path.join(root, f))
        ]
    except OSError:
        root_entries = []

    agent_docs = [f for f in root_entries if f.lower() in AGENT_PLANNING_DOCS]
    if agent_docs:
        evidence.append(
            f"Agent-generated planning docs at root: {', '.join(sorted(agent_docs))}"
        )
        score = 5 if len(agent_docs) >= 3 else (4 if len(agent_docs) >= 2 else 3)

    md_files = _find_files(root, [".md"])
    emoji_heavy = 0
    total_generic = 0

    for md in md_files:
        content = _read(md)
        emojis = len(_EMOJI_RE.findall(content))
        if emojis > 3:
            emoji_heavy += 1
            evidence.append(f"Emoji-heavy markdown: {_rel(md, root)} ({emojis} emojis)")
        total_generic += sum(
            1 for p in GENERIC_DOC_PHRASES if p.lower() in content.lower()
        )

    # Dockerfile generic comments
    dockerfile = None
    for dirpath, _, filenames in os.walk(root):
        if ".git" in dirpath:
            continue
        for fn in filenames:
            if fn.lower().startswith("dockerfile"):
                dockerfile = os.path.join(dirpath, fn)
                break
        if dockerfile:
            break
    if dockerfile:
        content = _read(dockerfile)
        generic_comments = sum(
            1
            for line in content.strip().split("\n")
            if any(
                line.strip().lower().startswith(p) for p in GENERIC_DOCKERFILE_COMMENTS
            )
        )
        if generic_comments >= 5:
            evidence.append(
                f"Generic Dockerfile comments: {_rel(dockerfile, root)} ({generic_comments} lines)"
            )
            score = max(score, 4)

    # Language-specific boilerplate
    if lang == "python":
        for cfg in ("setup.py", "pyproject.toml", "setup.cfg"):
            cf = os.path.join(root, cfg)
            if os.path.exists(cf):
                content = _read(cf)
                for bp in [
                    "a short description",
                    "todo: add description",
                    "example package",
                    "my project",
                ]:
                    if bp.lower() in content.lower():
                        evidence.append(
                            f"Boilerplate placeholder in {_rel(cf, root)}: '{bp}'"
                        )
                        score = max(score, 3)

    if emoji_heavy >= 1:
        score = max(score, 4)
    if total_generic >= 5:
        score = max(score, 3)
    if total_generic >= 10:
        score = max(score, 5)
    if emoji_heavy >= 1 and total_generic >= 5:
        score = 5

    # Overly perfect markdown (no TODOs/TBDs in substantial files)
    perfect_md = sum(
        1 for md in md_files if len(_read(md)) > 300 and not _TODO_RE.search(_read(md))
    )
    substantial_md = sum(1 for md in md_files if len(_read(md)) > 300)
    if substantial_md >= 3 and perfect_md == substantial_md:
        evidence.append(
            f"TODO-free markdown: {perfect_md}/{substantial_md} files have no TODOs/TBDs"
        )
        score = max(score, 3)

    if not md_files:
        evidence.append("No markdown documentation found")
    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 2: Comment Density & Tone
# ---------------------------------------------------------------------------

OBVIOUS_COMMENT_PATTERNS = [
    re.compile(
        r"(?://|#)\s*(?:import|export|define|declare|create|initialize|set up|handle)\s",
        re.I,
    ),
    re.compile(
        r"(?://|#)\s*(?:this function|this method|this class|this component|this module)\s",
        re.I,
    ),
]

AUTO_GEN_PATTERNS = [
    re.compile(r"//\s*generated\s+component", re.I),
    re.compile(r"(?:#|//)\s*auto[- ]?generated", re.I),
    re.compile(r"(?:#|//)\s*generated\s+by", re.I),
    re.compile(r"/\*\s*auto[- ]?generated", re.I),
    re.compile(r"@generated"),
    re.compile(r"// Code generated .* DO NOT EDIT"),
]


def _criterion_comments(
    root: str, lang: str, files: list[str]
) -> tuple[int, list[str]]:
    score = 1
    evidence = []
    total_files = len(files)
    if not files:
        return 1, ["No source files found"]

    auto_markers = 0
    auto_examples: list[str] = []
    obvious_files = 0
    obvious_examples: list[str] = []

    for f in files:
        content = _read(f)
        lines = content.split("\n")
        rel = _rel(f, root)

        # Auto-generated markers in first 10 lines
        head = "\n".join(lines[:10])
        if any(pat.search(head) for pat in AUTO_GEN_PATTERNS):
            auto_markers += 1
            if len(auto_examples) < 5:
                auto_examples.append(rel)

        # Narration/obvious comments
        flagged = False
        for line in lines:
            stripped = line.strip()
            if not (stripped.startswith("//") or stripped.startswith("#")):
                continue
            if any(pat.search(stripped) for pat in OBVIOUS_COMMENT_PATTERNS):
                if not flagged:
                    obvious_files += 1
                    if len(obvious_examples) < 5:
                        obvious_examples.append(rel)
                    flagged = True

    # Language-specific: over-documented functions
    if lang == "python":
        docstring_heavy = 0
        ds_examples: list[str] = []
        for f in [f for f in files if os.path.splitext(f)[1] == ".py"]:
            content = _read(f)
            funcs = len(re.findall(r"def\s+\w+\s*\(", content))
            docstrings = len(re.findall(r'"""[\s\S]*?"""', content))
            if funcs >= 4 and docstrings >= funcs * 0.9:
                docstring_heavy += 1
                if len(ds_examples) < 5:
                    ds_examples.append(_rel(f, root))
        if docstring_heavy > 5:
            evidence.append(
                f"{docstring_heavy} Python files have docstrings on every function: {', '.join(ds_examples)}"
            )
            score = max(score, 3)

    if auto_markers > 0:
        pct = (auto_markers / total_files) * 100
        evidence.append(
            f"{auto_markers}/{total_files} files have '// auto-generated' or '@generated' stamps in the header ({pct:.0f}%): "
            + ", ".join(auto_examples)
        )
        score = 5 if pct > 50 else (max(score, 4) if pct > 20 else max(score, 3))

    if obvious_files > total_files * 0.3:
        evidence.append(
            f"{obvious_files} files have narration/obvious comments: "
            + ", ".join(obvious_examples)
        )
        score = max(score, 4)

    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 3: Naming Consistency
# ---------------------------------------------------------------------------


def _criterion_naming(root: str, lang: str, files: list[str]) -> tuple[int, list[str]]:
    score = 1
    evidence = []

    if lang == "js":
        tsx_files = [f for f in files if os.path.splitext(f)[1] in (".tsx", ".jsx")]
        component_names = [os.path.splitext(os.path.basename(f))[0] for f in tsx_files]

        # Random 2-letter suffixes (AI-generated component names)
        random_suffix = [
            n
            for n in component_names
            if re.match(r"^[A-Z][a-z]+[A-Z][a-z]+[A-Z]{2}$", n)
        ]
        if len(random_suffix) > 5:
            evidence.append(
                f"{len(random_suffix)} components with random 2-letter suffixes: "
                + ", ".join(random_suffix[:5])
            )
            score = (
                5 if len(random_suffix) > 20 else (4 if len(random_suffix) > 10 else 3)
            )

        # Generic template base names
        template_names = [
            "EndPanel",
            "FirstBlock",
            "FullArea",
            "FullWrapper",
            "HalfLayout",
            "HalfZone",
            "MainBanner",
            "MainSegment",
            "MidDisplay",
            "MidElement",
            "NextBox",
            "NextModule",
            "SideContainer",
            "SideSection",
            "SubPart",
            "TopHighlight",
            "TopUnit",
        ]
        template_matches = sum(
            1 for n in component_names for t in template_names if n.startswith(t)
        )
        if template_matches > 10:
            evidence.append(
                f"{template_matches} components use generic template base names"
            )
            score = max(score, 4)

        # Machine-generated section markers
        for f in tsx_files:
            content = _read(f)
            markers = re.findall(r"//\s*={3,}\s*\w+.*={3,}", content)
            if len(markers) >= 5:
                evidence.append(
                    f"{_rel(f, root)}: {len(markers)} machine-generated section markers"
                )
                score = max(score, 4)

    if lang == "python":
        class_names: list[str] = []
        func_names: list[str] = []
        for f in [f for f in files if os.path.splitext(f)[1] == ".py"]:
            content = _read(f)
            class_names.extend(re.findall(r"class\s+(\w+)", content))
            func_names.extend(re.findall(r"def\s+(\w+)", content))

        ai_suffixes = ["Manager", "Handler", "Processor", "Helper", "Utility", "Base"]
        ai_prefixes = ["handle_", "process_", "do_", "run_", "execute_", "perform_"]
        total_names = len(class_names) + len(func_names)
        generic_pct = 0.0

        if total_names > 10:
            generic = sum(
                1 for n in class_names if any(n.endswith(s) for s in ai_suffixes)
            )
            generic += sum(
                1 for n in func_names if any(n.startswith(p) for p in ai_prefixes)
            )
            generic_pct = (generic / total_names) * 100
            if generic_pct > 60:
                evidence.append(
                    f"{generic_pct:.0f}% of names are generic (Manager/Handler/process_/handle_)"
                )
                score = max(score, 4)
            elif generic_pct > 40:
                evidence.append(f"{generic_pct:.0f}% of names use generic AI patterns")
                score = max(score, 3)

        numbered = [n for n in class_names if re.match(r"^[A-Z]\w+\d+$", n)]
        if len(numbered) >= 3:
            evidence.append(f"Numbered class names: {', '.join(numbered[:5])}")
            score = max(score, 3)

    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 4: Error Handling Uniformity
# ---------------------------------------------------------------------------


def _criterion_error_handling(
    root: str, lang: str, files: list[str]
) -> tuple[int, list[str]]:
    score = 1
    evidence = []
    if not files:
        return 1, ["No source files found"]

    pattern_to_files: dict[str, list[str]] = defaultdict(list)
    total_catches = 0

    for f in files:
        content = _read(f)
        rel = _rel(f, root)
        ext = os.path.splitext(f)[1]

        if ext in JS_EXTS:
            catches = re.findall(r"}\s*catch\s*\([^)]*\)\s*\{([^}]{0,300})", content)
        else:
            catches = re.findall(r"except\s+[^:]+:\s*\n((?:\s+.+\n?){1,3})", content)

        for raw in catches:
            cleaned = re.sub(r"""["\'].*?["\']""", "'MSG'", raw.strip())
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            pattern_to_files[cleaned].append(rel)
            total_catches += 1

    if total_catches == 0:
        return 1, ["No try/catch or try/except blocks found"]

    counter = Counter({k: len(v) for k, v in pattern_to_files.items()})
    top = counter.most_common(1)
    if top:
        top_pattern, top_count = top[0]
        top_pct = (top_count / total_catches) * 100
        if top_pct > 60 and total_catches >= 5:
            score = 5
        elif top_pct > 40 and total_catches >= 5:
            score = 4
        elif top_pct > 25 and total_catches >= 5:
            score = 3
        if top_count >= 3:
            unique_files = list(dict.fromkeys(pattern_to_files[top_pattern]))[:5]
            evidence.append(
                f"Most common catch pattern repeated {top_count}/{total_catches} times "
                f"({top_pct:.0f}%) in: {', '.join(unique_files)}"
            )

    if lang == "js":
        sf_files = []
        for f in [f for f in files if os.path.splitext(f)[1] in JS_EXTS]:
            content = _read(f)
            for c in re.findall(r"}\s*catch\s*\([^)]*\)\s*\{([^}]{0,300})", content):
                if re.search(r"return\s*\{[^}]*success:\s*false", c):
                    sf_files.append(_rel(f, root))
        if len(sf_files) >= 3:
            evidence.append(
                f"{len(sf_files)} identical `{{ success: false }}` catch returns in: "
                + ", ".join(list(dict.fromkeys(sf_files))[:5])
            )
            score = max(score, 4)

    if lang == "python":
        bare_files = []
        pass_count = 0
        pass_examples: list[str] = []
        for f in [f for f in files if os.path.splitext(f)[1] == ".py"]:
            content = _read(f)
            rel = _rel(f, root)
            if re.findall(r"except\s*:", content):
                bare_files.append(rel)
            blocks = re.findall(r"except\s+\w+.*:\s*\n\s+pass\b", content)
            if blocks:
                pass_count += len(blocks)
                if len(pass_examples) < 5:
                    pass_examples.append(rel)
        if len(bare_files) >= 3:
            evidence.append(
                f"Bare `except:` in {len(bare_files)} files: {', '.join(bare_files[:5])}"
            )
            score = max(score, 3)
        if pass_count >= 5:
            evidence.append(
                f"{pass_count} `except: pass` blocks: {', '.join(pass_examples)}"
            )
            score = max(score, 4)

    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 5: Dead Code / Unused Imports / Test Quality
# ---------------------------------------------------------------------------


def _criterion_dead_code(
    root: str, lang: str, files: list[str]
) -> tuple[int, list[str]]:
    score = 1
    evidence = []

    # Unused lib directories (JS)
    if lang == "js":
        lib_dir = os.path.join(root, "lib")
        app_dirs = [
            os.path.join(root, d)
            for d in [
                "components",
                "views",
                "app",
                "src/components",
                "src/app",
                "src/views",
                "pages",
                "src/pages",
            ]
            if os.path.isdir(os.path.join(root, d))
        ]
        if os.path.isdir(lib_dir) and app_dirs:
            all_app = "\n".join(
                _read(f) for ad in app_dirs for f in _find_files(ad, JS_EXTS)
            )
            unused_lib = [
                f"lib/{e}/"
                for e in sorted(os.listdir(lib_dir))
                if os.path.isdir(os.path.join(lib_dir, e))
                and not any(p in all_app for p in [f"lib/{e}", f"'{e}", f"/{e}/"])
            ]
            if unused_lib:
                evidence.append(
                    f"{len(unused_lib)} lib dirs never imported: {', '.join(unused_lib[:8])}"
                )
                score = (
                    5 if len(unused_lib) >= 10 else (4 if len(unused_lib) >= 5 else 3)
                )

    # Unused imports
    unused_imports: list[tuple[str, str]] = []
    if lang == "js":
        for f in [f for f in files if os.path.splitext(f)[1] in JS_EXTS]:
            content = _read(f)
            rel = _rel(f, root)
            for line in content.split("\n"):
                stripped = line.strip()
                if re.match(r"import\s+type\s", stripped) or stripped.startswith(
                    "export"
                ):
                    continue
                m = re.match(r"""import\s*\{([^}]+)\}\s*from\s*['"]""", stripped)
                if m:
                    for raw in m.group(1).split(","):
                        name = raw.strip().split(" as ")[-1].strip()
                        if name and not raw.strip().startswith("type "):
                            if (
                                len(
                                    re.findall(r"\b" + re.escape(name) + r"\b", content)
                                )
                                <= 1
                            ):
                                unused_imports.append((rel, name))
    elif lang == "python":
        for f in [f for f in files if os.path.splitext(f)[1] == ".py"]:
            if os.path.basename(f) == "__init__.py":
                continue
            content = _read(f)
            rel = _rel(f, root)
            if "__all__" in content:
                continue
            for line in content.split("\n"):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                m = re.match(r"from\s+\S+\s+import\s+(.+)", stripped)
                if m:
                    for raw in m.group(1).split(","):
                        name = raw.strip().split(" as ")[-1].strip()
                        if name and name != "*" and not name.startswith("("):
                            if (
                                len(
                                    re.findall(r"\b" + re.escape(name) + r"\b", content)
                                )
                                <= 1
                            ):
                                unused_imports.append((rel, name))

    if unused_imports:
        examples = [f"{f}: `{n}`" for f, n in unused_imports[:5]]
        evidence.append(
            f"{len(unused_imports)} unused imports. Examples: {'; '.join(examples)}"
        )
        score = max(score, 4 if len(unused_imports) >= 15 else 3)

    # Placeholder implementations
    placeholder_count = 0
    placeholder_examples: list[str] = []
    for f in files:
        content = _read(f)
        ext = os.path.splitext(f)[1]
        rel = _rel(f, root)
        ph = 0
        if ext == ".py":
            ph += len(
                re.findall(
                    r"def\s+\w+\s*\([^)]*\)\s*(?:->.*?)?:\s*\n\s+(?:pass\s*$|\.\.\.)",
                    content,
                    re.M,
                )
            )
            ph += len(re.findall(r"raise\s+NotImplementedError", content))
        if ext in JS_EXTS:
            ph += len(
                re.findall(
                    r'throw\s+new\s+Error\s*\(\s*["\'](?:not implemented|TODO|FIXME)',
                    content,
                    re.I,
                )
            )
            ph += len(re.findall(r"(?://|/\*)\s*TODO:?\s*implement", content, re.I))
        if ph > 0:
            placeholder_count += ph
            if len(placeholder_examples) < 5:
                placeholder_examples.append(rel)

    if placeholder_count >= 10:
        evidence.append(
            f"{placeholder_count} placeholder implementations (pass/NotImplementedError/TODO): "
            + ", ".join(placeholder_examples)
        )
        score = max(score, 4)
    elif placeholder_count >= 3:
        evidence.append(
            f"{placeholder_count} placeholder implementations: "
            + ", ".join(placeholder_examples)
        )
        score = max(score, 3)

    # Over-engineering for project size
    total_source = len(files)
    if lang == "js" and total_source <= 30:
        di_hits = sum(
            1
            for f in files
            if re.search(
                r"(?:inversify|tsyringe|typedi|@injectable|@inject\b|container\.resolve)",
                _read(f),
                re.I,
            )
        )
        if di_hits >= 2:
            evidence.append(
                f"DI container in small codebase ({total_source} files, {di_hits} DI files)"
            )
            score = max(score, 3)

    if lang == "python" and total_source <= 30:
        abc_hits = sum(
            1 for f in files if "ABC" in _read(f) or "abstractmethod" in _read(f)
        )
        if abc_hits >= 3:
            evidence.append(
                f"Heavy ABC usage ({abc_hits} files) in small codebase ({total_source} files)"
            )
            score = max(score, 3)

    # Test quality
    test_files = [f for f in _find_files(root, ALL_SOURCE_EXTS) if _is_test(f, root)]
    test_lib_pats = [
        "vitest",
        "jest",
        "testing-library",
        "@testing",
        "pytest",
        "unittest",
        "org.junit",
    ]

    fake_tests = 0
    fake_examples: list[str] = []
    for f in test_files:
        content = _read(f)
        rel = _rel(f, root)
        imports = re.findall(r"""(?:from|import)\s+['"]?([^'"\s;]+)""", content)
        src_imports = [i for i in imports if not any(x in i for x in test_lib_pats)]
        has_assertions = bool(
            re.search(
                r"(expect\(|assert |self\.assert|assertEquals|assertTrue)", content
            )
        )
        if not src_imports and has_assertions:
            fake_tests += 1
            if len(fake_examples) < 3:
                fake_examples.append(rel)
    if fake_tests > 0:
        evidence.append(
            f"{fake_tests} test files don't import the module they test: {', '.join(fake_examples)}"
        )
        score = max(score, 4 if fake_tests >= 5 else 3)

    # No edge-case tests
    if test_files:
        edge_re = re.compile(
            r"(?:throw|reject|error|fail|invalid|unauthorized|forbidden|null|undefined|"
            r"empty|missing|timeout|overflow|negative|boundary|edge|not found|bad request|exception)",
            re.I,
        )
        checked = min(len(test_files), 30)
        tests_with_edge = sum(
            1
            for f in test_files[:checked]
            if any(
                edge_re.search(d)
                for d in re.findall(
                    r"""(?:it|test|describe)\s*\(\s*['"]([^'"]+)""", _read(f)
                )
            )
        )
        if checked >= 5 and tests_with_edge == 0:
            evidence.append(
                f"0/{checked} test files mention failure/edge-case scenarios"
            )
            score = max(score, 4)
        elif checked >= 10 and tests_with_edge < checked * 0.1:
            evidence.append(
                f"Only {tests_with_edge}/{checked} test files contain edge-case scenarios"
            )
            score = max(score, 3)

    # Mirror tests (Python)
    if test_files and lang == "python":
        source_funcs: set[str] = set()
        for f in [f for f in files if not _is_test(f, root) and f.endswith(".py")]:
            source_funcs.update(re.findall(r"def\s+(\w+)\s*\(", _read(f)))
        test_funcs: set[str] = set()
        for f in [f for f in test_files if f.endswith(".py")]:
            test_funcs.update(re.findall(r"def\s+(test_\w+)\s*\(", _read(f)))
        if len(test_funcs) >= 5 and source_funcs:
            mirrored = sum(1 for tf in test_funcs if tf[len("test_") :] in source_funcs)
            if mirrored / len(test_funcs) >= 0.8:
                evidence.append(
                    f"{mirrored}/{len(test_funcs)} test functions directly mirror source names "
                    "(one-per-function pattern)"
                )
                score = max(score, 4)

    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 6: Git History
# ---------------------------------------------------------------------------

GENERIC_MSG_PATTERNS = [
    re.compile(
        r"^(add|update|fix|create|implement|refactor)\s+"
        r"(feature|code|bug|file|component|module|stuff|things)s?$",
        re.I,
    ),
    re.compile(
        r"^(initial commit|first commit|wip|work in progress|"
        r"save|checkpoint|changes|updates)$",
        re.I,
    ),
    re.compile(r"^(add|update|fix)\s+\w+$", re.I),
    re.compile(r"^(misc|cleanup|minor|patch|hotfix)$", re.I),
]


def _parse_git_log(root: str) -> tuple[list[dict], list[str]]:
    """Parse git log filtering  Returns (commits, dates)."""
    raw = _run_git(
        ["log", "--format=%H|%an|%ae|%s", "--shortstat", "--no-merges", "-200"], root
    )
    if not raw:
        return [], []

    commits: list[dict] = []
    current = None
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        stat = re.match(r"(\d+)\s+files?\s+changed", line)
        if stat and current is not None:
            ins = (
                int(m.group(1)) if (m := re.search(r"(\d+)\s+insertions?", line)) else 0
            )
            dels = (
                int(m.group(1)) if (m := re.search(r"(\d+)\s+deletions?", line)) else 0
            )
            commits.append(
                {
                    "msg": current["msg"],
                    "files": int(stat.group(1)),
                    "ins": ins,
                    "del": dels,
                }
            )
            current = None
        elif "|" in line:
            parts = line.split("|", 3)
            if len(parts) == 4:
                _, name, email, msg = parts
                current = {
                    "msg": msg.strip(),
                }
    dates_raw = _run_git(
        ["log", "--format=%H|%an|%ae|%ai", "--no-merges", "-200"], root
    )
    dates: list[str] = []
    if dates_raw:
        for dline in dates_raw.split("\n"):
            dline = dline.strip()
            if not dline or "|" not in dline:
                continue
            parts = dline.split("|", 3)
            if len(parts) == 4:
                _, name, email, date_str = parts
                dates.append(date_str.strip()[:10])

    return commits, dates


def _criterion_git_history(root: str, files: list[str]) -> tuple[int, list[str]]:
    score = 1
    evidence = []

    if not os.path.isdir(os.path.join(root, ".git")):
        return 1, ["No .git directory found"]

    commits, dates = _parse_git_log(root)
    if not commits:
        return 1, ["Could not read git log (or all commits)"]

    # Large commits (50+ files)
    large = [c for c in commits if c["files"] >= 50]
    if large:
        evidence.append(
            f"{len(large)}/{len(commits)} commits touch 50+ files. "
            f"Largest: {max(c['files'] for c in large)} files"
        )
        score = max(score, 4 if len(large) >= 3 else 3)

    # Massive commits (500+ lines)
    massive = [c for c in commits if c["ins"] + c["del"] >= 500]
    if len(massive) > len(commits) * 0.5 and len(commits) >= 3:
        evidence.append(
            f"{len(massive)}/{len(commits)} commits have 500+ lines changed"
        )
        score = max(score, 4)

    # Generic commit messages
    generic_count = 0
    generic_examples: list[str] = []
    seen_examples: set[str] = set()
    for c in commits:
        if any(pat.match(c["msg"].strip().lower()) for pat in GENERIC_MSG_PATTERNS):
            generic_count += 1
            msg = c["msg"].strip()
            if msg not in seen_examples and len(generic_examples) < 5:
                generic_examples.append(msg)
                seen_examples.add(msg)

    if generic_count > 0:
        pct = (generic_count / len(commits)) * 100
        evidence.append(
            f"{generic_count}/{len(commits)} generic commit messages ({pct:.0f}%): "
            + ", ".join(f'"{m}"' for m in generic_examples)
        )
        score = max(score, 5 if pct > 60 else (4 if pct > 30 else 3))

    # Few commits for large codebase
    if len(commits) <= 5 and len(files) > 50:
        evidence.append(f"Only {len(commits)} commits for {len(files)} source files")
        score = max(score, 4)

    # All commits on same day(s)
    if dates:
        unique_days = len(set(dates))
        if len(dates) >= 5 and unique_days <= 2:
            evidence.append(
                f"All {len(dates)} commits happened within {unique_days} day(s)"
            )
            score = max(score, 4)

    return score, evidence


# ---------------------------------------------------------------------------
# Smart sampling — flagged files first
# ---------------------------------------------------------------------------


def _extract_flagged_paths(root: str, criteria_results: dict) -> set[str]:
    """Extract absolute file paths mentioned in static findings."""
    flagged: set[str] = set()
    for crit_data in criteria_results.values():
        for e in crit_data.get("evidence", []):
            if " in " not in e:
                continue
            rel = e.split(" in ")[-1].strip()
            # Handle "file1, file2, file3" at end of evidence
            for part in rel.split(","):
                part = part.strip()
                abs_path = os.path.join(root, part)
                if os.path.isfile(abs_path):
                    flagged.add(abs_path)
    return flagged


def _smart_sample_vibe(
    root: str, files: list[str], criteria_results: dict, token_budget: int = 8000
) -> str:
    """
    Build code samples for LLM analysis.
    Pass 1: files flagged by static analysis (guaranteed, labelled)
    Pass 2: remaining budget filled by scored non-flagged files

    Also includes up to 3 markdown files for documentation signal.
    """
    non_test = [f for f in files if not _is_test(f, root)]
    if not non_test:
        return ""

    char_budget = token_budget * 4
    flagged_paths = _extract_flagged_paths(root, criteria_results)

    snippets: list[str] = []
    total_chars = 0
    included: set[str] = set()

    def _add_snippet(f: str, flagged: bool, max_lines: int = 60) -> bool:
        nonlocal total_chars
        content = _read(f)
        if not content.strip():
            return False
        lines = content.split("\n")[:max_lines]
        tag = " [FLAGGED]" if flagged else ""
        chunk = (
            f"\n--- {_rel(f, root)}{tag} ---\n"
            + "\n".join(f"{i + 1}: {l}" for i, l in enumerate(lines))
            + "\n"
        )
        if total_chars + len(chunk) > char_budget:
            return False
        snippets.append(chunk)
        total_chars += len(chunk)
        included.add(f)
        return True

    # Pass 1: flagged files
    for f in non_test:
        if f in flagged_paths:
            _add_snippet(f, flagged=True, max_lines=80)

    # Pass 2: scored non-flagged files
    scores: dict[str, int] = defaultdict(int)
    remaining = [f for f in non_test if f not in included]

    for f in remaining:
        rel_lower = _rel(f, root).lower()
        # Boost files with naming/structure signals
        if any(
            kw in rel_lower
            for kw in [
                "service",
                "controller",
                "handler",
                "util",
                "helper",
                "manager",
                "model",
            ]
        ):
            scores[f] += 15

    seen_dirs: set[str] = set()
    for f in sorted(remaining, key=lambda x: -scores[x]):
        parts = _rel(f, root).split(os.sep)
        top = parts[0] if len(parts) > 1 else "__root__"
        if top not in seen_dirs:
            scores[f] += 10
            seen_dirs.add(top)

    for f in sorted(remaining, key=lambda x: -scores[x]):
        if not _add_snippet(f, flagged=False):
            break

    # Add markdown docs (documentation criterion needs them)
    for md in _find_files(root, [".md"])[:3]:
        content = _read(md)
        if content.count("\n") < 3:
            continue
        lines = content.split("\n")[:30]
        chunk = f"\n--- {_rel(md, root)} (markdown) ---\n" + "\n".join(lines) + "\n"
        if total_chars + len(chunk) <= char_budget:
            snippets.append(chunk)
            total_chars += len(chunk)

    return "".join(snippets)


# ---------------------------------------------------------------------------
# LLM analysis — single call per repo
# ---------------------------------------------------------------------------

_LLM_SYSTEM = """You are an expert at detecting AI-generated (vibe-coded) repositories.
You analyze code samples to determine if they were written organically by human developers
or generated by AI coding assistants (Copilot, Cursor, ChatGPT, Claude, etc.).

Scoring rubric (1=definitely human, 5=definitely AI-generated):
  1 — strong human signals: domain jargon, iterative naming, inconsistent formatting, real bug fixes
  2 — mostly human with some generic patterns
  3 — mixed signals, ambiguous
  4 — mostly AI: uniform structure, generic naming, perfect docs, large infrequent commits
  5 — strong AI signals: narration comments, planning docs, identical error handling, all commits same day

Rules:
  - Base your assessment ONLY on what you can see in the provided code and evidence
  - Distinguish between AI-generated code and well-written human code (they can look similar)
  - Generic naming alone is weak evidence — look for combinations of signals
  - Return ONLY valid JSON — no markdown, no explanation outside JSON"""

_LLM_AI_SIGNALS = """
  - Narration comments: "// Import the module", "// Define the function", "// Handle the error"
  - Agent planning docs at repo root: ARCHITECTURE.md, IMPLEMENTATION_PLAN.md, BACKLOG.md
  - Emoji-heavy markdown with perfect structure and zero TODOs
  - Identical error handling pattern copy-pasted across all files
  - Generic naming: handleUserInput, processData, executeOperation, ManagerHelper
  - Placeholder/stub implementations: pass, return null, throw NotImplementedError
  - Tests that mirror implementation 1:1 with no edge cases or failure paths
  - Large single commits with fully-formed code (500+ lines, 50+ files)
  - All commits within 1-2 days for a large codebase
  - Auto-generated markers: "// Generated component", "@generated", "// Code generated DO NOT EDIT"
  - Over-engineered abstractions (DI containers, ABCs) in small projects
"""

_LLM_HUMAN_SIGNALS = """
  - Domain-specific variable names reflecting real business logic
  - Comments explaining WHY not WHAT, with occasional frustration or humor
  - Inconsistent formatting across files (different developers, different days)
  - Iterative naming choices visible across commits
  - Real bug-fix comments referencing specific tickets or issues
  - Stale/incomplete documentation with TODOs and TBDs
  - Test cases that cover weird edge cases only a real user would discover
  - Small incremental commits with focused changes
"""


def _llm_vibe_analysis(
    code_samples: str, criteria_results: dict, client, model: str, lang: str
) -> dict:
    """Single LLM call per repo for AI-generation detection."""

    auto_summary = "\n".join(
        f"  {k}: score={v['score']}/5 — {'; '.join(v['evidence'][:3])}"
        for k, v in criteria_results.items()
    )

    prompt = f"""Repository language: {lang}

Automated scanner results (6 criteria, 1=human, 5=AI-generated):
{auto_summary}

AI signals to look for:
{_LLM_AI_SIGNALS}

Human signals to look for:
{_LLM_HUMAN_SIGNALS}

Code samples ([FLAGGED] = files the automated scanner flagged):
{code_samples}

Tasks:
1. Review each automated criterion score — confirm or correct it based on the code
2. Identify AI or human signals the scanner missed
3. Give an overall confidence score

Return this exact JSON:
{{
  "confidence": <0.0-1.0, probability code is AI-generated>,
  "verdict": "LIKELY_AI" | "MIXED" | "LIKELY_HUMAN",
  "per_criterion": {{
    "documentation":   {{"agree": true/false, "refined_score": 1-5, "note": "if refined_score > automated score: concrete evidence with specific file paths and line numbers; otherwise empty string"}},
    "comments":        {{"agree": true/false, "refined_score": 1-5, "note": "if refined_score > automated score: concrete evidence with specific file paths and line numbers; otherwise empty string"}},
    "naming":          {{"agree": true/false, "refined_score": 1-5, "note": "if refined_score > automated score: concrete evidence with specific file paths and line numbers; otherwise empty string"}},
    "error_handling":  {{"agree": true/false, "refined_score": 1-5, "note": "if refined_score > automated score: concrete evidence with specific file paths and line numbers; otherwise empty string"}},
    "dead_code":       {{"agree": true/false, "refined_score": 1-5, "note": "if refined_score > automated score: concrete evidence with specific file paths and line numbers; otherwise empty string"}},
    "git_history":     {{"agree": true/false, "refined_score": 1-5, "note": "if refined_score > automated score: concrete evidence with specific file paths and line numbers; otherwise empty string"}}
  }},
  "false_positives": ["automated findings that are NOT real AI signals"],
  "missed_signals": ["AI or human signals the scanner missed"],
  "ai_signals": ["specific evidence of AI generation seen in the code"],
  "human_signals": ["specific evidence of human authorship seen in the code"],
  "summary": "2-3 sentence overall assessment"
}}"""

    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0,
                messages=[
                    {"role": "system", "content": _LLM_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = resp.choices[0].message.content.strip()
            raw = re.sub(r"```(?:json)?", "", raw).strip()
            start, end = raw.find("{"), raw.rfind("}") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]
            return json.loads(raw)
        except json.JSONDecodeError:
            if attempt == 2:
                return {"error": "JSON parse failed", "verdict": "", "confidence": 0}
        except Exception as exc:
            return {"error": str(exc), "verdict": "", "confidence": 0}
    return {}


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def _get_verdict(total: int, llm_confidence: float = 0.0, llm_verdict: str = "") -> str:
    """
    Derive verdict from static score, adjusted by LLM confidence when available.
    LLM can shift a MEDIUM → HIGH or LOW → MEDIUM when confidence is strong (>0.75).
    """
    if total <= 10:
        static = "LOW"
    elif total <= 17:
        static = "MEDIUM"
    else:
        static = "HIGH"

    if not llm_verdict:
        return static

    # Strong LLM disagreement can shift verdict one step
    if llm_confidence >= 0.75:
        if llm_verdict == "LIKELY_AI" and static == "LOW":
            return "MEDIUM"
        if llm_verdict == "LIKELY_AI" and static == "MEDIUM":
            return "HIGH"
        if llm_verdict == "LIKELY_HUMAN" and static == "HIGH":
            return "MEDIUM"
        if llm_verdict == "LIKELY_HUMAN" and static == "MEDIUM":
            return "LOW"

    return static


# ---------------------------------------------------------------------------
# Build final details
# ---------------------------------------------------------------------------

_EVIDENCE_SKIP = (
    "Documentation appears natural",
    "No markdown documentation found",
    "Comment patterns appear natural",
    "Naming patterns appear domain-specific",
    "Error handling shows natural variety",
    "No try/catch or try/except blocks found",
    "No significant dead code",
    "Git history appears natural",
    "Analyzed ",
    "No source files found",
    "No .git directory found",
    "Could not read git log",
)

_CRIT_TO_CATEGORY: dict[str, tuple[str, str]] = {
    # criterion → (display label, "critical"|"signal")
    "documentation": ("Planning/perfect docs", "signal"),
    "comments": ("Narration comments", "critical"),
    "naming": ("Generic naming", "signal"),
    "error_handling": ("Identical error handling", "signal"),
    "dead_code": ("Unused/placeholder code", "signal"),
    "git_history": ("Large/generic commits", "critical"),
}

_OVER_ENG_KW = ("DI container", "over-abstraction", "over-engineer", "ABC usage")
_MIRROR_TEST_KW = (
    "don't import the module",
    "edge-case",
    "mirror source function",
    "one-per-function",
)
_PLAN_DOC_KW = ("Agent-generated planning docs",)


def _categorize(crit: str, text: str) -> tuple[str, str] | None:
    if any(text.startswith(p) for p in _EVIDENCE_SKIP):
        return None
    if crit == "documentation":
        if any(k in text for k in _PLAN_DOC_KW):
            return "Planning docs", "signal"
        return "Perfect/emoji markdown", "signal"
    if crit == "dead_code":
        if any(k in text for k in _OVER_ENG_KW):
            return "Over-engineering", "critical"
        if any(k in text for k in _MIRROR_TEST_KW):
            return "Mirror tests", "critical"
        return "Unused/placeholder code", "signal"
    label, kind = _CRIT_TO_CATEGORY.get(crit, ("Other", "signal"))
    return label, kind


def _build_final_details(result: dict) -> tuple[list[str], list[str]]:
    """Return (critical_lines, signal_lines) deduplicated."""
    critical: list[str] = []
    signals: list[str] = []

    for crit_name, crit_data in result.get("criteria", {}).items():
        for e in crit_data.get("evidence", []):
            cat = _categorize(crit_name, e)
            if cat:
                label, kind = cat
                (critical if kind == "critical" else signals).append(e)

    return critical, signals


# ---------------------------------------------------------------------------
# Per-repo orchestrator
# ---------------------------------------------------------------------------

CRITERIA_KEYS = [
    "documentation",
    "comments",
    "naming",
    "error_handling",
    "dead_code",
    "git_history",
]


def _check_repo(
    owner: str,
    repo: str,
    token: str,
    clone_base: str,
    verbose_log=None,
    client=None,
    model: str = DEFAULT_MODEL,
    skip_llm: bool = True,
    sample_tokens: int = 8000,
    existing_repo_path: str | None = None,
) -> dict:

    result = {
        "repo": repo,
        "language": "unknown",
        "total_score": 0,
        "verdict": "LOW",
        "criteria": {k: {"score": 1, "evidence": []} for k in CRITERIA_KEYS},
        "files_scanned": 0,
        "error": None,
        "llm_verdict": "",
        "llm_confidence": 0.0,
        "llm_summary": "",
        "final_details_critical": [],
        "final_details_signals": [],
        "final_details_count": 0,
    }

    owns_clone = not existing_repo_path
    clone_dir = ""

    if existing_repo_path:
        root = str(Path(existing_repo_path).resolve())
        if not os.path.isdir(root):
            result["error"] = f"repository path does not exist: {root}"
            return result
        if verbose_log:
            verbose_log(f"    Using existing repo at {root} ...")
    else:
        clone_dir = os.path.join(clone_base, repo)
        if os.path.exists(clone_dir):
            shutil.rmtree(clone_dir, ignore_errors=True)

        if verbose_log:
            verbose_log(f"    Cloning {owner}/{repo} ...")

        ok, err = _clone_repo(owner, repo, clone_dir, token)
        if not ok:
            result["error"] = f"clone failed: {err}" if err else "clone failed"
            return result

        root = clone_dir

    source_files = _find_files(root, ALL_SOURCE_EXTS)
    if not source_files:
        result["error"] = "no source files found"
        if owns_clone:
            shutil.rmtree(clone_dir, ignore_errors=True)
        return result

    lang = _detect_language(source_files)
    result["language"] = lang
    result["files_scanned"] = len(source_files)

    if verbose_log:
        verbose_log(f"    Language: {lang} | {len(source_files)} source files")

    # Run all 6 static criteria
    s1, e1 = _criterion_documentation(root, lang)
    s2, e2 = _criterion_comments(root, lang, source_files)
    s3, e3 = _criterion_naming(root, lang, source_files)
    s4, e4 = _criterion_error_handling(root, lang, source_files)
    s5, e5 = _criterion_dead_code(root, lang, source_files)
    s6, e6 = _criterion_git_history(root, source_files)

    result["criteria"] = {
        "documentation": {"score": s1, "evidence": e1},
        "comments": {"score": s2, "evidence": e2},
        "naming": {"score": s3, "evidence": e3},
        "error_handling": {"score": s4, "evidence": e4},
        "dead_code": {"score": s5, "evidence": e5},
        "git_history": {"score": s6, "evidence": e6},
    }

    # LLM — single call per repo
    if not skip_llm and client:
        if verbose_log:
            verbose_log(f"    Running LLM AI-detection for {repo} ...")
        code_samples = _smart_sample_vibe(
            root, source_files, result["criteria"], sample_tokens
        )
        if code_samples:
            llm = _llm_vibe_analysis(
                code_samples, result["criteria"], client, model, lang
            )
            result["llm_analysis"] = llm
            if isinstance(llm, dict) and "verdict" in llm:
                result["llm_verdict"] = llm.get("verdict", "")
                result["llm_confidence"] = llm.get("confidence", 0.0)
                result["llm_summary"] = llm.get("summary", "")

                # Merge per-criterion notes + refined scores
                for crit_name, llm_crit in llm.get("per_criterion", {}).items():
                    if crit_name not in result["criteria"]:
                        continue
                    if isinstance(llm_crit, dict):
                        note = llm_crit.get("note", "")
                        static_score = result["criteria"][crit_name]["score"]
                        if note and llm_crit.get("refined_score", 0) > static_score:
                            result["criteria"][crit_name]["evidence"] = [note]
                        if "refined_score" in llm_crit:
                            result["criteria"][crit_name]["llm_score"] = llm_crit[
                                "refined_score"
                            ]

                # Store false positives and missed signals
                result["llm_false_positives"] = llm.get("false_positives", [])
                result["llm_missed_signals"] = llm.get("missed_signals", [])

    # Compute total using LLM-refined scores where available
    total = 0
    for k in CRITERIA_KEYS:
        crit = result["criteria"][k]
        final_score = crit.get("llm_score", crit["score"])
        crit["final_score"] = final_score
        total += final_score

    result["total_score"] = total
    result["verdict"] = _get_verdict(
        total, result["llm_confidence"], result["llm_verdict"]
    )

    # Build final details
    critical, signals = _build_final_details(result)
    result["final_details_critical"] = critical
    result["final_details_signals"] = signals
    result["final_details_count"] = len(critical) + len(signals)

    if owns_clone:
        shutil.rmtree(clone_dir, ignore_errors=True)
    return result
