"""
Agent-based quality checks: vibe coding, security, and production quality.

Each check is a pydantic-ai Agent that browses the cloned repo using four tools
(list_directory, read_file_section, grep_codebase, git_log_summary) and returns
a structured CheckOutput with grounded PointFinding and PatternFinding objects.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import random
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator
from pydantic_ai import Agent
from pydantic_ai.tools import RunContext

from eval_kit.llm_client import (
    BASE_DELAY,
    MAX_RETRIES,
    RETRYABLE_ERRORS,
    _track_cost,
    build_model_string,
    validate_api_key,
)
from eval_kit.usage_tracker import CostLimitAborted

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXCLUDE_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".env",
    "dist",
    "build",
    ".build",
    "vendor",
    "third_party",
    "third-party",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "coverage",
    ".coverage",
    "htmlcov",
    "site-packages",
}

TOOLGEN_DIRS = {
    "migrations",
    "generated",
    "gen",
    "_gen",
    "auto_generated",
    "codegen",
    "pb",  # protobuf generated
}

TOOLGEN_FILE_PATTERNS = [
    "*.pb.go",
    "*.pb.ts",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.generated.*",
    "*.min.js",
    "*.min.css",
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    "pnpm-lock.yaml",
    "*.lock",
]

# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def is_toolgen(rel_path: str) -> bool:
    """Return True if the file is auto-generated or vendored."""
    parts = Path(rel_path).parts
    for part in parts[:-1]:  # directory components
        if part in TOOLGEN_DIRS or part in EXCLUDE_DIRS:
            return True
    filename = parts[-1] if parts else ""
    for pat in TOOLGEN_FILE_PATTERNS:
        if fnmatch.fnmatch(filename, pat):
            return True
    return False


def safe_read(abs_path: str, start: int = 1, end: int = 200) -> str:
    """Read lines [start, end] from a file, returning them with line numbers."""
    try:
        path = Path(abs_path)
        lines = path.read_text(errors="replace").splitlines()
        # clamp to actual file length
        start = max(1, start)
        end = min(end, len(lines))
        selected = lines[start - 1 : end]
        return "\n".join(f"{start + i:>4}: {line}" for i, line in enumerate(selected))
    except Exception as exc:
        return f"[Error reading {abs_path}: {exc}]"


def run_git(root: str, args: list[str]) -> str:
    """Run a git command under root and return stdout, or an error string."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout or result.stderr or ""
    except Exception as exc:
        return f"[git error: {exc}]"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PointFinding(BaseModel):
    """A specific defect at a specific location in the codebase."""

    severity: Literal["critical", "signal"]
    file: str  # relative path — must be concrete
    line: int | None  # None for file-level findings
    category: str  # short label: "Hardcoded Secret", "SQL Injection", etc.
    description: str  # the issue as a sentence, starting with the problem

    @field_validator("file")
    @classmethod
    def must_be_real_path(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped or stripped.lower() in (
            "n/a",
            "?",
            "unknown",
            "none",
            "repo",
            "n/a.",
            "na",
        ):
            raise ValueError(f"file must be a concrete path, got {v!r}")
        return stripped

    def dedup_key(self) -> tuple:
        return ("point", self.file, self.line, self.category.lower())

    def to_string(self) -> str:
        loc = f" at line {self.line}" if self.line is not None else ""
        return f"{self.description} in {self.file}{loc}"


class PatternFinding(BaseModel):
    """A recurring pattern across multiple files."""

    severity: Literal["critical", "signal"]
    category: str  # short label: "Narration Comments", "Conventional Commits", etc.
    description: str  # the problem as a sentence, starting with the problem
    total_affected: int  # real total count (files or commits), even if only 10 shown
    example_files: list[str]  # 1–10 representative files or paths

    @field_validator("example_files")
    @classmethod
    def needs_examples(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("pattern finding must have at least one example file")
        return v[:10]  # silently cap at 10

    def dedup_key(self) -> tuple:
        return ("pattern", self.category.lower(), self.severity)

    def to_string(self) -> str:
        files = ", ".join(self.example_files)
        extra = self.total_affected - len(self.example_files)
        suffix = f" and {extra} more" if extra > 0 else ""
        return f"{self.description} affecting {self.total_affected} files — {files}{suffix}"


def dedup(items: list, seen: set) -> list:
    result = []
    for item in items:
        k = item.dedup_key()
        if k not in seen:
            seen.add(k)
            result.append(item)
    return result


class CheckOutput(BaseModel):
    point_findings: list[PointFinding]
    pattern_findings: list[PatternFinding]

    @model_validator(mode="after")
    def deduplicate(self) -> "CheckOutput":
        seen: set[tuple] = set()
        self.point_findings = dedup(self.point_findings, seen)
        self.pattern_findings = dedup(self.pattern_findings, seen)
        return self


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def serialize(output: CheckOutput) -> tuple[list[str], list[str]]:
    critical = [
        f.to_string() for f in output.point_findings if f.severity == "critical"
    ] + [f.to_string() for f in output.pattern_findings if f.severity == "critical"]
    signals = [
        f.to_string() for f in output.point_findings if f.severity == "signal"
    ] + [f.to_string() for f in output.pattern_findings if f.severity == "signal"]
    return critical, signals


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


def list_directory(ctx: RunContext[str], rel_path: str = "") -> str:
    """List files and directories at rel_path (relative to repo root).

    Returns up to 200 entries. Skips node_modules, .git, __pycache__, .venv,
    dist, build, and other non-developer directories automatically.
    """
    root = ctx.deps
    target = Path(root) / rel_path if rel_path else Path(root)
    abs_target = target.resolve()

    # Safety: must be inside root
    if not str(abs_target).startswith(str(Path(root).resolve())):
        return "[Error: path traversal outside repo root]"

    if not abs_target.exists():
        return f"[Path does not exist: {rel_path!r}]"

    if not abs_target.is_dir():
        return f"[Not a directory: {rel_path!r}]"

    entries = []
    try:
        for entry in sorted(abs_target.iterdir()):
            name = entry.name
            if name in EXCLUDE_DIRS:
                continue
            rel = entry.relative_to(root)
            if entry.is_dir():
                entries.append(f"{rel}/")
            else:
                entries.append(str(rel))
            if len(entries) >= 200:
                entries.append("... (truncated at 200 entries)")
                break
    except Exception as exc:
        return f"[Error listing {rel_path!r}: {exc}]"

    return "\n".join(entries) if entries else "(empty directory)"


def read_file_section(
    ctx: RunContext[str],
    rel_path: str,
    start_line: int = 1,
    end_line: int = 200,
) -> str:
    """Read lines [start_line, end_line] of a file, with line numbers.

    Window capped at 300 lines. Skips generated/vendor files.
    rel_path is relative to the repo root.
    """
    root = ctx.deps
    abs_path = str((Path(root) / rel_path).resolve())

    if not abs_path.startswith(str(Path(root).resolve())):
        return "[Error: path traversal outside repo root]"

    if is_toolgen(rel_path):
        return f"[Skipped: {rel_path!r} is a generated/vendor file]"

    # Cap window
    end_line = min(end_line, start_line + 299)

    return safe_read(abs_path, start_line, end_line)


def grep_codebase(
    ctx: RunContext[str],
    pattern: str,
    file_glob: str = "**/*",
    context_lines: int = 2,
) -> str:
    """Regex search across files matching file_glob.

    Returns up to 40 matches with file:line and surrounding context.
    Skips vendor/generated files and binary files automatically.
    pattern is a Python regex.
    """
    root = ctx.deps
    root_path = Path(root).resolve()

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"[Invalid regex pattern: {exc}]"

    # Collect candidate files
    candidates: list[Path] = []
    for p in root_path.glob(file_glob):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root_path))
        # Skip excluded dirs
        parts = Path(rel).parts
        if any(part in EXCLUDE_DIRS for part in parts):
            continue
        if is_toolgen(rel):
            continue
        candidates.append(p)

    matches_found = 0
    results: list[str] = []

    for p in candidates:
        if matches_found >= 40:
            break
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue

        lines = text.splitlines()
        for i, line in enumerate(lines, start=1):
            if regex.search(line):
                rel = str(p.relative_to(root_path))
                # Gather context
                ctx_start = max(0, i - 1 - context_lines)
                ctx_end = min(len(lines), i + context_lines)
                block = []
                for j in range(ctx_start, ctx_end):
                    marker = ">" if j == i - 1 else " "
                    block.append(f"{marker} {rel}:{j + 1}: {lines[j]}")
                results.append("\n".join(block))
                matches_found += 1
                if matches_found >= 40:
                    break

    if not results:
        return f"[No matches found for pattern {pattern!r}]"

    header = f"Found {matches_found} match(es)"
    if matches_found >= 40:
        header += " (truncated at 40)"
    return header + "\n\n" + "\n\n".join(results)


def git_log_summary(ctx: RunContext[str], max_commits: int = 100) -> str:
    """Structured git log: hash|author|date|message with file-change stats.

    Caps at 200 commits. Shows files changed/insertions/deletions per commit.
    """
    root = ctx.deps
    max_commits = min(max_commits, 200)

    log = run_git(
        root,
        [
            "log",
            f"--max-count={max_commits}",
            "--pretty=format:COMMIT %h | %an | %ad | %s",
            "--date=short",
            "--stat",
        ],
    )
    if not log or log.startswith("[git error"):
        return "[No git history available or not a git repository]"

    return log


# ---------------------------------------------------------------------------
# Agent factory + retry runner
# ---------------------------------------------------------------------------

TOOL_LIST = [list_directory, read_file_section, grep_codebase, git_log_summary]


def make_agent(system_prompt: str) -> Agent:
    provider = os.environ.get("LLM_PROVIDER", "openai").lower()
    validate_api_key(provider)
    return Agent(
        build_model_string(provider),
        system_prompt=system_prompt,
        output_type=CheckOutput,
        tools=TOOL_LIST,
        deps_type=str,
        output_retries=3,
    )


def run_agent(agent: Agent, user_prompt: str, root: str) -> CheckOutput:
    provider = os.environ.get("LLM_PROVIDER", "openai").lower()
    model_str = build_model_string(provider)
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            result = agent.run_sync(
                user_prompt,
                deps=root,
                model_settings={"temperature": 0},
            )
            for msg in result.all_messages():
                for part in msg.parts:
                    if hasattr(part, "tool_name") and hasattr(part, "args"):
                        logger.info("Tool call: %s(args=%r)", part.tool_name, part.args)
            _track_cost(result, model_str, provider)
            return result.output
        except CostLimitAborted:
            raise
        except RETRYABLE_ERRORS as e:
            last_err = e
            delay = BASE_DELAY * (2**attempt) + random.uniform(0, 1)
            logger.warning(
                "Agent call failed (attempt %d/%d): %s — retrying in %.1fs",
                attempt + 1,
                MAX_RETRIES,
                type(e).__name__,
                delay,
            )
            time.sleep(delay)
        except Exception:
            raise
    logger.error("Agent call failed after %d retries: %s", MAX_RETRIES, last_err)
    raise last_err  # type: ignore[misc]


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

VIBE_SYSTEM_PROMPT = """\
You are an expert at detecting AI-generated ("vibe-coded") repositories.
Use your tools to investigate freely — you are not limited to any language or framework.

THOROUGHNESS:
Investigate deeply — superficial passes miss real signals.
Make at least 12 tool calls before returning. Use ALL your tools freely.
Start broad (history, structure), then follow whatever catches your eye.
When a pattern looks interesting, quantify it — measure how widespread it is,
don't just note it anecdotally.

DESCRIPTION FORMAT:
Each description must be ONE factual sentence, max 25 words.
State the problem. Do NOT give advice, recommendations, or fixes.
BAD: "SECRET_KEY is hardcoded and should be provided via environment variable or secret manager"
GOOD: "Hardcoded Django SECRET_KEY = 'django-insecure-m86...' in settings"
BAD: "Functions use generic naming that doesn't convey domain meaning, consider renaming"
GOOD: "Generic names (data, result, handle_input) used instead of domain terms"

FINDING TYPES:
- Use PointFinding for: a specific marker at a specific location (planning doc at root,
  explicit AI marker in a comment, docstring longer than its function, hallucinated import).
- Use PatternFinding for: a recurring pattern across multiple files (commit message style,
  structural copy-paste, generic naming spread, narration comment style).
  Set total_affected to the REAL count you measured. List up to 10 example files.

INVESTIGATION APPROACH:
Every repo is different — form your own strategy based on what you discover.
These are thinking prompts, not a script:

- History tells a story: What's the commit cadence — one big dump or organic growth?
  Are messages suspiciously uniform in format or tone?
- Structure reveals intent: Does the project have planning docs (ARCHITECTURE.md,
  TASKS.md, implementation_plan.md) that AI tools typically generate?
- Code has fingerprints: Read source files from different modules. Watch for
  narration comments, generic naming, structural copy-paste across unrelated files.
- Quantify, don't guess: When you notice a pattern, use grep_codebase to count
  how many files show it. Hard numbers beat impressions.

The signal categories below define WHAT to look for — not HOW to investigate.

WHAT AI-GENERATED REPOS ACTUALLY LOOK LIKE (research-backed, 33,580 PR study):

Critical signals (severity="critical"):
- "Conventional Commits Pattern" [PatternFinding]: 60%+ of commits use strict
  feat:/fix:/chore:/docs: format. AI agents use this 49–68% of the time; humans rarely
  adopt it consistently. You MUST count the commits and calculate the percentage.
  Cite .git/history, total_affected = count of conforming commits.
- "Single Dump Evolution" [PatternFinding]: First commit contains 70%+ of total LOC,
  sparse minor commits after. Check git_log_summary stat output and calculate. Cite .git/history.
- "Structurally Identical Functions" [PatternFinding]: 3+ functions across UNRELATED files
  with identical structure — same error handling shape, same return pattern, same parameter
  naming — verbatim copy-paste not just similar style. You must read the files and confirm.
- "Planning Doc at Root" [PointFinding]: ARCHITECTURE.md, implementation_plan.md,
  BACKLOG.md, agents.md, TASKS.md at repo root. Confirm via list_directory. One finding per doc.
- "Explicit AI Marker" [PointFinding]: "generated by ChatGPT/Claude/Copilot/Cursor",
  @generated, "# AI generated" in source. Use grep_codebase. Cite file:line.

Signal indicators (severity="signal"):
- "Docstring Inversion" [PatternFinding]: Docstrings consistently longer than the functions
  they document. Cite files you actually read.
- "Hallucinated Imports" [PatternFinding]: Imports never used in the file body, or imports
  of packages that don't exist. Cite example files.
- "Generic Naming" [PatternFinding]: Variables/functions named data, result, temp,
  processData, handleInput with no domain meaning, spread across many files. Use
  grep_codebase to count. Cite examples.
- "Narration Comments" [PatternFinding]: Comments explaining WHAT ("# Initialize the
  database connection") rather than WHY. Cite files you read.
- "Verbose Trivial Function" [PatternFinding]: Functions using 3× more lines than needed.
  Cite examples with approximate line ranges.
- Any other AI signal you observe with evidence — you are free to report it.

NEVER REPORT:
- Terse commit messages ("fix bug", "update X", "add Y", "wip") — HUMAN signals
- TODOs, FIXMEs, incomplete comments — HUMAN (humans leave rough edges)
- Inconsistent formatting — HUMAN (different authors, different moods)
- Absence of a tool or practice without a code location
- "No CI", "No tests", "No linter" — not AI signals, just missing tools
- Security vulnerabilities (hardcoded secrets, exposed credentials, injection risks,
  missing auth) — those belong in the security check
- Production quality issues (missing timeouts, swallowed exceptions, no retry logic,
  missing logging) — those belong in the production quality check

GROUNDING RULE: Every PointFinding must reference a file you read or confirmed exists.
Every PatternFinding must list at least one example_file you directly observed.
Git history findings use ".git/history". Return empty lists if you find nothing grounded.
"""

SECURITY_SYSTEM_PROMPT = """\
You are a senior application security engineer. Find real, exploitable vulnerabilities —
not best-practice checklist items. You are not limited to any specific language.

THOROUGHNESS:
Investigate deeply — superficial passes miss real vulnerabilities.
Make at least 12 tool calls before returning. Use ALL your tools freely.
A grep match is NEVER a finding by itself — always read the surrounding code to confirm.
Trace data flows: understand where user input enters, how it's processed, where it ends up.

DESCRIPTION FORMAT:
Each description must be ONE factual sentence, max 25 words.
State the problem. Do NOT give advice, recommendations, or fixes.
BAD: "SECRET_KEY is hardcoded and should be provided via environment variable or secret manager"
GOOD: "Hardcoded Django SECRET_KEY 'django-insecure-m86...' committed to source"
BAD: "Catches broad Exception — consider narrowing exception types"
GOOD: "Bare except catches all errors including KeyboardInterrupt, returns None"

FINDING TYPES:
- Use PointFinding for: a specific vulnerability at a specific line (hardcoded secret,
  SQL injection, missing auth on a route, debug flag).
- Use PatternFinding for: a vulnerability class across multiple files (sensitive data
  logged throughout, missing input validation on all routes).
  Set total_affected to the REAL count. List up to 10 example files.

INVESTIGATION APPROACH:
Every codebase has different attack surfaces — form your own strategy.
Think like an attacker, not a checklist auditor:

- What does this project do? Understand the architecture first — web app, API,
  library, service? The attack surface depends on the project type.
- Where does user input enter? Trace it to where it's used. Injection lives in
  the gap between input and sanitization.
- Where are the credentials? Config files, environment variables, source code.
  Check all of them — don't just grep for "password".
- What should be locked down but might not be? Admin routes, debug flags,
  CORS policies, TLS settings. Read the config, not just the code.
- Secrets hide in plain sight: grep for SECRET, KEY, PASSWORD, TOKEN, and
  check config files, .env files, and settings modules early — these are
  the most common critical findings and easy to miss if you start elsewhere.
- Verify everything: a grep hit is a lead, not a finding. Read the file,
  understand the context, confirm it's actually exploitable.

The vulnerability categories below define WHAT to look for — not HOW to investigate.

CRITICAL FINDINGS — directly exploitable (OWASP Top 10:2025):
- "Hardcoded Secret" [A02/A07] [PointFinding]: Literal API key, password, token, or
  connection string with the actual value visible. NOT a variable name reading from env.
- "SQL Injection" [A03] [PointFinding]: String-interpolated SQL where user input reaches
  the query. Must trace data flow from input to query.
- "Command Injection" [A03] [PointFinding]: subprocess/os.system/eval/exec with
  user-controlled input in the expression.
- "Broken Access Control" [A01] [PointFinding]: Admin/privileged route with no auth
  check visible in the route file or its middleware chain.
- "Cryptographic Failure" [A02] [PointFinding]: MD5/SHA1 for password hashing (not file
  checksums). verify=False on TLS.
- "Private Key in Repo" [A07] [PointFinding]: PEM block or -----BEGIN...KEY-----.
- "Debug Mode in Production Config" [A05] [PointFinding]: DEBUG=True in a non-test,
  non-example config file.

SIGNAL FINDINGS — important but not immediately exploitable:
- "Sensitive Data Logged" [A09] [PatternFinding]: Password/token/secret values inside
  log/print statements.
- "Missing Input Validation" [A03] [PatternFinding]: Route handlers accepting user input
  with no validation or sanitization before use.
- "Missing Lockfile" [Supply Chain] [PointFinding]: Manifest (package.json, requirements.txt)
  present with no lockfile.
- "Exposed Stack Trace" [A10:2025] [PointFinding]: Error handler returning full exception
  to HTTP client.
- "Insecure Cookie" [A07] [PatternFinding]: Cookies without HttpOnly or Secure flags.
- Any other vulnerability you find in code — report it.

NEVER REPORT:
- Tool/service absence: "No dependabot", "No rate limiting", "No SAST", "No security headers"
- "No HTTPS enforced" unless you see a config explicitly forcing HTTP
- Recommendations without a code location
- Production quality issues (missing timeouts, swallowed exceptions, no retry logic,
  missing logging, no connection pooling) — those belong in the production quality check
- Vibe coding signals (AI-generated markers, narration comments, planning docs) —
  those belong in the vibe coding check
- Anything you cannot confirm by reading the file

GROUNDING RULE: Every PointFinding needs file:line you confirmed by reading.
Every PatternFinding needs at least one example_file you read.
Do not infer from filenames alone — read the file. Return empty lists if nothing confirmed.
"""

PROD_SYSTEM_PROMPT = """\
You are a senior SRE conducting a production readiness review.
Frame: Susan Fowler's production-readiness axes + Google SRE PRR.
Not limited to any specific language.

THOROUGHNESS:
Investigate deeply — superficial passes miss real issues.
Make at least 12 tool calls before returning. Use ALL your tools freely.
Don't just scan for patterns — understand the code's failure modes by reading it.

DESCRIPTION FORMAT:
Each description must be ONE factual sentence, max 25 words.
State the problem. Do NOT give advice, recommendations, or fixes.
BAD: "aiohttp.ClientSession().post is used without a timeout, which can hang indefinitely if the gateway is slow"
GOOD: "aiohttp.ClientSession().post called without timeout parameter"
BAD: "Catches broad Exception and returns None, making it hard to distinguish error types"
GOOD: "except Exception returns None, masking all gateway failures"

FINDING TYPES:
- Use PointFinding for: a specific problem at a specific location (swallowed exception,
  missing timeout on a specific call, hardcoded config value, N+1 query).
- Use PatternFinding for: a quality issue spread across many files (bare print instead
  of logging throughout, copy-paste blocks, no failure tests).
  Set total_affected to the REAL count you measured. List up to 10 example files.

INVESTIGATION APPROACH:
Every system has different failure modes — form your own strategy.
Think like an on-call engineer, not a checklist auditor:

- What does this system depend on? Databases, APIs, queues, filesystem.
  Each dependency is a potential failure point. Check how each one is handled.
- What happens when things go wrong? Read error handling paths.
  Are exceptions caught and swallowed? Are external calls given timeouts?
  Is there retry logic where it matters?
- How is the system configured? Hardcoded values are brittle. Check if
  configuration is externalized and how defaults are set.
- Is the system observable? Can you tell what's happening from the logs?
  Are critical operations instrumented?
- Does the test suite cover failure paths, or only happy paths?

The categories below define WHAT to look for — not HOW to investigate.

CRITICAL FINDINGS — production-blocking (would cause or mask an incident):
- "Silently Swallowed Exception" [PointFinding]: bare except: pass, catch(e){}, or empty
  catch block in non-trivial code. If widespread, use PatternFinding.
- "Missing Timeout on External Call" [PointFinding]: HTTP request, DB query, or queue
  operation with no timeout in application code (not test/script).
- "N+1 Query" [PointFinding]: DB query inside a for/while loop in a request handler.
- "DB Connection Without Pooling" [PointFinding]: Direct connection in a frequently-called
  function without a pool.
- "Zero Environment Variable Usage" [PointFinding]: Config file with all values hardcoded,
  zero os.environ/process.env/config-library usage.
- "Race Condition Risk" [PointFinding]: Shared mutable state modified across threads/async
  without a lock.

SIGNAL FINDINGS — quality debt:
- "No Logging Library" [PatternFinding]: Source files with 50+ lines of logic using only
  bare print()/console.log() and NO import of any logging library at all.
  NOT "uses logging but not structured logging" — that is a style preference, not a finding.
- "Overly Long Function" [PatternFinding]: Functions exceeding ~60 lines with multiple
  concerns. List examples with function names.
- "No Failure Path Tests" [PatternFinding]: Test directory exists but zero tests cover
  error/failure scenarios.
- "Copy-Paste Block" [PatternFinding]: 3+ structurally identical code blocks.
- "Missing Retry on External Call" [PatternFinding]: HTTP calls to external services with
  no retry/backoff.
- "FIXME in Critical Path" [PointFinding]: FIXME or HACK in a request handler, auth module,
  or payment flow.
- Any other production concern you observe with evidence.

NEVER REPORT:
- Style preferences: "no structured logging" when they use the logging library, "no custom
  exception types", "could use dataclasses", "inconsistent naming"
- Tool absence: "no monitoring configured", "no alerting", "no CI pipeline"
- Global absence: "no retry logic" (only flag specific calls missing it)
- N/A findings: "no database detected", "no API routes", "no external calls"
- Recommendations without a code location
- Security vulnerabilities (hardcoded secrets, exposed credentials, injection risks,
  missing auth, ALLOWED_HOSTS wildcards, DEBUG flags) — those belong in the security check
- Vibe coding signals (AI-generated markers, narration comments, planning docs) —
  those belong in the vibe coding check
- Anything you cannot tie to a file you actually read

GROUNDING RULE: Every PointFinding must name a file:line you confirmed by reading.
Every PatternFinding must name at least one example_file you directly observed.
5 grounded findings beat 20 vague ones. Return empty lists if nothing confirmed.
"""

# ---------------------------------------------------------------------------
# Agent singletons (lazy init)
# ---------------------------------------------------------------------------

_agent_lock = threading.Lock()
_vibe_agent: Agent | None = None
_security_agent: Agent | None = None
_prod_agent: Agent | None = None


def get_vibe_agent() -> Agent:
    global _vibe_agent
    if _vibe_agent is None:
        with _agent_lock:
            if _vibe_agent is None:
                _vibe_agent = make_agent(VIBE_SYSTEM_PROMPT)
    return _vibe_agent


def get_security_agent() -> Agent:
    global _security_agent
    if _security_agent is None:
        with _agent_lock:
            if _security_agent is None:
                _security_agent = make_agent(SECURITY_SYSTEM_PROMPT)
    return _security_agent


def get_prod_agent() -> Agent:
    global _prod_agent
    if _prod_agent is None:
        with _agent_lock:
            if _prod_agent is None:
                _prod_agent = make_agent(PROD_SYSTEM_PROMPT)
    return _prod_agent


# ---------------------------------------------------------------------------
# Runner functions
# ---------------------------------------------------------------------------


def run_vibe_agent(root: str) -> tuple[list[str], list[str]]:
    """Run the vibe coding check agent on a cloned repo.

    Returns (critical_list, signals_list). Raises on any error.
    """
    if not os.path.isdir(root):
        raise ValueError(
            f"run_vibe_agent: root {root!r} does not exist or is not a directory"
        )
    output = run_agent(
        get_vibe_agent(),
        (
            f"Analyze this repository for AI-generation signals.\n"
            f"Repo root: {root}\n\n"
            "Investigate freely — form hypotheses, test them, follow leads. "
            "Report only findings you can ground in code you directly observed."
        ),
        root,
    )
    return serialize(output)


def run_security_agent(root: str) -> tuple[list[str], list[str]]:
    """Run the security check agent on a cloned repo.

    Returns (critical_list, signals_list). Raises on any error.
    """
    if not os.path.isdir(root):
        raise ValueError(
            f"run_security_agent: root {root!r} does not exist or is not a directory"
        )
    output = run_agent(
        get_security_agent(),
        (
            f"Conduct a security review of this repository.\n"
            f"Repo root: {root}\n\n"
            "Investigate freely — think like an attacker, find real exploitable "
            "vulnerabilities. Report only findings you can ground in code you "
            "directly observed."
        ),
        root,
    )
    return serialize(output)


def run_production_agent(root: str) -> tuple[list[str], list[str]]:
    """Run the production quality check agent on a cloned repo.

    Returns (critical_list, signals_list). Raises on any error.
    """
    if not os.path.isdir(root):
        raise ValueError(
            f"run_production_agent: root {root!r} does not exist or is not a directory"
        )
    output = run_agent(
        get_prod_agent(),
        (
            f"Conduct a production readiness review of this repository.\n"
            f"Repo root: {root}\n\n"
            "Investigate freely — think about how this system fails under "
            "pressure. Report only findings you can ground in code you "
            "directly observed."
        ),
        root,
    )
    return serialize(output)
