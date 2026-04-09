"""
Production quality assessment across multiple repos — clone-based, with optional LLM.

Excludes files authored by turing / pr-sourcing accounts (via git blame).
Only evaluates code written by the original developers.

Supports Python and JavaScript/TypeScript repos (auto-detected per repo).

10 Criteria (each scored 1-5, 1=excellent, 5=poor):
  1. Error Handling       — bare except, broad except, large try, mutable defaults,
                            except:pass, empty catch, custom types, retry, graceful
  2. Logging              — structured with severity, context, debugger, no-logging files
  3. Configuration        — magic numbers, hardcoded IP/port/URLs, env vars
  4. Database Practices   — parameterized queries, migrations, pooling, N+1 in loops
  5. API Design           — consistent naming, schemas, pagination, uniform errors
  6. Resource Management  — close/cleanup, HTTP timeouts, open() without with
  7. Architecture         — function length, func count/file, imports, nesting, circular deps
  8. Testing              — failure paths covered, integration tests, deterministic
  9. CI/CD & Deployment   — CI runs tests/lint, entrypoint, build artifacts, .env committed
 10. Tech Debt            — TODOs (tracked/untracked), FIXME/HACK, loose equality,
                            any type, 6+ param functions, copy-paste blocks

LLM deep analysis (--skip-llm to disable):
  Sends smart-sampled code + all automated findings to GPT in a single call per repo.
  LLM validates all 10 scores, fills gaps static checks miss, adds [LLM] evidence.

Grades: CLEAN (<=10) | MINOR (<=18) | MODERATE (<=28) | CRITICAL (>28)

Usage:
    python tools/production_quality_check.py \\
        --owner Khaleddnfr \\
        --repos "core,eventify,..." \\
        [--skip-llm] [--model gpt-4o]
"""

import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)

DEFAULT_MODEL = "gpt-4o"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PYTHON_EXTS = {".py"}
JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
SOURCE_EXTS = PYTHON_EXTS | JS_EXTS
CONFIG_EXTS = {".json", ".yml", ".yaml", ".toml", ".cfg", ".env", ".ini"}
ALL_EXTS = SOURCE_EXTS | CONFIG_EXTS

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
    "coverage",
    ".nyc_output",
}

TOOLGEN_DIRS = {
    "migrations",
    "generated",
    "generated-sources",
    "__generated__",
    "typechain",
    "typechain-types",
    ".prisma",
}

# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def _detect_language(files: list[str]) -> str:
    """Return 'python' or 'js' based on dominant file extension count."""
    py_count = sum(1 for f in files if os.path.splitext(f)[1] in PYTHON_EXTS)
    js_count = sum(1 for f in files if os.path.splitext(f)[1] in JS_EXTS)
    return "python" if py_count >= js_count else "js"


# ---------------------------------------------------------------------------
# Per-language pattern sets
# ---------------------------------------------------------------------------

# Error handling
PATTERNS_ERROR = {
    "python": {
        "bare_except": re.compile(r"except\s*:", re.M),
        "broad_except": re.compile(r"except\s+Exception\b", re.M),
        "except_pass": re.compile(r"except\s+\w+[^:]*:\s*\n\s+pass\b", re.M),
        "mutable_default": re.compile(r"def\s+\w+\([^)]*=\s*(\[\]|\{\}|set\(\))", re.M),
        "custom_error": re.compile(r"class\s+\w+(?:Error|Exception)\s*[\(:]", re.M),
    },
    "js": {
        "empty_catch": re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}", re.M),
        "broad_catch": re.compile(r"catch\s*\(\s*(?:e|err|error)?\s*\)\s*\{", re.M),
        "custom_error": re.compile(r"extends\s+(?:Error|Exception)", re.M),
    },
    "common": {
        "retry": re.compile(
            r"(?:retry|retries|backoff|exponential|@retry|tenacity)", re.I
        ),
        "graceful": re.compile(r"(?:fallback|graceful|degrade|circuit.?breaker)", re.I),
    },
}

# Logging
PATTERNS_LOG = {
    "python": {
        "bare": re.compile(r"\bprint\s*\(", re.M),
        "structured": re.compile(r"(?:logging|loguru|structlog)\.\w+\s*\(", re.I),
        "context": re.compile(
            r"(?:request.?id|correlation.?id|trace.?id|extra\s*=)", re.I
        ),
    },
    "js": {
        "bare": re.compile(r"console\.(?:log|warn|error|info)\s*\(", re.M),
        "structured": re.compile(
            r"(?:winston|pino|bunyan|morgan|logger\.\w+)\s*\(", re.I
        ),
        "debugger": re.compile(r"\bdebugger\b", re.M),
        "context": re.compile(r"(?:requestId|correlationId|traceId|context)", re.I),
    },
}

# Configuration
PATTERNS_CONFIG = {
    "common": {
        "magic_num": re.compile(
            r"(?:timeout|port|limit|max|min|size|count|interval|delay|retries)\s*[=:]\s*\d{2,}",
            re.I,
        ),
        "hardcoded_url": re.compile(
            r"""https?://(?!localhost|127\.0\.0\.1|example\.com)[^\s'"]{15,}"""
        ),
        "ip_addr": re.compile(
            r"\b(?!127\.0\.0\.1|0\.0\.0\.0)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
        ),
    },
    "python": {
        "env_var": re.compile(r"os\.(?:environ|getenv)\s*\(", re.I),
    },
    "js": {
        "env_var": re.compile(r"process\.env\.", re.I),
    },
}

# Database
PATTERNS_DB = {
    "python": {
        "raw_query": re.compile(
            r"""(?:execute|query)\s*\(\s*(?:f["']|["']\s*%|["']\s*\+)""", re.I
        ),
        "orm": re.compile(r"(?:sqlalchemy|django\.db|peewee|tortoise|databases)", re.I),
        "pool": re.compile(r"(?:pool|Pool|pool_size|max_connections|QueuePool)", re.I),
        "raw_connect": re.compile(
            r"(?:psycopg2\.connect|mysql\.connector\.connect|sqlite3\.connect|MongoClient\s*\()",
            re.I,
        ),
    },
    "js": {
        "raw_query": re.compile(
            r"""(?:query|execute|raw)\s*\(\s*(?:['"`]\s*\$\{|['"`].*?\+|`[^`]*\$\{)""",
            re.I,
        ),
        "orm": re.compile(r"(?:sequelize|typeorm|prisma|mongoose|knex|drizzle)", re.I),
        "pool": re.compile(r"(?:pool|createPool|connectionPool|Pool\s*\()", re.I),
        "raw_connect": re.compile(
            r"(?:createConnection|\.connect\s*\(|MongoClient\s*\(|mysql\.createConnection)",
            re.I,
        ),
    },
    "common": {
        "query_in_loop": re.compile(
            r"(?:\.execute|\.query|\.find|\.findOne|\.findMany|\.findAll|"
            r"\.fetch|\.fetchone|\.fetchall|\.get|\.select|\.where|"
            r"\.findById|\.aggregate|\.count|\.deleteOne|\.updateOne|"
            r"\.findUnique|\.findFirst)\s*\(",
            re.I,
        ),
        "loop_start": re.compile(
            r"^\s*(for\s|while\s|\.forEach\s*\(|\.map\s*\(|\.flatMap\s*\()"
        ),
    },
}

# Resource management
PATTERNS_RESOURCE = {
    "python": {
        "http_call": re.compile(r"requests\.(get|post|put|delete|patch|head)\s*\("),
        "bare_open": re.compile(r"(?<!\bwith\s)\bopen\s*\("),
        "cleanup": re.compile(
            r"(?:\.close\(\)|with\s+open|\bfinally\b|contextmanager)", re.I
        ),
        "timeout": re.compile(r"\btimeout\s*=", re.I),
    },
    "js": {
        "http_call": re.compile(r"\bfetch\s*\("),
        "cleanup": re.compile(
            r"(?:\.close\(\)|\.destroy\(\)|\.end\(\)|finally\s*\{|AbortController)",
            re.I,
        ),
        "timeout": re.compile(r"(?:timeout|AbortController|signal\s*:)", re.I),
    },
}

# API design
PATTERNS_API = {
    "python": {
        "route": re.compile(
            r"""(?:app|router)\.(?:get|post|put|delete|patch)\s*\(\s*['"]([^'"]+)""",
            re.I,
        ),
        "schema": re.compile(
            r"(?:pydantic|marshmallow|cerberus|jsonschema|fastapi)", re.I
        ),
        "pagination": re.compile(r"(?:page|limit|offset|per_page|cursor)\b", re.I),
    },
    "js": {
        "route": re.compile(
            r"""(?:app|router)\.(?:get|post|put|delete|patch)\s*\(\s*['"`]([^'"`]+)""",
            re.I,
        ),
        "nextjs_page": re.compile(r"pages/api/.+\.[jt]sx?$"),
        "nextjs_app": re.compile(r"(?:src/)?app/.+/route\.[jt]sx?$"),
        "schema": re.compile(
            r"(?:joi|zod|yup|ajv|class-validator|@ApiProperty|swagger)", re.I
        ),
        "pagination": re.compile(
            r"(?:page|limit|offset|pageSize|cursor|skip|take)\b", re.I
        ),
    },
}

# Tech debt
PATTERNS_DEBT = {
    "common": {
        "todo": re.compile(r"(?://|#|/\*)\s*TODO\b.*", re.I),
        "fixme": re.compile(r"(?://|#|/\*)\s*FIXME\b", re.I),
        "hack": re.compile(r"(?://|#|/\*)\s*HACK\b", re.I),
        "ticket": re.compile(r"(?:#[0-9]+|[A-Z]{2,}-[0-9]+|@\w+)"),
    },
    "python": {
        "long_params": re.compile(r"def\s+\w+\s*\(([^)]*)\)", re.M),
    },
    "js": {
        "long_params": re.compile(
            r"(?:function\s+\w+\s*\(([^)]*)\)|(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?\(([^)]*)\))",
            re.M,
        ),
        "loose_eq": re.compile(r"[^=!]==[^=]"),
    },
    "ts": {
        "any_type": re.compile(r":\s*any\b"),
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: str) -> str:
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.stdout.strip()
    except Exception:
        return ""


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
    if r.returncode != 0:
        return False, r.stderr.strip()
    return True, ""


def _find_files(root: str, extensions: set[str]) -> list[str]:
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in EXCLUDE_DIRS and d not in TOOLGEN_DIRS
        ]
        for f in filenames:
            if os.path.splitext(f)[1] in extensions:
                results.append(os.path.join(dirpath, f))
    return results


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


def _rel(path: str, root: str) -> str:
    return os.path.relpath(path, root)


def _is_test_file(path: str, root: str) -> bool:
    rel = _rel(path, root).lower()
    return any(p in rel for p in ["test", "spec", "__tests__", "tests/", "testing/"])


def _is_turing_file(filepath: str, root: str, patterns: list[str]) -> bool:
    """Return True if >50% of lines are authored by turing-pattern accounts."""
    if not patterns:
        return False
    blame = _run_git(["blame", "--porcelain", _rel(filepath, root)], root)
    if not blame:
        return False

    total_lines = 0
    turing_lines = 0
    author = ""
    email = ""

    for line in blame.split("\n"):
        if line.startswith("author "):
            author = line[7:].strip().lower()
        elif line.startswith("author-mail "):
            email = line[12:].strip().lower().strip("<>")
            combined = f"{author} {email}"
            is_turing = any(p in combined for p in patterns)
            total_lines += 1
            if is_turing:
                turing_lines += 1

    if total_lines == 0:
        return False
    return (turing_lines / total_lines) > 0.5


def _filter_non_turing_files(
    root: str, files: list[str], patterns: list[str], verbose_log=None
) -> list[str]:
    if not patterns:
        return files
    kept, excluded = [], 0
    for f in files:
        if _is_turing_file(f, root, patterns):
            excluded += 1
        else:
            kept.append(f)
    if verbose_log and excluded:
        verbose_log(
            f"      Excluded {excluded} turing-authored files, kept {len(kept)}"
        )
    return kept


def _count_params(param_str: str, is_python: bool = False) -> int:
    """Count meaningful parameters, stripping self/cls for Python."""
    params = [p.strip() for p in param_str.split(",") if p.strip()]
    if is_python:
        params = [p for p in params if p not in ("self", "cls")]
    return len(params)


def _stable_hash(text: str) -> str:
    """Deterministic hash for duplicate detection (avoids Python hash randomization)."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Criterion 1: Error Handling
# ---------------------------------------------------------------------------


def _c1_error_handling(root: str, files: list[str], lang: str) -> tuple[int, list[str]]:
    score = 1
    evidence = []
    bare_count = broad_no_reraise = empty_catch = except_pass = large_try = 0
    mutable_defaults = custom_errors = 0
    has_retry = has_graceful = False
    pat = PATTERNS_ERROR

    for f in files:
        if _is_test_file(f, root):
            continue
        content = _read(f)

        if lang == "python":
            bare_count += len(pat["python"]["bare_except"].findall(content))
            broad_hits = pat["python"]["broad_except"].findall(content)
            broad_count = len(broad_hits)
            # broad except without re-raise
            blocks = re.findall(
                r"except\s+Exception[^:]*:(.*?)(?=\n\s*(?:except|def|class)\b|\Z)",
                content,
                re.DOTALL,
            )
            broad_no_reraise += sum(1 for b in blocks if "raise" not in b)
            except_pass += len(pat["python"]["except_pass"].findall(content))
            mutable_defaults += len(pat["python"]["mutable_default"].findall(content))
            custom_errors += len(pat["python"]["custom_error"].findall(content))

        else:  # js
            empty_catch += len(pat["js"]["empty_catch"].findall(content))
            custom_errors += len(pat["js"]["custom_error"].findall(content))

        # Large try blocks (>15 non-empty lines)
        try_blocks = re.findall(
            r"try\s*:(.*?)(?=\n\s*except\b|\n\s*finally\b)", content, re.DOTALL
        )
        for block in try_blocks:
            if len([l for l in block.split("\n") if l.strip()]) > 15:
                large_try += 1

        if pat["common"]["retry"].search(content):
            has_retry = True
        if pat["common"]["graceful"].search(content):
            has_graceful = True

    total_catch_all = bare_count + broad_no_reraise + empty_catch
    if bare_count > 0:
        evidence.append(f"{bare_count} bare `except:` handlers")
        score = max(score, 4)
    if broad_no_reraise > 0:
        evidence.append(f"{broad_no_reraise} broad except without re-raise")
        score = max(score, 3)
    if empty_catch > 0:
        evidence.append(f"{empty_catch} empty catch blocks")
        score = max(score, 3)
    if total_catch_all > 10:
        score = max(score, 4)
    elif total_catch_all > 5:
        score = max(score, 3)
    if except_pass > 0:
        evidence.append(f"{except_pass} except:pass (errors silently swallowed)")
        score = max(score, 3)
    if large_try > 0:
        evidence.append(f"{large_try} try block(s) with 15+ lines")
        score = max(score, 2)
    if mutable_defaults > 0:
        evidence.append(f"{mutable_defaults} mutable default args")
        score = max(score, 2)
    if custom_errors == 0 and len(files) > 5:
        evidence.append("No custom error/exception types defined")
        score = max(score, 3)
    if not has_retry and len(files) > 10:
        evidence.append("No retry/backoff logic detected")
        score = max(score, 2)
    if not has_graceful and len(files) > 10:
        evidence.append("No graceful degradation/fallback patterns")
    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 2: Logging
# ---------------------------------------------------------------------------


def _c2_logging(root: str, files: list[str], lang: str) -> tuple[int, list[str]]:
    score = 1
    evidence = []
    bare_files = structured_files = no_log_files = debugger_files = 0
    has_context = False
    non_test_count = 0
    pat = PATTERNS_LOG

    for f in files:
        if _is_test_file(f, root):
            continue
        content = _read(f)
        non_test_count += 1

        if lang == "python":
            has_bare = bool(pat["python"]["bare"].search(content))
            has_structured = bool(pat["python"]["structured"].search(content))
            if pat["python"]["context"].search(content):
                has_context = True
        else:
            has_bare = bool(pat["js"]["bare"].search(content))
            has_structured = bool(pat["js"]["structured"].search(content))
            if pat["js"]["debugger"].search(content):
                debugger_files += 1
            if pat["js"]["context"].search(content):
                has_context = True

        if has_bare:
            bare_files += 1
        if has_structured:
            structured_files += 1

        has_any_log = has_bare or has_structured
        if not has_any_log and content.count("\n") > 50:
            no_log_files += 1

    if non_test_count == 0:
        return 1, ["No non-test files found"]

    bare_ratio = bare_files / non_test_count
    if bare_ratio > 0.5 and non_test_count > 3:
        evidence.append(
            f"{bare_files}/{non_test_count} files use bare print/console.log"
        )
        score = max(score, 4)
    elif bare_ratio > 0.3:
        evidence.append(
            f"{bare_files}/{non_test_count} files use bare print/console.log"
        )
        score = max(score, 3)
    elif bare_files > 0:
        evidence.append(f"{bare_files} files use bare print/console.log")
        score = max(score, 2)

    if structured_files == 0 and non_test_count > 5:
        evidence.append("No structured logging library detected")
        score = max(score, 4)

    if not has_context and non_test_count > 5:
        evidence.append("No request/correlation ID logging context found")
        score = max(score, 3)

    if no_log_files > non_test_count * 0.5 and non_test_count > 5:
        evidence.append(
            f"{no_log_files}/{non_test_count} non-trivial files have no logging"
        )
        score = max(score, 3)

    if debugger_files > 0:
        evidence.append(f"{debugger_files} files contain `debugger` statements")
        score = max(score, 3)

    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 3: Configuration
# ---------------------------------------------------------------------------


def _c3_configuration(root: str, files: list[str], lang: str) -> tuple[int, list[str]]:
    score = 1
    evidence = []
    magic_count = hardcoded_urls = ip_count = port_count = env_usage = 0
    has_dotenv = False
    pat = PATTERNS_CONFIG

    for f in files:
        if _is_test_file(f, root):
            continue
        content = _read(f)

        magic_count += len(pat["common"]["magic_num"].findall(content))
        hardcoded_urls += len(pat["common"]["hardcoded_url"].findall(content))

        if lang == "python":
            if pat["python"]["env_var"].search(content):
                env_usage += 1
            if re.search(r"(?:dotenv|from_env|config\(\))", content, re.I):
                has_dotenv = True
        else:
            if pat["js"]["env_var"].search(content):
                env_usage += 1
            if re.search(r"(?:dotenv|next\.config|env\.local)", content, re.I):
                has_dotenv = True

        # IP and port detection (skip comments and string values)
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith(("#", "//", "/*", "*")):
                continue
            no_str = re.sub(r"([\"'])(?:(?!\1).)*?\1", '""', stripped)
            if pat["common"]["ip_addr"].search(no_str):
                ip_count += 1
            if re.search(r"(?:port|PORT)\s*[=:]\s*\d{2,5}", no_str):
                if not re.match(r"^[A-Z_][A-Z0-9_]*\s*=", stripped):
                    port_count += 1

    if magic_count > 20:
        evidence.append(
            f"{magic_count} hardcoded numeric config values (magic numbers)"
        )
        score = max(score, 4)
    elif magic_count > 10:
        evidence.append(f"{magic_count} hardcoded config values")
        score = max(score, 3)
    elif magic_count > 0:
        evidence.append(f"{magic_count} hardcoded config values")

    if hardcoded_urls > 5:
        evidence.append(f"{hardcoded_urls} hardcoded URLs in source")
        score = max(score, 3)

    if ip_count > 0:
        evidence.append(f"{ip_count} hardcoded IP address(es)")
        score = max(score, 3)
    if port_count > 0:
        evidence.append(f"{port_count} hardcoded port number(s)")
        score = max(score, 2)

    if env_usage == 0 and len(files) > 5:
        evidence.append("No env var usage detected")
        score = max(score, 4)

    has_env_example = any(
        os.path.exists(os.path.join(root, n))
        for n in (".env.example", ".env.sample", ".env.template")
    )
    if not has_env_example and not has_dotenv and len(files) > 5:
        evidence.append("No .env.example or dotenv setup found")
        score = max(score, 2)

    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 4: Database Practices
# ---------------------------------------------------------------------------


def _c4_database(root: str, files: list[str], lang: str) -> tuple[int, list[str]]:
    score = 1
    evidence = []
    raw_queries = n_plus_one = 0
    has_orm = has_pool = has_raw_connect = False
    no_pool_files: list[str] = []
    pat = PATTERNS_DB

    # Migration detection
    migration_markers = {
        "migrations",
        "alembic",
        "flyway",
        "liquibase",
        "db/migrate",
        "prisma/migrations",
    }
    all_dirs: set[str] = set()
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for d in dirnames:
            all_dirs.add(d)
    has_migrations = bool(migration_markers & all_dirs)
    for cfg in (
        "alembic.ini",
        "schema.prisma",
        "knexfile.js",
        "knexfile.ts",
        "drizzle.config.ts",
        "drizzle.config.js",
    ):
        if os.path.exists(os.path.join(root, cfg)):
            has_migrations = True

    for f in files:
        if _is_test_file(f, root):
            continue
        content = _read(f)
        lines = content.split("\n")
        rel = _rel(f, root)

        lang_pat = pat[lang]
        raw_queries += len(lang_pat["raw_query"].findall(content))
        if lang_pat["orm"].search(content):
            has_orm = True
        if lang_pat["pool"].search(content):
            has_pool = True
        if lang_pat["raw_connect"].search(content):
            has_raw_connect = True
            if not lang_pat["pool"].search(content):
                no_pool_files.append(rel)

        # N+1 detection — indent-aware for Python, brace-aware for JS
        if lang == "python":
            in_loop = False
            base_indent = 0
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip())
                if re.match(r"(for |while )", stripped):
                    in_loop = True
                    base_indent = indent
                elif in_loop and indent <= base_indent and stripped:
                    in_loop = False
                if in_loop and indent > base_indent:
                    if pat["common"]["query_in_loop"].search(stripped):
                        n_plus_one += 1
        else:
            brace_depth = loop_depth = 0
            for line in lines:
                stripped = line.strip()
                if pat["common"]["loop_start"].search(stripped):
                    loop_depth += 1
                brace_depth += stripped.count("{") - stripped.count("}")
                if loop_depth > 0 and pat["common"]["query_in_loop"].search(stripped):
                    if not stripped.startswith(("//", "*")):
                        n_plus_one += 1
                if brace_depth <= 0:
                    loop_depth = max(loop_depth - 1, 0)

    has_db = has_orm or raw_queries > 0 or has_raw_connect
    if not has_db:
        return 1, ["No database usage detected (N/A)"]

    if raw_queries > 5:
        evidence.append(f"{raw_queries} raw/string-interpolated SQL queries")
        score = max(score, 4)
    elif raw_queries > 0:
        evidence.append(f"{raw_queries} raw queries detected")
        score = max(score, 2)

    if not has_migrations:
        evidence.append("No DB migration framework detected")
        score = max(score, 3)

    if no_pool_files:
        evidence.append(
            f"DB connection without pooling in: {', '.join(no_pool_files[:3])}"
        )
        score = max(score, 3)
    elif not has_pool and has_raw_connect:
        evidence.append("No connection pooling detected")
        score = max(score, 2)

    if n_plus_one > 0:
        evidence.append(f"{n_plus_one} DB queries inside loops (N+1 pattern)")
        score = max(score, 3)

    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 5: API Design
# ---------------------------------------------------------------------------


def _c5_api_design(root: str, files: list[str], lang: str) -> tuple[int, list[str]]:
    score = 1
    evidence = []
    routes: list[str] = []
    has_schema = has_pagination = False
    pat = PATTERNS_API

    for f in files:
        if _is_test_file(f, root):
            continue
        content = _read(f)
        rel = _rel(f, root)
        rel_posix = rel.replace(os.sep, "/")

        if lang == "python":
            for m in pat["python"]["route"].finditer(content):
                routes.append(m.group(1))
            if pat["python"]["schema"].search(content):
                has_schema = True
            if pat["python"]["pagination"].search(content):
                has_pagination = True
        else:
            for m in pat["js"]["route"].finditer(content):
                routes.append(m.group(1))
            # Next.js file-based routes
            if pat["js"]["nextjs_page"].search(rel_posix):
                rp = "/" + re.sub(r"\.[jt]sx?$", "", rel_posix).replace("pages/", "")
                routes.append(re.sub(r"/index$", "", rp) or "/")
            elif pat["js"]["nextjs_app"].search(rel_posix):
                rp = re.sub(r"/route\.[jt]sx?$", "", rel_posix)
                rp = re.sub(r"^(?:src/)?app", "", rp) or "/"
                routes.append(rp)
            if pat["js"]["schema"].search(content):
                has_schema = True
            if pat["js"]["pagination"].search(content):
                has_pagination = True

    if not routes:
        return 1, ["No API routes detected (N/A)"]

    camel = sum(1 for r in routes if re.search(r"[a-z][A-Z]", r))
    snake = sum(1 for r in routes if "_" in r)
    if camel > 0 and snake > 0:
        evidence.append(f"Mixed naming: {camel} camelCase + {snake} snake_case routes")
        score = max(score, 3)

    if not has_schema:
        evidence.append("No request/response validation schema library detected")
        score = max(score, 3)

    if not has_pagination and len(routes) > 5:
        evidence.append("No pagination parameters detected")
        score = max(score, 2)

    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 6: Resource Management
# ---------------------------------------------------------------------------


def _c6_resource_management(
    root: str, files: list[str], lang: str
) -> tuple[int, list[str]]:
    score = 1
    evidence = []
    no_cleanup = http_no_timeout = bare_open = 0
    has_cleanup = has_timeout = False
    pat = PATTERNS_RESOURCE

    for f in files:
        if _is_test_file(f, root):
            continue
        content = _read(f)
        lines = content.split("\n")

        has_resource = bool(
            re.search(
                r"(?:open\s*\(|connect\s*\(|createConnection|createClient|createPool)",
                content,
                re.I,
            )
        )

        if lang == "python":
            if pat["python"]["cleanup"].search(content):
                has_cleanup = True
            if pat["python"]["timeout"].search(content):
                has_timeout = True
            if has_resource and not pat["python"]["cleanup"].search(content):
                no_cleanup += 1

            for i, line in enumerate(lines):
                # HTTP without timeout
                if pat["python"]["http_call"].search(line):
                    block = "\n".join(lines[i : i + 4])
                    if "timeout" not in block:
                        http_no_timeout += 1
                # bare open() not preceded by 'with'
                stripped = line.strip()
                if (
                    re.search(r"\bopen\s*\(", stripped)
                    and not stripped.startswith("with ")
                    and not stripped.startswith("#")
                    and not re.search(
                        r"(?:webbrowser|os|subprocess)\.\s*open", stripped
                    )
                ):
                    bare_open += 1

        else:  # js
            if pat["js"]["cleanup"].search(content):
                has_cleanup = True
            if pat["js"]["timeout"].search(content):
                has_timeout = True
            if has_resource and not pat["js"]["cleanup"].search(content):
                no_cleanup += 1

            for i, line in enumerate(lines):
                if pat["js"]["http_call"].search(line):
                    block = "\n".join(lines[i : i + 6])
                    if (
                        "timeout" not in block
                        and "AbortController" not in block
                        and "signal" not in block
                    ):
                        http_no_timeout += 1

    if no_cleanup > 3:
        evidence.append(f"{no_cleanup} files open resources without visible cleanup")
        score = max(score, 3)
    elif no_cleanup > 0:
        evidence.append(f"{no_cleanup} files open resources without cleanup")
        score = max(score, 2)

    if http_no_timeout > 0:
        evidence.append(f"{http_no_timeout} HTTP call(s) without timeout")
        score = max(score, 3)

    if bare_open > 0:
        evidence.append(f"{bare_open} `open()` without `with` statement")
        score = max(score, 2)

    if not has_timeout and len(files) > 10:
        evidence.append("No timeout/cancellation patterns detected")
        score = max(score, 2)

    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 7: Architecture
# ---------------------------------------------------------------------------


def _c7_architecture(root: str, files: list[str], lang: str) -> tuple[int, list[str]]:
    score = 1
    evidence = []
    god_files: list[tuple[str, int]] = []
    long_funcs: list[tuple[str, str, int]] = []
    high_func_files: list[tuple[str, int]] = []
    high_import_files: list[tuple[str, int]] = []
    deep_nesting_files: list[tuple[str, int]] = []

    # Directory structure check
    top_dirs: set[str] = set()
    for f in files:
        parts = _rel(f, root).split(os.sep)
        if len(parts) > 1:
            top_dirs.add(parts[0])
    common_layers = {
        "controllers",
        "services",
        "models",
        "routes",
        "middleware",
        "utils",
        "lib",
        "helpers",
        "config",
        "core",
        "api",
        "views",
        "components",
        "pages",
        "app",
        "src",
    }
    found_layers = top_dirs & common_layers
    if len(found_layers) < 3 and len(files) > 10:
        flat = sum(1 for f in files if os.sep not in _rel(f, root))
        if flat > len(files) * 0.5:
            evidence.append("Flat project structure — most files in root")
            score = max(score, 3)

    for f in files:
        if _is_test_file(f, root):
            continue
        content = _read(f)
        lines = content.split("\n")
        line_count = len(lines)
        rel = _rel(f, root)
        ext = os.path.splitext(f)[1]

        if line_count > 500:
            god_files.append((rel, line_count))

        # Function extraction per language
        func_spans: list[tuple[int, str]] = []
        if lang == "python":
            for i, line in enumerate(lines):
                m = re.match(r"\s*(?:async\s+)?def\s+(\w+)", line)
                if m:
                    func_spans.append((i, m.group(1)))
        else:
            for i, line in enumerate(lines):
                m = re.search(
                    r"(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:function|\())",
                    line,
                )
                if m:
                    name = m.group(1) or m.group(2) or "anon"
                    func_spans.append((i, name))

        for idx, (start, name) in enumerate(func_spans):
            end = func_spans[idx + 1][0] if idx + 1 < len(func_spans) else line_count
            if end - start > 50:
                long_funcs.append((rel, name, end - start))

        if len(func_spans) > 20:
            high_func_files.append((rel, len(func_spans)))

        # Import coupling
        if lang == "python":
            import_count = len(
                re.findall(r"^(?:import |from \S+ import )", content, re.M)
            )
        else:
            import_count = len(re.findall(r"^import\s+", content, re.M))
        if import_count > 15:
            high_import_files.append((rel, import_count))

        # Deep nesting (indent-based for both languages)
        max_depth = 0
        for line in lines:
            exp = line.expandtabs(4)
            stripped = exp.lstrip()
            if not stripped:
                continue
            depth = (len(exp) - len(stripped)) // 4
            max_depth = max(max_depth, depth)
        if max_depth >= 6:
            deep_nesting_files.append((rel, max_depth))

    if god_files:
        god_files.sort(key=lambda x: -x[1])
        ex = ", ".join(f"{n} ({l}L)" for n, l in god_files[:3])
        evidence.append(f"{len(god_files)} files exceed 500 lines: {ex}")
        score = max(score, 4 if len(god_files) > 5 else 2)

    if long_funcs:
        long_funcs.sort(key=lambda x: -x[2])
        ex = ", ".join(f"`{n}()` {l}L" for _, n, l in long_funcs[:3])
        evidence.append(f"{len(long_funcs)} functions exceed 50 lines: {ex}")
        score = max(score, 3)

    if high_func_files:
        ex = ", ".join(
            f"{p} ({c})" for p, c in sorted(high_func_files, key=lambda x: -x[1])[:3]
        )
        evidence.append(f"{len(high_func_files)} files with 20+ functions: {ex}")
        score = max(score, 3)

    if high_import_files:
        ex = ", ".join(
            f"{p} ({c})" for p, c in sorted(high_import_files, key=lambda x: -x[1])[:3]
        )
        evidence.append(
            f"{len(high_import_files)} files with 15+ imports (high coupling): {ex}"
        )
        score = max(score, 2)

    if deep_nesting_files:
        ex = ", ".join(
            f"{p} (depth {d})"
            for p, d in sorted(deep_nesting_files, key=lambda x: -x[1])[:3]
        )
        evidence.append(
            f"{len(deep_nesting_files)} files with deep nesting (≥6 levels): {ex}"
        )
        score = max(score, 3)

    # Circular import detection — Python only, full module path matching
    if lang == "python":
        py_files = [f for f in files if not _is_test_file(f, root)]
        import_graph: dict[str, set[str]] = {}
        for f in py_files:
            content = _read(f)
            mod = _rel(f, root).replace(os.sep, ".").removesuffix(".py")
            imports: set[str] = set()
            for m in re.finditer(
                r"^(?:from\s+(\S+)\s+import|import\s+(\S+))", content, re.M
            ):
                raw = m.group(1) or m.group(2)
                imports.add(raw)  # full import path kept
            import_graph[mod] = imports

        circular: list[tuple[str, str]] = []
        checked: set[tuple[str, str]] = set()
        for mod_a, imp_a in import_graph.items():
            for mod_b, imp_b in import_graph.items():
                if mod_a == mod_b:
                    continue
                pair = tuple(sorted([mod_a, mod_b]))
                if pair in checked:
                    continue
                checked.add(pair)

                # Check if mod_b (or any prefix) appears in mod_a's imports and vice versa
                def _imported(target: str, imports: set[str]) -> bool:
                    return any(
                        imp == target
                        or target.startswith(imp + ".")
                        or imp.startswith(target + ".")
                        for imp in imports
                    )

                if _imported(mod_b, imp_a) and _imported(mod_a, imp_b):
                    circular.append(pair)

        if circular:
            ex = ", ".join(f"{a} <-> {b}" for a, b in circular[:3])
            evidence.append(f"{len(circular)} potential circular import(s): {ex}")
            score = max(score, 3)

    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 8: Testing
# ---------------------------------------------------------------------------


def _c8_testing(
    root: str, files: list[str], all_files: list[str], lang: str
) -> tuple[int, list[str]]:
    score = 1
    evidence = []

    test_files = [
        f
        for f in all_files
        if _is_test_file(f, root) and os.path.splitext(f)[1] in SOURCE_EXTS
    ]
    source_files = [f for f in files if not _is_test_file(f, root)]

    if not test_files:
        if len(source_files) > 5:
            evidence.append("No test files found")
            score = 5
        return score, evidence or ["No tests (small project)"]

    ratio = len(test_files) / max(len(source_files), 1)

    if ratio < 0.1 and len(source_files) > 10:
        evidence.append(
            f"{len(test_files)} test files / {len(source_files)} source files (ratio: {ratio:.2f})"
        )
        score = max(score, 4)
    elif ratio < 0.3:
        evidence.append(
            f"{len(test_files)} test files / {len(source_files)} source files (ratio: {ratio:.2f})"
        )
        score = max(score, 3)
    elif ratio < 0.5:
        evidence.append(
            f"{len(test_files)} test files / {len(source_files)} source files (ratio: {ratio:.2f})"
        )
        score = max(score, 2)

    failure_re = re.compile(
        r"(?:throw|reject|error|fail|invalid|unauthorized|forbidden|404|500)", re.I
    )
    integration_re = re.compile(
        r"(?:integration|e2e|end.to.end|supertest|request\(app\)|TestClient|api\.test)",
        re.I,
    )
    has_failure = has_integration = False

    for f in test_files[:30]:
        content = _read(f)
        if failure_re.search(content):
            has_failure = True
        if integration_re.search(content):
            has_integration = True

    if not has_failure:
        evidence.append("No failure-path tests detected")
        score = max(score, 3)

    if not has_integration:
        evidence.append("No integration/e2e tests detected")
        score = max(score, 2)

    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 9: CI/CD
# ---------------------------------------------------------------------------


def _c9_cicd(root: str) -> tuple[int, list[str]]:
    score = 1
    evidence = []

    ci_paths = [
        ".github/workflows",
        ".gitlab-ci.yml",
        "Jenkinsfile",
        ".circleci/config.yml",
        ".travis.yml",
        "azure-pipelines.yml",
        "bitbucket-pipelines.yml",
    ]
    found_ci = [p for p in ci_paths if os.path.exists(os.path.join(root, p))]

    if not found_ci:
        evidence.append("No CI/CD configuration found")
        score = max(score, 4)
    else:
        wf_dir = os.path.join(root, ".github", "workflows")
        if os.path.isdir(wf_dir):
            combined = "".join(
                _read(os.path.join(wf_dir, y))
                for y in os.listdir(wf_dir)
                if y.endswith((".yml", ".yaml"))
            )
            if not re.search(
                r"(?:npm test|pytest|jest|vitest|cargo test|go test|mvn test)",
                combined,
                re.I,
            ):
                evidence.append("CI does not run tests")
                score = max(score, 3)
            if not re.search(
                r"(?:eslint|flake8|pylint|ruff|prettier|black|mypy)", combined, re.I
            ):
                evidence.append("CI does not run linting")
                score = max(score, 2)

    entrypoints = [
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "Procfile",
        "app.yaml",
    ]
    found_entry = [e for e in entrypoints if os.path.exists(os.path.join(root, e))]
    if not found_entry and len(_find_files(root, SOURCE_EXTS)) > 10:
        evidence.append("No deployment entrypoint (Dockerfile/Procfile/etc.)")
        score = max(score, 2)

    # Build artifacts committed
    build_dirs = {"dist", "build", "out", ".next"}
    gitignore = _read(os.path.join(root, ".gitignore"))
    ignored = {
        l.strip().rstrip("/")
        for l in gitignore.splitlines()
        if l.strip() and not l.strip().startswith("#")
    }
    not_ignored = [
        d
        for d in build_dirs
        if os.path.isdir(os.path.join(root, d))
        and d not in ignored
        and f"/{d}" not in ignored
        and f"{d}/" not in ignored
    ]
    if not_ignored:
        evidence.append(f"Build artifacts may be committed: {', '.join(not_ignored)}")
        score = max(score, 3)

    # Sensitive env files
    env_files = [
        e
        for e in os.listdir(root)
        if (
            e == ".env"
            or (
                e.startswith(".env.")
                and not e.endswith((".example", ".template", ".sample"))
            )
        )
    ]
    if env_files:
        evidence.append(f"Sensitive env files committed: {', '.join(env_files)}")
        score = max(score, 4)

    return score, evidence


# ---------------------------------------------------------------------------
# Criterion 10: Tech Debt
# ---------------------------------------------------------------------------


def _c10_tech_debt(root: str, files: list[str], lang: str) -> tuple[int, list[str]]:
    score = 1
    evidence = []
    todo_count = untracked_todos = fixme_count = hack_count = 0
    long_param_funcs = loose_eq = any_type = 0
    dup_hashes: dict[str, list[str]] = defaultdict(list)
    pat = PATTERNS_DEBT

    for f in files:
        if _is_test_file(f, root):
            continue
        content = _read(f)
        ext = os.path.splitext(f)[1]
        rel = _rel(f, root)

        todos = pat["common"]["todo"].findall(content)
        todo_count += len(todos)
        for t in todos:
            if not pat["common"]["ticket"].search(t):
                untracked_todos += 1
        fixme_count += len(pat["common"]["fixme"].findall(content))
        hack_count += len(pat["common"]["hack"].findall(content))

        if lang == "python":
            for m in pat["python"]["long_params"].finditer(content):
                if _count_params(m.group(1), is_python=True) >= 6:
                    long_param_funcs += 1
        else:
            for m in pat["js"]["long_params"].finditer(content):
                params_str = m.group(1) or m.group(2) or ""
                if _count_params(params_str) >= 6:
                    long_param_funcs += 1
            loose = pat["js"]["loose_eq"].findall(content)
            if len(loose) > 3:
                loose_eq += len(loose)

        if ext in {".ts", ".tsx"}:
            hits = pat["ts"]["any_type"].findall(content)
            if len(hits) > 3:
                any_type += len(hits)

        # Duplicate block detection (stable hash, 10-line window)
        lines = content.split("\n")
        window = 10
        for i in range(len(lines) - window + 1):
            block_lines = [l.strip() for l in lines[i : i + window]]
            non_empty = [l for l in block_lines if l]
            if len(non_empty) < 7:
                continue
            boilerplate = sum(
                1
                for l in block_lines
                if (
                    not l
                    or l.startswith(("import ", "from ", "//", "#", "/*", "*", "@"))
                    or re.match(r"^[)\]}{;,]*$", l)
                )
            )
            if boilerplate > window * 0.5:
                continue
            h = _stable_hash("\n".join(block_lines))
            dup_hashes[h].append(rel)

    dup_groups = sum(1 for locs in dup_hashes.values() if len(set(locs)) >= 3)

    if todo_count > 20:
        evidence.append(f"{todo_count} TODO/FIXME/HACK comments")
        score = max(score, 4)
    elif todo_count > 10:
        evidence.append(f"{todo_count} TODO/FIXME/HACK comments")
        score = max(score, 3)
    elif todo_count > 0:
        evidence.append(f"{todo_count} TODO/FIXME comments")

    if untracked_todos > 0 and todo_count > 0:
        pct = int(untracked_todos / todo_count * 100)
        evidence.append(
            f"{untracked_todos}/{todo_count} TODOs ({pct}%) lack a ticket reference"
        )
        if pct > 80:
            score = max(score, 3)

    if fixme_count > 0:
        evidence.append(f"{fixme_count} FIXME comments (unresolved bugs)")
        score = max(score, 2)
    if hack_count > 0:
        evidence.append(f"{hack_count} HACK comments (acknowledged debt)")
        score = max(score, 2)
    if long_param_funcs > 5:
        evidence.append(f"{long_param_funcs} functions with 6+ parameters")
        score = max(score, 3)
    elif long_param_funcs > 0:
        evidence.append(f"{long_param_funcs} functions with 6+ parameters")
        score = max(score, 2)
    if loose_eq > 0:
        evidence.append(f"{loose_eq} uses of `==` instead of `===` in JS/TS")
        score = max(score, 2)
    if any_type > 0:
        evidence.append(f"{any_type} uses of `any` type in TypeScript")
        score = max(score, 2)
    if dup_groups > 10:
        evidence.append(f"{dup_groups} duplicated code blocks across files")
        score = max(score, 3)
    elif dup_groups > 5:
        evidence.append(f"{dup_groups} copy-pasted code blocks")
        score = max(score, 2)

    if score == 1 and not evidence:
        evidence.append("Tech debt appears manageable")
    return score, evidence


# ---------------------------------------------------------------------------
# Smart code sampling for LLM
# ---------------------------------------------------------------------------


def _smart_sample(
    root: str, files: list[str], criteria_results: dict, token_budget: int = 3000
) -> str:
    """
    Pick the most representative non-test files for LLM review.
    Strategy:
      1. One file per top-level directory (breadth)
      2. Largest files by line count (depth)
      3. Files with the most static findings (relevance)
    Stops when estimated token budget is reached (~4 chars/token).
    """
    non_test = [f for f in files if not _is_test_file(f, root)]
    if not non_test:
        return ""

    char_budget = token_budget * 4
    scored: dict[str, int] = defaultdict(int)

    # Score by directory coverage
    seen_dirs: set[str] = set()
    for f in non_test:
        parts = _rel(f, root).split(os.sep)
        top = parts[0] if len(parts) > 1 else "__root__"
        if top not in seen_dirs:
            scored[f] += 10
            seen_dirs.add(top)

    # Score by size
    sizes = {f: len(_read(f)) for f in non_test}
    for f in sorted(sizes, key=lambda x: -sizes[x])[:5]:
        scored[f] += 5

    # Score by evidence count
    all_evidence = " ".join(
        e for cd in criteria_results.values() for e in cd["evidence"]
    )
    for f in non_test:
        rel = _rel(f, root)
        if rel.lower() in all_evidence.lower():
            scored[f] += 8

    ordered = sorted(non_test, key=lambda f: -scored[f])

    snippets: list[str] = []
    total_chars = 0
    for f in ordered:
        content = _read(f)
        if not content.strip():
            continue
        lines = content.split("\n")
        # Take up to 80 lines per file
        sample = "\n".join(f"{i + 1}: {l}" for i, l in enumerate(lines[:80]))
        chunk = f"\n--- {_rel(f, root)} ---\n{sample}\n"
        if total_chars + len(chunk) > char_budget:
            break
        snippets.append(chunk)
        total_chars += len(chunk)

    return "".join(snippets)


# ---------------------------------------------------------------------------
# LLM analysis — single call per repo, all 10 criteria
# ---------------------------------------------------------------------------

_LLM_SYSTEM = (
    "You are a senior software engineer conducting a production readiness review. "
    "You receive code samples from a repository and automated static analysis findings. "
    "Your job is to validate the scores, find gaps the static analysis missed, "
    "and return a structured JSON assessment. Be practical and specific. "
    "Return ONLY valid JSON — no markdown, no preamble."
)

_LLM_CRITERIA_DESC = {
    "error_handling": "bare/broad exception handling, missing retry/fallback, swallowed errors, missing custom error types",
    "logging": "bare print/console.log, missing structured logging, no correlation IDs, debugger statements",
    "configuration": "hardcoded URLs/IPs/ports/secrets, magic numbers, missing env var usage",
    "database": "raw SQL interpolation, N+1 patterns, missing connection pooling, no migrations",
    "api_design": "inconsistent naming, missing validation schemas, no pagination, inconsistent error formats",
    "resource_management": "unclosed connections/files, HTTP calls without timeout, memory leaks",
    "architecture": "god files, circular deps, deep nesting, mixed concerns, oversized functions",
    "testing": "missing failure-path tests, no integration tests, non-deterministic tests",
    "cicd": "CI skips tests/lint, missing Dockerfile/Procfile, committed .env or build artifacts",
    "tech_debt": "stale TODOs without tickets, copy-paste blocks, 6+ param functions, any types",
}


def _llm_analysis(
    code_samples: str, criteria_results: dict, client, model: str, lang: str
) -> dict:
    """Single LLM call per repo. Returns refined scores and new findings per criterion."""

    automated_summary = "\n".join(
        f"  {k}: score={v['score']}/5, findings={'; '.join(v['evidence'][:3])}"
        for k, v in criteria_results.items()
    )

    criteria_list = "\n".join(
        f'  "{k}": "{desc}"' for k, desc in _LLM_CRITERIA_DESC.items()
    )

    prompt = f"""Repository language: {lang}

Automated static analysis results (scores 1=excellent, 5=poor):
{automated_summary}

Criteria definitions:
{criteria_list}

Code samples:
{code_samples}

Return a JSON object with exactly these keys (one per criterion):
{{
  "error_handling":      {{"refined_score": 1-5, "new_findings": ["...", ...], "summary": "..."}},
  "logging":             {{"refined_score": 1-5, "new_findings": ["...", ...], "summary": "..."}},
  "configuration":       {{"refined_score": 1-5, "new_findings": ["...", ...], "summary": "..."}},
  "database":            {{"refined_score": 1-5, "new_findings": ["...", ...], "summary": "..."}},
  "api_design":          {{"refined_score": 1-5, "new_findings": ["...", ...], "summary": "..."}},
  "resource_management": {{"refined_score": 1-5, "new_findings": ["...", ...], "summary": "..."}},
  "architecture":        {{"refined_score": 1-5, "new_findings": ["...", ...], "summary": "..."}},
  "testing":             {{"refined_score": 1-5, "new_findings": ["...", ...], "summary": "..."}},
  "cicd":                {{"refined_score": 1-5, "new_findings": ["...", ...], "summary": "..."}},
  "tech_debt":           {{"refined_score": 1-5, "new_findings": ["...", ...], "summary": "..."}}
}}

Rules:
- refined_score: your best assessment integrating both static findings and code review
- new_findings: issues the static analysis MISSED (empty list if none); only populate when refined_score exceeds the automated score; each entry must include specific file paths and line numbers
- summary: one sentence
- Be strict — production code should score 1 or 2 only if genuinely solid
"""

    from llm_client import call_llm
    try:
        raw = call_llm(
            [
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            model=model,
            client=client,
            temperature=0,
        )
        raw = raw.strip()
        # Strip any accidental markdown fences
        raw = re.sub(r"```(?:json)?", "", raw).strip()
        start, end = raw.find("{"), raw.rfind("}") + 1
        if start >= 0 and end > start:
            raw = raw[start:end]
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_error": "JSON parse failed after 3 attempts"}
    except Exception as exc:
        return {"_error": str(exc)}


# ---------------------------------------------------------------------------
# Verdict helpers
# ---------------------------------------------------------------------------


def _get_grade(total: int) -> str:
    if total <= 10:
        return "CLEAN"
    elif total <= 18:
        return "MINOR"
    elif total <= 28:
        return "MODERATE"
    return "CRITICAL"


_CRITERIA_CATEGORY: dict[str, tuple[str, str]] = {
    "error_handling": ("Error Handling", "critical"),
    "logging": ("Logging", "signal"),
    "configuration": ("Config", "critical"),
    "database": ("DB Patterns", "critical"),
    "api_design": ("API Design", "signal"),
    "resource_management": ("Resource Leaks", "critical"),
    "architecture": ("Architecture", "signal"),
    "testing": ("Test Coverage", "signal"),
    "cicd": ("CI/CD", "critical"),
    "tech_debt": ("Tech Debt", "signal"),
}


def _build_final_details_split(result: dict) -> tuple[list[str], list[str]]:
    critical: list[str] = []
    signals: list[str] = []

    for crit_name, crit_data in result.get("criteria", {}).items():
        cat_info = _CRITERIA_CATEGORY.get(crit_name)
        if not cat_info:
            continue
        label, kind = cat_info
        bucket = critical if kind == "critical" else signals

        for e in crit_data.get("evidence", []):
            bucket.append(e)

    return critical, signals


# ---------------------------------------------------------------------------
# Per-repo orchestrator
# ---------------------------------------------------------------------------

CRITERIA_KEYS = [
    "error_handling",
    "logging",
    "configuration",
    "database",
    "api_design",
    "resource_management",
    "architecture",
    "testing",
    "cicd",
    "tech_debt",
]


def _check_repo(
    owner: str,
    repo: str,
    token: str,
    clone_base: str,
    verbose_log=None,
    client=None,
    model: str = DEFAULT_MODEL,
    skip_llm: bool = False,
    existing_repo_path: str | None = None,
) -> dict:

    result = {
        "repo": repo,
        "language": "unknown",
        "total_score": 0,
        "grade": "?",
        "criteria": {},
        "files_analyzed": 0,
        "files_excluded_turing": 0,
        "error": None,
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

        ok, err_msg = _clone_repo(owner, repo, clone_dir, token)
        if not ok:
            result["error"] = f"clone failed: {err_msg}"
            return result

        root = clone_dir

    all_source = _find_files(root, SOURCE_EXTS)

    if not all_source:
        result["error"] = "no source files"
        if owns_clone:
            shutil.rmtree(clone_dir, ignore_errors=True)
        return result

    lang = _detect_language(all_source)
    result["language"] = lang

    if verbose_log:
        verbose_log(
            f"    Detected language: {lang} | {len(all_source)} source files found"
        )

    files_before = len(all_source)
    source_files = _filter_non_turing_files(root, all_source, verbose_log)
    result["files_excluded_turing"] = files_before - len(source_files)
    result["files_analyzed"] = len(source_files)

    if not source_files:
        result["error"] = "all files turing-authored"
        if owns_clone:
            shutil.rmtree(clone_dir, ignore_errors=True)
        return result

    # Run all 10 static checks
    s1, e1 = _c1_error_handling(root, source_files, lang)
    s2, e2 = _c2_logging(root, source_files, lang)
    s3, e3 = _c3_configuration(root, source_files, lang)
    s4, e4 = _c4_database(root, source_files, lang)
    s5, e5 = _c5_api_design(root, source_files, lang)
    s6, e6 = _c6_resource_management(root, source_files, lang)
    s7, e7 = _c7_architecture(root, source_files, lang)
    s8, e8 = _c8_testing(root, source_files, all_source, lang)
    s9, e9 = _c9_cicd(root)
    s10, e10 = _c10_tech_debt(root, source_files, lang)

    result["criteria"] = {
        "error_handling": {"score": s1, "evidence": e1},
        "logging": {"score": s2, "evidence": e2},
        "configuration": {"score": s3, "evidence": e3},
        "database": {"score": s4, "evidence": e4},
        "api_design": {"score": s5, "evidence": e5},
        "resource_management": {"score": s6, "evidence": e6},
        "architecture": {"score": s7, "evidence": e7},
        "testing": {"score": s8, "evidence": e8},
        "cicd": {"score": s9, "evidence": e9},
        "tech_debt": {"score": s10, "evidence": e10},
    }

    # LLM — single call per repo, all 10 criteria
    if not skip_llm and client:
        if verbose_log:
            verbose_log(f"    Running LLM analysis for {repo} (lang={lang}) ...")
        code_samples = _smart_sample(root, source_files, result["criteria"])
        if code_samples:
            llm = _llm_analysis(code_samples, result["criteria"], client, model, lang)
            result["llm_analysis"] = llm
            if "_error" not in llm:
                for crit_name in CRITERIA_KEYS:
                    if crit_name not in llm:
                        continue
                    cd = llm[crit_name]
                    if isinstance(cd, dict):
                        if "refined_score" in cd:
                            result["criteria"][crit_name]["llm_score"] = cd[
                                "refined_score"
                            ]
                            result["criteria"][crit_name]["llm_summary"] = cd.get(
                                "summary", ""
                            )
                        static_score = result["criteria"][crit_name]["score"]
                        if cd.get("refined_score", 0) > static_score:
                            result["criteria"][crit_name]["evidence"] = cd.get(
                                "new_findings", []
                            )[:4]

    # Compute totals using LLM-refined scores where available
    total = 0
    for k in CRITERIA_KEYS:
        crit = result["criteria"][k]
        final_score = crit.get("llm_score", crit["score"])
        crit["final_score"] = final_score
        total += final_score

    result["total_score"] = total
    result["grade"] = _get_grade(total)

    all_details: list[str] = []
    crit_lines, sig_lines = _build_final_details_split(result)
    all_details.extend(crit_lines)
    all_details.extend(sig_lines)
    result["final_details"] = all_details
    result["final_details_critical"] = crit_lines
    result["final_details_signals"] = sig_lines
    result["final_details_count"] = len(all_details)

    if owns_clone:
        shutil.rmtree(clone_dir, ignore_errors=True)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

CRITERIA_SHORT = ["Err", "Log", "Cfg", "DB", "API", "Res", "Arch", "Test", "CI", "Debt"]
