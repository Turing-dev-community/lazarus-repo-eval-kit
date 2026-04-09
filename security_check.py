import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)

DEFAULT_MODEL = "gpt-5.1"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PYTHON_EXTS = {".py"}
JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
SOURCE_EXTS = PYTHON_EXTS | JS_EXTS
CONFIG_EXTS = {".json", ".yml", ".yaml", ".toml", ".cfg", ".lock"}

SKIP_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "env",
    "dist",
    "build",
    ".next",
    "coverage",
    ".tox",
    "vendor",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    "target",
}

MAX_FILE_SIZE = 512_000

LOCKFILE_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Pipfile.lock",
    "poetry.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "go.sum",
    "composer.lock",
}
MANIFEST_NAMES = {
    "package.json",
    "requirements.txt",
    "Pipfile",
    "pyproject.toml",
    "Cargo.toml",
    "Gemfile",
    "go.mod",
    "composer.json",
    "pom.xml",
    "build.gradle",
}

# ---------------------------------------------------------------------------
# Language detection (reused from quality script)
# ---------------------------------------------------------------------------


def _detect_language(files: list[str]) -> str:
    py = sum(1 for f in files if os.path.splitext(f)[1] in PYTHON_EXTS)
    js = sum(1 for f in files if os.path.splitext(f)[1] in JS_EXTS)
    return "python" if py >= js else "js"


# ---------------------------------------------------------------------------
# Patterns — organised per category
# ---------------------------------------------------------------------------

# Category 1: Secrets
SECRET_PATTERNS = [
    (
        re.compile(r"""(?:password|passwd|pwd)\s*[=:]\s*["'][^"']{4,}["']""", re.I),
        "Hardcoded password",
    ),
    (
        re.compile(
            r"""(?:secret|api_?key|apikey|access_?key|auth_?token|private_?key)\s*[=:]\s*["'][^"']{8,}["']""",
            re.I,
        ),
        "Hardcoded secret/API key",
    ),
    (re.compile(r"""(?:AKIA|ASIA)[A-Z0-9]{16}"""), "AWS Access Key ID"),
    (re.compile(r"""ghp_[A-Za-z0-9]{36}"""), "GitHub PAT"),
    (re.compile(r"""gho_[A-Za-z0-9]{36}"""), "GitHub OAuth token"),
    (re.compile(r"""sk-[A-Za-z0-9]{20,}"""), "OpenAI/Stripe secret key"),
    (
        re.compile(r"""-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"""),
        "Private key in source",
    ),
    (
        re.compile(
            r"""(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis)://[^\s'"]{10,}""",
            re.I,
        ),
        "DB connection string with credentials",
    ),
    (re.compile(r"""xox[bpsa]-[A-Za-z0-9-]{10,}"""), "Slack token"),
    (
        re.compile(r"""Bearer\s+[A-Za-z0-9\-._~+/]{20,}=*""", re.I),
        "Hardcoded Bearer token",
    ),
]

# Category 3: Sensitive logging
LOG_PATTERNS = [
    (
        re.compile(
            r"""(?:console\.log|print|logger\.\w+|logging\.\w+)\s*\(.*(?:password|secret|token|api_?key|credential|ssn|credit.?card)""",
            re.I,
        ),
        "Logging sensitive field",
    ),
    (
        re.compile(
            r"""(?:console\.log|print|logger\.\w+)\s*\(.*(?:req(?:uest)?\.body|req\.headers)""",
            re.I,
        ),
        "Logging raw request body/headers",
    ),
]

# Category 4: Auth
ROUTE_PATTERNS = [
    re.compile(
        r"""(?:app|router)\.(?:get|post|put|delete|patch)\s*\(\s*["'`]([^"'`]+)""", re.I
    ),
    re.compile(r"""@app\.route\s*\(\s*["']([^"']+)""", re.I),
    re.compile(r"""path\s*\(\s*["']([^"']+)["'].*(?:views?\.|ViewSet|APIView)""", re.I),
    re.compile(
        r"""export\s+(?:default\s+)?(?:async\s+)?function\s+(?:GET|POST|PUT|DELETE|PATCH|handler)\b""",
        re.I,
    ),
]
ADMIN_ROUTE_RE = re.compile(r"""(?:admin|superuser|staff|manage|dashboard)""", re.I)
AUTH_RE = re.compile(
    r"(?:authenticate|authorize|isAuthenticated|requireAuth|@login_required|"
    r"protect|jwt\.verify|passport\.authenticate|authMiddleware|ensureAuth|"
    r"checkAuth|verifyToken|auth_required|@requires_auth)",
    re.I,
)
RATE_LIMIT_RE = re.compile(
    r"(?:express-rate-limit|rate-limiter-flexible|ratelimit|slowapi|"
    r"django-ratelimit|throttle|RateLimiter)",
    re.I,
)
VALIDATION_RE = re.compile(
    r"(?:joi|zod|express-validator|pydantic|marshmallow|@IsString|"
    r"@IsEmail|class-validator|cerberus|wtforms|yup|ajv|jsonschema)",
    re.I,
)

# Category 5: Injection
INJECTION_PATTERNS = [
    (
        re.compile(
            r"""(?:execute|query|raw)\s*\(\s*(?:f["']|["']\s*%|["']\s*\+|['"].*\$\{)""",
            re.I,
        ),
        "SQL injection risk",
    ),
    (
        re.compile(
            r"""(?:SELECT|INSERT|UPDATE|DELETE)\s+.*(?:\$\{|\+\s*(?:req|request|params|query|input|user))""",
            re.I,
        ),
        "SQL user-input concatenation",
    ),
    (
        re.compile(
            r"""(?:subprocess\.(?:call|run|Popen|check_output)|os\.(?:system|popen)|child_process\.exec)\s*\(.*(?:req\.|request\.|input|user|params|argv)""",
            re.I,
        ),
        "Command injection",
    ),
    (
        re.compile(
            r"""(?:eval|exec)\s*\(\s*(?:req\.|request\.|input|user|params|data)""", re.I
        ),
        "eval/exec with user input",
    ),
    (
        re.compile(
            r"""(?:open|readFile|readFileSync|createReadStream|send_file|send_from_directory)\s*\(.*(?:req\.|request\.|params|query|input)""",
            re.I,
        ),
        "Path traversal",
    ),
    (
        re.compile(
            r"""(?:render_template_string|Template)\s*\(.*(?:req\.|request\.|input|user)""",
            re.I,
        ),
        "Template injection",
    ),
]

# Category 6: Debug exposure
DEBUG_PATTERNS = [
    (
        re.compile(
            r"""(?:traceback\.print_exc|traceback\.format_exc|\.printStackTrace)""",
            re.I,
        ),
        "Stack trace exposed",
    ),
    (
        re.compile(
            r"""res\.status\(\d+\)\.(?:send|json)\s*\(\s*(?:err|error|e)(?:\.stack|\.message)?""",
            re.I,
        ),
        "Error details in response",
    ),
    (re.compile(r"""DEBUG\s*=\s*True"""), "DEBUG mode enabled"),
    (re.compile(r"""app\.run\s*\(.*debug\s*=\s*True""", re.I), "Flask debug mode on"),
]

# Category 7: Crypto
CRYPTO_PATTERNS = [
    (
        re.compile(r"""(?:hashlib\.md5|MD5\.Create|MessageDigest.*MD5|md5\()""", re.I),
        "Weak hash: MD5",
    ),
    (
        re.compile(
            r"""(?:hashlib\.sha1|SHA1\.Create|MessageDigest.*SHA-?1|sha1\()""", re.I
        ),
        "Weak hash: SHA-1",
    ),
    (re.compile(r"""(?:DES(?:ede)?|RC4|RC2)\s*[.(]"""), "Weak cipher"),
    (
        re.compile(
            r"""(?:password|passwd|pwd)\s*==\s*(?:req\.|request\.|params|input|data)""",
            re.I,
        ),
        "Plaintext password comparison",
    ),
    (
        re.compile(
            r"""http://(?!localhost|127\.0\.0\.1|0\.0\.0\.0|::1|example\.)""", re.I
        ),
        "Non-TLS HTTP URL",
    ),
    (re.compile(r"""verify\s*=\s*False""", re.I), "TLS verification disabled"),
    (
        re.compile(r"""rejectUnauthorized\s*:\s*false""", re.I),
        "Node TLS verification disabled",
    ),
]

# Category 9: CORS / headers / cookies
CORS_PATTERNS = [
    (
        re.compile(r"""Access-Control-Allow-Origin['":\s]*\*""", re.I),
        "Wildcard CORS origin",
    ),
    (re.compile(r"""cors\(\s*\)""", re.I), "CORS enabled with defaults"),
    (re.compile(r"""origin\s*:\s*(?:true|['"]?\*)""", re.I), "Permissive CORS origin"),
    (
        re.compile(r"""(?:httpOnly|httponly)\s*:\s*false""", re.I),
        "Cookie missing httpOnly",
    ),
    (re.compile(r"""(?:secure)\s*:\s*false""", re.I), "Cookie missing secure flag"),
    (
        re.compile(r"""(?:sameSite|samesite)\s*:\s*['"]?none['"]?""", re.I),
        "Cookie SameSite=None",
    ),
]
SECURITY_HEADER_RE = re.compile(
    r"(?:helmet|django\.middleware\.security|secure-headers|flask-talisman|"
    r"Content-Security-Policy|Strict-Transport-Security|X-Frame-Options|X-Content-Type-Options)",
    re.I,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout
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
    return r.returncode == 0, r.stderr.strip()


def _find_files(root: str, exts: set[str]) -> list[str]:
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if os.path.splitext(f)[1] in exts:
                full = os.path.join(dirpath, f)
                try:
                    if os.path.getsize(full) <= MAX_FILE_SIZE:
                        results.append(full)
                except OSError:
                    pass
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
    return any(p in rel for p in ["test", "spec", "mock", "fixture", "__tests__"])


def _is_comment_line(line: str) -> bool:
    s = line.strip()
    return s.startswith(("#", "//", "/*", "*", "<!--"))


# ---------------------------------------------------------------------------
# Category 1: Secrets
# ---------------------------------------------------------------------------


def _scan_secrets(root: str, files: list[str]) -> tuple[int, list[str]]:
    count = 0
    details = []

    for f in files:
        content = _read(f)
        rel = _rel(f, root)
        is_test = _is_test(f, root)

        for pat, desc in SECRET_PATTERNS:
            for m in pat.finditer(content):
                # Skip comment lines
                line = content[
                    content.rfind("\n", 0, m.start()) + 1 : content.find("\n", m.end())
                ]
                if _is_comment_line(line):
                    continue
                # In test files only flag real token patterns (AWS, GitHub)
                if is_test and not any(
                    x in m.group() for x in ["AKIA", "ASIA", "ghp_", "gho_", "BEGIN"]
                ):
                    continue
                count += 1
                details.append(f"SECRET ({desc}) in {rel}")
                break  # one finding per pattern per file

    # Git history — scan deleted content across multiple secret terms
    history_terms = ["password", "secret", "api_key", "token", "private_key"]
    history_count = 0
    seen_desc: set[str] = set()
    for term in history_terms:
        try:
            out = _run_git(
                [
                    "log",
                    "--all",
                    "-p",
                    "-n",
                    "50",
                    "--no-merges",
                    "--diff-filter=D",
                    f"-S{term}",
                ],
                root,
                timeout=60,
            )
            if not out:
                continue
            for pat, desc in SECRET_PATTERNS:
                if desc in seen_desc:
                    continue
                matches = pat.findall(out)
                # Only count lines that start with '+' (additions in diff)
                real = [
                    m
                    for m in matches
                    if out.find(m) > 0
                    and out[
                        out.rfind("\n", 0, out.find(m)) + 1 : out.find(m)
                    ].startswith("+")
                ]
                if real:
                    history_count += len(real)
                    seen_desc.add(desc)
                    details.append(
                        f"SECRET_IN_HISTORY ({desc}): {len(real)} occurrence(s)"
                    )
        except Exception:
            pass

    return count + history_count, details


# ---------------------------------------------------------------------------
# Category 2: Dependencies
# ---------------------------------------------------------------------------


def _scan_dependencies(root: str) -> tuple[int, list[str]]:
    count = 0
    details = []

    root_files = os.listdir(root) if os.path.isdir(root) else []
    manifests = [f for f in root_files if f in MANIFEST_NAMES]
    lockfiles = [f for f in root_files if f in LOCKFILE_NAMES]

    if manifests and not lockfiles:
        count += 1
        details.append(
            f"No lockfile found (manifest present: {', '.join(manifests[:2])})"
        )

    # npm audit — only if node_modules exists (i.e. npm install was run)
    pkg_json = os.path.join(root, "package.json")
    node_modules = os.path.join(root, "node_modules")
    if os.path.exists(pkg_json) and os.path.exists(node_modules):
        try:
            r = subprocess.run(
                ["npm", "audit", "--json"],
                capture_output=True,
                text=True,
                check=False,
                cwd=root,
                timeout=120,
            )
            if r.stdout.strip():
                audit = json.loads(r.stdout)
                vulns = audit.get("vulnerabilities", audit.get("advisories", {}))
                if isinstance(vulns, dict):
                    crit_high = sum(
                        1
                        for v in vulns.values()
                        if isinstance(v, dict)
                        and v.get("severity") in ("critical", "high")
                    )
                    if crit_high:
                        count += crit_high
                        details.append(
                            f"npm audit: {crit_high} critical/high vulnerabilities"
                        )
                    if len(vulns) > crit_high:
                        details.append(
                            f"npm audit: {len(vulns)} total vulnerable packages"
                        )
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass

    # pip-audit
    py_manifests = [
        f
        for f in ("requirements.txt", "Pipfile", "pyproject.toml")
        if os.path.exists(os.path.join(root, f))
    ]
    if py_manifests:
        try:
            r = subprocess.run(
                ["pip-audit", "--format", "json", "--desc"],
                capture_output=True,
                text=True,
                check=False,
                cwd=root,
                timeout=120,
            )
            if r.stdout.strip():
                items = json.loads(r.stdout)
                if isinstance(items, dict):
                    items = items.get("dependencies", [])
                vuln_pkgs = [i for i in items if isinstance(i, dict) and i.get("vulns")]
                if vuln_pkgs:
                    count += len(vuln_pkgs)
                    details.append(f"pip-audit: {len(vuln_pkgs)} vulnerable packages")
        except (
            subprocess.TimeoutExpired,
            OSError,
            json.JSONDecodeError,
            FileNotFoundError,
        ):
            pass  # pip-audit not installed — skip silently

    return count, details


# ---------------------------------------------------------------------------
# Category 3: Sensitive logging
# ---------------------------------------------------------------------------


def _scan_sensitive_logging(root: str, files: list[str]) -> tuple[int, list[str]]:
    count = 0
    by_desc: dict[str, list[str]] = {}
    for f in files:
        if _is_test(f, root):
            continue
        content = _read(f)
        rel = _rel(f, root)
        for pat, desc in LOG_PATTERNS:
            if pat.search(content):
                count += 1
                by_desc.setdefault(desc, []).append(rel)
                break
    details = []
    for desc, affected in by_desc.items():
        if len(affected) == 1:
            details.append(f"SENSITIVE_LOG ({desc}) in {affected[0]}")
        else:
            details.append(
                f"SENSITIVE_LOG ({desc}) in {len(affected)} files: {', '.join(affected[:5])}"
                + (" ..." if len(affected) > 5 else "")
            )
    return count, details


# ---------------------------------------------------------------------------
# Category 4: Auth / access control
# ---------------------------------------------------------------------------


def _scan_auth(root: str, files: list[str]) -> tuple[int, list[str]]:
    count = 0
    details = []

    # Detect globally registered auth middleware (e.g. app.use(authMiddleware))
    global_auth_files: set[str] = set()
    for f in files:
        if _is_test(f, root):
            continue
        content = _read(f)
        # Global middleware registration patterns
        if re.search(
            r"(?:app\.use|app\.all)\s*\(.*(?:auth|protect|jwt|passport|requireAuth)",
            content,
            re.I,
        ):
            global_auth_files.add(_rel(f, root))

    has_global_auth = len(global_auth_files) > 0
    has_rate_limiting = False
    total_route_files = 0
    unprotected = 0
    admin_no_auth = 0
    no_validation = 0

    for f in files:
        if _is_test(f, root):
            continue
        content = _read(f)
        rel = _rel(f, root)

        # Check if file has routes
        file_has_routes = any(pat.search(content) for pat in ROUTE_PATTERNS)
        if not file_has_routes:
            continue

        total_route_files += 1
        file_has_auth = bool(AUTH_RE.search(content)) or has_global_auth

        routes = [m for pat in ROUTE_PATTERNS for m in pat.findall(content) if m]
        has_admin = any(ADMIN_ROUTE_RE.search(r) for r in routes if r)

        if not file_has_auth:
            unprotected += 1
            details.append(f"UNPROTECTED_ROUTES in {rel}")

        if has_admin and not file_has_auth:
            admin_no_auth += 1
            details.append(f"ADMIN_NO_AUTH in {rel}")

        if not VALIDATION_RE.search(content):
            no_validation += 1

        if RATE_LIMIT_RE.search(content):
            has_rate_limiting = True

    count += unprotected + admin_no_auth

    if total_route_files > 0 and not has_rate_limiting:
        count += 1
        details.append("No rate-limiting library detected")

    if total_route_files > 0 and no_validation > total_route_files * 0.5:
        details.append(
            f"{no_validation}/{total_route_files} route files lack input validation library"
        )

    return count, details


# ---------------------------------------------------------------------------
# Category 5: Injection
# ---------------------------------------------------------------------------


def _scan_injections(root: str, files: list[str]) -> tuple[int, list[str]]:
    count = 0
    by_desc: dict[str, list[str]] = {}
    for f in files:
        if _is_test(f, root):
            continue
        content = _read(f)
        rel = _rel(f, root)
        for pat, desc in INJECTION_PATTERNS:
            if pat.search(content):
                count += 1
                by_desc.setdefault(desc, []).append(rel)
    details = []
    for desc, affected in by_desc.items():
        if len(affected) == 1:
            details.append(f"INJECTION ({desc}) in {affected[0]}")
        else:
            details.append(
                f"INJECTION ({desc}) in {len(affected)} files: {', '.join(affected[:5])}"
                + (" ..." if len(affected) > 5 else "")
            )
    return count, details


# ---------------------------------------------------------------------------
# Category 6: Debug exposure
# ---------------------------------------------------------------------------


def _scan_debug(root: str, files: list[str]) -> tuple[int, list[str]]:
    count = 0
    by_desc: dict[str, list[str]] = {}
    for f in files:
        if _is_test(f, root):
            continue
        content = _read(f)
        rel = _rel(f, root)
        for pat, desc in DEBUG_PATTERNS:
            if pat.search(content):
                count += 1
                by_desc.setdefault(desc, []).append(rel)
                break
    details = []
    for desc, affected in by_desc.items():
        if len(affected) == 1:
            details.append(f"DEBUG_EXPOSURE ({desc}) in {affected[0]}")
        else:
            details.append(
                f"DEBUG_EXPOSURE ({desc}) in {len(affected)} files: {', '.join(affected[:5])}"
                + (" ..." if len(affected) > 5 else "")
            )
    return count, details


# ---------------------------------------------------------------------------
# Category 7: Crypto
# ---------------------------------------------------------------------------


def _scan_crypto(root: str, files: list[str]) -> tuple[int, list[str]]:
    count = 0
    by_desc: dict[str, list[str]] = {}
    for f in files:
        if _is_test(f, root):
            continue
        content = _read(f)
        rel = _rel(f, root)
        for pat, desc in CRYPTO_PATTERNS:
            found = False
            for line in content.split("\n"):
                if _is_comment_line(line):
                    continue
                if "http://" in pat.pattern and re.match(
                    r"\s*(?:import|from|require)", line
                ):
                    continue
                if pat.search(line):
                    found = True
                    break
            if found:
                count += 1
                by_desc.setdefault(desc, []).append(rel)
    details = []
    for desc, affected in by_desc.items():
        if len(affected) == 1:
            details.append(f"CRYPTO ({desc}) in {affected[0]}")
        else:
            details.append(
                f"CRYPTO ({desc}) in {len(affected)} files: {', '.join(affected[:5])}"
                + (" ..." if len(affected) > 5 else "")
            )
    return count, details


# ---------------------------------------------------------------------------
# Category 8: Supply chain
# ---------------------------------------------------------------------------


def _scan_supply_chain(root: str) -> tuple[int, list[str]]:
    count = 0
    details = []

    # package.json git-based deps
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn != "package.json":
                continue
            rel = _rel(os.path.join(dirpath, fn), root)
            try:
                data = json.loads(_read(os.path.join(dirpath, fn)))
                for section in ("dependencies", "devDependencies"):
                    for name, ver in data.get(section, {}).items():
                        if isinstance(ver, str) and any(
                            x in ver
                            for x in ["github:", "git+", "git://", "bitbucket:"]
                        ):
                            count += 1
                            details.append(f"GIT_DEP in {rel}: {name} → {ver[:60]}")
            except (json.JSONDecodeError, OSError):
                pass

    # requirements.txt git deps
    for req_file in ("requirements.txt",):
        path = os.path.join(root, req_file)
        if os.path.exists(path):
            git_lines = re.findall(r"(?:-e\s+)?git\+[^\s]+", _read(path))
            if git_lines:
                count += len(git_lines)
                details.append(
                    f"GIT_DEP in {req_file}: {len(git_lines)} git-sourced dep(s)"
                )

    # pyproject.toml / Pipfile / Cargo.toml
    for fname in ("pyproject.toml", "Pipfile", "Cargo.toml"):
        path = os.path.join(root, fname)
        if os.path.exists(path):
            refs = re.findall(r'git\s*=\s*["\'][^"\']+["\']', _read(path))
            if refs:
                count += len(refs)
                details.append(f"GIT_DEP in {fname}: {len(refs)} git-sourced dep(s)")

    # Unofficial npm registry
    npmrc = os.path.join(root, ".npmrc")
    if os.path.exists(npmrc):
        if re.search(r"registry\s*=\s*(?!https://registry\.npmjs\.org)", _read(npmrc)):
            count += 1
            details.append("UNOFFICIAL_REGISTRY: custom npm registry in .npmrc")

    # Unofficial pip index
    for fname in ("pip.conf", "requirements.txt"):
        path = os.path.join(root, fname)
        if os.path.exists(path):
            if re.search(
                r"--index-url|--extra-index-url|index-url\s*=", _read(path), re.I
            ):
                count += 1
                details.append(f"UNOFFICIAL_REGISTRY: custom index-url in {fname}")
                break

    return count, details


# ---------------------------------------------------------------------------
# Category 9: CORS / headers / cookies
# ---------------------------------------------------------------------------


def _scan_cors_headers(root: str, files: list[str]) -> tuple[int, list[str]]:
    count = 0
    details = []
    has_security_lib = False

    for f in files:
        if _is_test(f, root):
            continue
        content = _read(f)
        rel = _rel(f, root)

        if SECURITY_HEADER_RE.search(content):
            has_security_lib = True

        for pat, desc in CORS_PATTERNS:
            # Skip comment lines to reduce false positives
            for line in content.split("\n"):
                if _is_comment_line(line):
                    continue
                if pat.search(line):
                    tag = (
                        "CORS"
                        if "origin" in pat.pattern.lower()
                        or "cors" in pat.pattern.lower()
                        else "INSECURE_COOKIE"
                    )
                    count += 1
                    details.append(f"{tag} ({desc}) in {rel}")
                    break

    has_routes = any(
        any(pat.search(_read(f)) for pat in ROUTE_PATTERNS)
        for f in files
        if not _is_test(f, root)
    )
    if has_routes and not has_security_lib:
        count += 1
        details.append(
            "MISSING_SECURITY_HEADERS: no helmet/talisman/CSP middleware detected"
        )

    return count, details


# ---------------------------------------------------------------------------
# Dependabot / Renovate check (folded into supply chain display)
# ---------------------------------------------------------------------------


def _scan_dependabot(root: str) -> tuple[int, list[str]]:
    paths = [
        ".github/dependabot.yml",
        ".github/dependabot.yaml",
        "renovate.json",
        "renovate.json5",
        ".renovaterc",
        ".renovaterc.json",
    ]
    for p in paths:
        if os.path.exists(os.path.join(root, p)):
            return 0, []
    # Check package.json for inline renovate config
    pkg = os.path.join(root, "package.json")
    if os.path.exists(pkg):
        if '"renovate"' in _read(pkg):
            return 0, []
    return 1, ["No dependabot/renovate configuration found"]


# ---------------------------------------------------------------------------
# Smart sampling for LLM (security-relevant files)
# ---------------------------------------------------------------------------

# Patterns that indicate security-relevant code
_SECURITY_SIGNAL_RE = re.compile(
    r"(?:password|secret|token|api_?key|credential|"
    r"authenticate|authorize|login|jwt|oauth|session|cookie|"
    r"execute|query|eval|exec|subprocess|os\.system|"
    r"cors|helmet|csp|verify|encrypt|decrypt|hash|"
    r"traceback|DEBUG|console\.log|logging\.|print\s*\()",
    re.I,
)


def _extract_flagged_paths(root: str, static_details: list[str]) -> set[str]:
    """Extract absolute file paths from static finding strings like:
    'SECRET (Hardcoded password) in src/config.py'"""
    flagged: set[str] = set()
    for detail in static_details:
        if " in " not in detail:
            continue
        rel_path = detail.split(" in ")[-1].strip()
        abs_path = os.path.join(root, rel_path)
        if os.path.isfile(abs_path):
            flagged.add(abs_path)
    return flagged


def _snippet_for_file(f: str, root: str, flagged: bool = False) -> str:
    """Extract a code snippet from a file.
    Flagged files: wider context (±10 lines), max 150 lines, always include first 50 if no signals.
    Other files: narrower context (±5 lines), max 100 lines, skip if no signals."""
    content = _read(f)
    if not content.strip():
        return ""
    lines = content.split("\n")
    context = 10 if flagged else 5
    max_lines = 150 if flagged else 100

    relevant: set[int] = set()
    for i, line in enumerate(lines):
        if _SECURITY_SIGNAL_RE.search(line):
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                relevant.add(j)

    if not relevant:
        if flagged:
            relevant = set(range(min(50, len(lines))))
        else:
            return ""

    selected = sorted(relevant)[:max_lines]
    sample = "\n".join(f"{i + 1}: {lines[i]}" for i in selected)
    tag = " [FLAGGED BY STATIC ANALYSIS]" if flagged else ""
    return f"\n--- {_rel(f, root)}{tag} ---\n{sample}\n"


def _smart_sample_security(
    root: str, files: list[str], static_details: list[str], token_budget: int = 4000
) -> str:
    """
    Pick the most security-relevant files for LLM review.

    Priority order:
      1. Files flagged by static analysis — always included first (guaranteed)
      2. Route/auth/middleware files by name
      3. Files with highest security signal density
      4. One file per top-level directory for breadth

    Flagged files get larger snippets and are labelled [FLAGGED BY STATIC ANALYSIS]
    so the LLM knows exactly which code to verify.
    """
    non_test = [f for f in files if not _is_test(f, root)]
    if not non_test:
        return ""

    char_budget = token_budget * 4
    flagged_paths = _extract_flagged_paths(root, static_details)

    snippets: list[str] = []
    total_chars = 0
    included: set[str] = set()

    # Pass 1: flagged files first — guaranteed inclusion
    for f in non_test:
        if f not in flagged_paths:
            continue
        chunk = _snippet_for_file(f, root, flagged=True)
        if not chunk:
            continue
        if total_chars + len(chunk) > char_budget:
            break
        snippets.append(chunk)
        total_chars += len(chunk)
        included.add(f)

    # Pass 2: fill remaining budget with scored non-flagged files
    remaining = [f for f in non_test if f not in included]
    scores: dict[str, int] = defaultdict(int)

    for f in remaining:
        hits = len(_SECURITY_SIGNAL_RE.findall(_read(f)))
        scores[f] += hits * 2

    for f in remaining:
        rel_lower = _rel(f, root).lower()
        if any(
            kw in rel_lower
            for kw in [
                "auth",
                "route",
                "middleware",
                "security",
                "login",
                "user",
                "api",
                "controller",
            ]
        ):
            scores[f] += 20

    seen_dirs: set[str] = set()
    for f in sorted(remaining, key=lambda x: -scores[x]):
        parts = _rel(f, root).split(os.sep)
        top = parts[0] if len(parts) > 1 else "__root__"
        if top not in seen_dirs:
            scores[f] += 10
            seen_dirs.add(top)

    for f in sorted(remaining, key=lambda x: -scores[x]):
        chunk = _snippet_for_file(f, root, flagged=False)
        if not chunk:
            continue
        if total_chars + len(chunk) > char_budget:
            break
        snippets.append(chunk)
        total_chars += len(chunk)

    return "".join(snippets)


# ---------------------------------------------------------------------------
# LLM analysis — single call per repo
# ---------------------------------------------------------------------------

_LLM_SYSTEM = """You are a senior application security engineer reviewing code for production readiness.

Severity rubric (apply consistently across all categories):
  critical — directly exploitable with no/low auth, leads to RCE, data breach, or credential theft
  high     — exploitable with moderate effort, significant data or auth impact
  medium   — requires specific conditions to exploit, moderate impact
  low      — best-practice violation, minimal direct exploitability

Rules:
  - Only flag issues you can directly evidence from the code samples provided
  - Do NOT flag theoretical issues you cannot see in the code
  - If a finding is ambiguous, prefer low severity over skipping it
  - Prefer false negatives over false positives for critical/high severity
  - Return ONLY valid JSON — no markdown, no preamble, no explanation outside JSON"""

_LLM_CATEGORIES = """
  secrets        — hardcoded API keys, tokens, passwords, connection strings, private keys
  injection      — SQL/command/path traversal/template injection with user-controlled input
  auth           — missing authentication, broken access control, IDOR, no rate limiting
  logging        — sensitive fields (passwords, tokens, PII) in log statements
  crypto         — MD5/SHA1 for security, plaintext password comparison, disabled TLS
  debug          — stack traces in responses, DEBUG=True, verbose error messages
  cors           — wildcard CORS, missing security headers (CSP/HSTS), insecure cookie flags
  supply_chain   — git-sourced deps, unofficial registries, missing lockfile
"""


def _llm_analyze(
    code_samples: str, automated: dict, client, model: str, lang: str
) -> dict:
    """Single LLM call per repo covering all 9 security categories."""

    # Summarise automated findings for context
    auto_lines = []
    for cat, items in automated.items():
        if items:
            auto_lines.append(f"  {cat}: {len(items)} finding(s)")
            for d in items[:5]:
                auto_lines.append(f"    - {d}")
    auto_summary = "\n".join(auto_lines) if auto_lines else "  (none)"

    prompt = f"""Repository language: {lang}

Automated static analysis findings (may contain false positives — verify each):
{auto_summary}

Security categories to review:
{_LLM_CATEGORIES}

Code samples (line numbers included):
{code_samples}

Tasks:
1. Review the automated findings above — confirm which are real vs false positives
2. Identify any security issues the automated scanner missed
3. Apply the severity rubric consistently

Return this exact JSON structure:
{{
  "findings": [
    {{
      "category": "secrets|injection|auth|logging|crypto|debug|cors|supply_chain",
      "file": "relative/path/to/file",
      "line": <integer or null>,
      "severity": "critical|high|medium|low",
      "description": "what the vulnerability is and why it matters",
      "recommendation": "specific fix",
      "automated_confirmed": true
    }}
  ],
  "false_positives": [
    "description of automated finding that is NOT a real issue (include file path)"
  ],
  "summary": "2-3 sentence overall security assessment of this repository"
}}

Notes:
- Set automated_confirmed=true if the finding came from automated scanner, false if LLM-discovered
- findings array should include ONLY new LLM-discovered issues (automated_confirmed=false) with specific file paths and line numbers; do NOT repeat automated findings
- false_positives should list automated findings you are rejecting with a reason
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
        # Strip accidental markdown fences
        raw = re.sub(r"```(?:json)?", "", raw).strip()
        start, end = raw.find("{"), raw.rfind("}") + 1
        if start >= 0 and end > start:
            raw = raw[start:end]
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "error": "JSON parse failed",
            "findings": [],
            "false_positives": [],
            "summary": "",
        }
    except Exception as exc:
        return {
            "error": str(exc),
            "findings": [],
            "false_positives": [],
            "summary": "",
        }


# ---------------------------------------------------------------------------
# Build final deduplicated findings list
# ---------------------------------------------------------------------------


def _build_final_details(automated_details: list[str], llm_result: dict) -> list[str]:
    """
    Merge automated + LLM findings:
      - Remove automated findings that LLM marked as false positives
      - Add only new LLM-discovered findings (automated_confirmed=False)
    """
    if not llm_result or "findings" not in llm_result:
        return list(automated_details)

    # Collect false positive signals from LLM
    fp_texts = [
        fp.lower()
        for fp in llm_result.get("false_positives", [])
        if isinstance(fp, str)
    ]

    def _is_false_positive(detail: str) -> bool:
        dl = detail.lower()
        for fp in fp_texts:
            words = [w for w in dl.split() if len(w) > 4]
            if any(w in fp for w in words) and any(w in fp for w in dl.split("/")[-1:]):
                return True
        return False

    final: list[str] = []

    # Automated findings — keep unless false positive
    for d in automated_details:
        if _is_false_positive(d):
            continue
        final.append(d)

    # Only add genuinely new LLM-discovered findings
    for f in llm_result.get("findings", []):
        if not isinstance(f, dict) or "description" not in f:
            continue
        if f.get("automated_confirmed"):
            continue
        sev = f.get("severity", "medium").upper()
        cat = f.get("category", "?")
        fp = f.get("file", "?")
        desc = f.get("description", "")[:200]
        rec = f.get("recommendation", "")[:100]
        final.append(f"[{sev}] ({cat}) {desc} — fix: {rec} — in {fp}")

    return final


def _split_by_severity(final_details: list[str]) -> tuple[list[str], list[str]]:
    """Split final findings into critical-tier and signal-tier."""
    # Critical categories
    critical_keywords = {
        "secret",
        "injection",
        "sql",
        "command",
        "crypto",
        "tls",
        "password",
        "credential",
        "akia",
        "ghp_",
        "private key",
        "critical",
        "[critical",
        "[high",
    }
    critical, signals = [], []
    for d in final_details:
        dl = d.lower()
        if any(kw in dl for kw in critical_keywords):
            critical.append(d)
        else:
            signals.append(d)
    return critical, signals


# ---------------------------------------------------------------------------
# Per-repo orchestrator
# ---------------------------------------------------------------------------

CATEGORY_KEYS = [
    "secrets",
    "dep_vulns",
    "sensitive_logging",
    "auth_issues",
    "injection_risks",
    "debug_exposure",
    "deprecated_crypto",
    "supply_chain",
    "cors_headers",
]


def _check_repo(
    owner: str,
    repo: str,
    token: str,
    clone_base: str,
    client=None,
    model: str = DEFAULT_MODEL,
    skip_llm: bool = False,
    sample_tokens: int = 8000,
    verbose_log=None,
    existing_repo_path: str | None = None,
) -> dict:

    result = {
        "repo": repo,
        "language": "unknown",
        "secrets": 0,
        "dep_vulns": 0,
        "sensitive_logging": 0,
        "auth_issues": 0,
        "injection_risks": 0,
        "debug_exposure": 0,
        "deprecated_crypto": 0,
        "supply_chain": 0,
        "cors_headers": 0,
        "dependabot": 0,
        "total_findings": 0,
        "llm_findings": 0,
        "files_scanned": 0,
        "has_issues": False,
        "error": None,
        "details": [],
        "final_details": [],
        "final_details_critical": [],
        "final_details_signals": [],
        "final_details_count": 0,
        "llm_analysis": {},
        "llm_summary": "",
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

    source_files = _find_files(root, SOURCE_EXTS)
    if not source_files:
        result["error"] = "no source files"
        if owns_clone:
            shutil.rmtree(clone_dir, ignore_errors=True)
        return result

    lang = _detect_language(source_files)
    result["language"] = lang
    result["files_scanned"] = len(source_files)

    if verbose_log:
        verbose_log(f"    Language: {lang} | {len(source_files)} source files")

    # Run all static checks
    c1, d1 = _scan_secrets(root, source_files)
    c2, d2 = _scan_dependencies(root)
    c3, d3 = _scan_sensitive_logging(root, source_files)
    c4, d4 = _scan_auth(root, source_files)
    c5, d5 = _scan_injections(root, source_files)
    c6, d6 = _scan_debug(root, source_files)
    c7, d7 = _scan_crypto(root, source_files)
    c8, d8 = _scan_supply_chain(root)
    c9, d9 = _scan_cors_headers(root, source_files)
    c10, d10 = _scan_dependabot(root)

    result.update(
        {
            "secrets": c1,
            "dep_vulns": c2,
            "sensitive_logging": c3,
            "auth_issues": c4,
            "injection_risks": c5,
            "debug_exposure": c6,
            "deprecated_crypto": c7,
            "supply_chain": c8 + c10,
            "cors_headers": c9,
            "dependabot": c10,
        }
    )
    all_details = d1 + d2 + d3 + d4 + d5 + d6 + d7 + d8 + d9 + d10
    result["details"] = all_details
    result["total_findings"] = c1 + c2 + c3 + c4 + c5 + c6 + c7 + c8 + c9 + c10

    # LLM — single call per repo
    llm_result: dict = {}
    if not skip_llm and client:
        if verbose_log:
            verbose_log(f"    Running LLM security analysis for {repo} ...")
        code_samples = _smart_sample_security(
            root, source_files, all_details, sample_tokens
        )
        if code_samples:
            # Pass automated findings grouped by category for LLM context
            automated_grouped = {
                "secrets": d1,
                "dependencies": d2,
                "sensitive_logging": d3,
                "auth": d4,
                "injection": d5,
                "debug": d6,
                "crypto": d7,
                "supply_chain": d8,
                "cors_headers": d9,
            }
            llm_result = _llm_analyze(
                code_samples, automated_grouped, client, model, lang
            )
            result["llm_analysis"] = llm_result
            result["llm_summary"] = llm_result.get("summary", "")

            llm_findings = llm_result.get("findings", [])
            result["llm_findings"] = len(
                [f for f in llm_findings if isinstance(f, dict)]
            )
            result["total_findings"] += len(
                [
                    f
                    for f in llm_findings
                    if isinstance(f, dict) and not f.get("automated_confirmed")
                ]
            )

    # Build final deduplicated details
    final = _build_final_details(all_details, llm_result)
    critical, signals = _split_by_severity(final)

    result["final_details"] = final
    result["final_details_critical"] = critical
    result["final_details_signals"] = signals
    result["final_details_count"] = len(final)
    result["has_issues"] = result["total_findings"] > 0

    if owns_clone:
        shutil.rmtree(clone_dir, ignore_errors=True)
    return result
