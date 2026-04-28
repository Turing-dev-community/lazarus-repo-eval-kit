#!/usr/bin/env python3
"""Build fixture git repos and platform PR cassettes for Stage 0 characterization.

Run once from the project root:
    python tests/fixtures/build_fixture_repos.py

Outputs:
    tests/fixtures/repos/{tiny_python,no_prs,multi_lang_ci}/  — git repos
    tests/fixtures/platform/{tiny_python,no_prs,multi_lang_ci}_prs.json — cassettes

The cassettes embed the actual commit SHAs produced here so that
PRAnalyzer._get_patch_from_git() can retrieve diffs without network access.

Git env vars are fixed so the SHA is deterministic on every fresh build.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent
REPOS_DIR = FIXTURES_DIR / "repos"
PLATFORM_DIR = FIXTURES_DIR / "platform"

_BASE_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fixture Author",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "Fixture Committer",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
    "PATH": os.environ["PATH"],
}


def _git(args: list[str], cwd: Path, extra_env: dict | None = None) -> str:
    env = {**_BASE_GIT_ENV, **(extra_env or {})}
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd} failed:\n{r.stderr}")
    return r.stdout.strip()


def _init(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "fixture@example.com"], path)
    _git(["config", "user.name", "Fixture Author"], path)


def _commit(path: Path, message: str, date: str) -> str:
    _git(["add", "-A"], path)
    _git(
        ["commit", "-m", message],
        path,
        extra_env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
    )
    return _git(["rev-parse", "HEAD"], path)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ── tiny_python ───────────────────────────────────────────────────────────────


def _build_tiny_python() -> dict[str, str]:
    repo = REPOS_DIR / "tiny_python"
    _init(repo)

    _write(repo / "README.md", "# tiny_python\nA tiny Python project for testing.\n")
    _write(repo / "src" / "__init__.py", "")
    _write(
        repo / "src" / "main.py",
        "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n",
    )
    _write(
        repo / "src" / "utils.py",
        "def double(x):\n    return x * 2\n\ndef triple(x):\n    return x * 3\n",
    )
    _write(repo / "tests" / "__init__.py", "")
    _write(
        repo / "tests" / "test_main.py",
        "from src.main import add, subtract\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_subtract():\n    assert subtract(5, 3) == 2\n",
    )
    _write(
        repo / "tests" / "test_utils.py",
        "from src.utils import double, triple\n\n"
        "def test_double():\n    assert double(3) == 6\n\n"
        "def test_triple():\n    assert triple(4) == 12\n",
    )
    base_sha = _commit(repo, "Initial commit", "2024-01-01T00:00:00+00:00")

    _git(["checkout", "-b", "feature/add-helpers"], repo)
    _write(
        repo / "src" / "helpers.py",
        "def clamp(value, lo, hi):\n    return max(lo, min(hi, value))\n\n"
        "def square(x):\n    return x * x\n\n"
        "def cube(x):\n    return x * x * x\n",
    )
    _write(
        repo / "tests" / "test_helpers.py",
        "from src.helpers import clamp, square, cube\n\n"
        "def test_clamp():\n    assert clamp(5, 0, 10) == 5\n    assert clamp(-1, 0, 10) == 0\n\n"
        "def test_square():\n    assert square(4) == 16\n\n"
        "def test_cube():\n    assert cube(3) == 27\n",
    )
    _write(
        repo / "src" / "main.py",
        "from src.helpers import square\n\n"
        "def add(a, b):\n    return a + b\n\n"
        "def subtract(a, b):\n    return a - b\n\n"
        "def multiply(a, b):\n    return a * b\n",
    )
    _write(
        repo / "src" / "utils.py",
        "from src.helpers import cube\n\n"
        "def double(x):\n    return x * 2\n\n"
        "def triple(x):\n    return x * 3\n\n"
        "def quadruple(x):\n    return x * 4\n",
    )
    _write(
        repo / "tests" / "test_main.py",
        "from src.main import add, subtract, multiply\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_subtract():\n    assert subtract(5, 3) == 2\n\n"
        "def test_multiply():\n    assert multiply(3, 4) == 12\n",
    )
    _write(
        repo / "tests" / "test_utils.py",
        "from src.utils import double, triple, quadruple\n\n"
        "def test_double():\n    assert double(3) == 6\n\n"
        "def test_triple():\n    assert triple(4) == 12\n\n"
        "def test_quadruple():\n    assert quadruple(5) == 20\n",
    )
    head_sha = _commit(
        repo, "Add helpers module and extend tests", "2024-02-01T00:00:00+00:00"
    )

    _git(["checkout", "main"], repo)
    _git(
        ["merge", "--no-ff", "feature/add-helpers", "-m", "Merge feature/add-helpers"],
        repo,
        extra_env={
            "GIT_AUTHOR_DATE": "2024-02-02T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2024-02-02T00:00:00+00:00",
        },
    )
    return {"base_sha": base_sha, "head_sha": head_sha}


# ── no_prs ────────────────────────────────────────────────────────────────────


def _build_no_prs() -> dict:
    repo = REPOS_DIR / "no_prs"
    _init(repo)

    _write(repo / "README.md", "# no_prs\nA minimal single-commit project.\n")
    _write(repo / "src" / "__init__.py", "")
    _write(
        repo / "src" / "app.py",
        "def greet(name):\n    return f'Hello, {name}!'\n",
    )
    _commit(repo, "Initial commit", "2024-01-01T00:00:00+00:00")
    return {}


# ── multi_lang_ci ─────────────────────────────────────────────────────────────


def _build_multi_lang_ci() -> dict[str, str]:
    repo = REPOS_DIR / "multi_lang_ci"
    _init(repo)

    _write(
        repo / "README.md",
        "# multi_lang_ci\nA multi-language project with CI.\n"
        "## Usage\nRun `pytest` to execute the Python tests.\n",
    )
    _write(repo / "src" / "__init__.py", "")
    _write(
        repo / "src" / "calculator.py",
        "def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n",
    )
    _write(
        repo / "src" / "formatter.py",
        "def format_number(n, decimals=2):\n    return f'{n:.{decimals}f}'\n",
    )
    _write(
        repo / "lib" / "index.js",
        "function greet(n) { return `Hello, ${n}!`; }\nmodule.exports = { greet };\n",
    )
    _write(
        repo / "lib" / "utils.js",
        "function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi); }\nmodule.exports = { clamp };\n",
    )
    _write(repo / "tests" / "__init__.py", "")
    _write(
        repo / "tests" / "test_calculator.py",
        "from src.calculator import add, multiply\n\n"
        "def test_add():\n    assert add(2, 3) == 5\n\n"
        "def test_multiply():\n    assert multiply(3, 4) == 12\n",
    )
    _write(
        repo / "tests" / "test_formatter.py",
        "from src.formatter import format_number\n\n"
        "def test_format_number():\n    assert format_number(3.14159, 2) == '3.14'\n",
    )
    _write(repo / "pytest.ini", "[pytest]\ntestpaths = tests\n")
    _write(
        repo / ".github" / "workflows" / "ci.yml",
        "name: CI\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v3\n"
        "      - name: Run tests\n        run: pytest\n",
    )
    _write(
        repo / "coverage.xml",
        '<?xml version="1.0" ?>\n'
        '<coverage version="7.0" timestamp="1234567890" lines-valid="100" '
        'lines-covered="75" line-rate="0.75" branches-covered="0" '
        'branches-valid="0" branch-rate="0" complexity="0">\n'
        "    <packages>\n"
        '        <package name="src" line-rate="0.75" branch-rate="0" complexity="0">\n'
        "            <classes>\n"
        '                <class name="calculator.py" filename="src/calculator.py" '
        'line-rate="1.0" branch-rate="0" complexity="0">\n'
        "                    <lines>\n"
        '                        <line number="1" hits="1"/>\n'
        '                        <line number="2" hits="1"/>\n'
        "                    </lines>\n"
        "                </class>\n"
        "            </classes>\n"
        "        </package>\n"
        "    </packages>\n"
        "</coverage>\n",
    )
    base_sha = _commit(repo, "Initial commit", "2024-01-01T00:00:00+00:00")

    _git(["checkout", "-b", "feature/extend-calculator"], repo)
    _write(
        repo / "src" / "calculator.py",
        "def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n\n"
        "def divide(a, b):\n    if b == 0:\n        raise ValueError('Cannot divide by zero')\n    return a / b\n\n"
        "def subtract(a, b):\n    return a - b\n",
    )
    _write(
        repo / "src" / "statistics.py",
        "def mean(values):\n    return sum(values) / len(values)\n\n"
        "def median(values):\n    s = sorted(values)\n    n = len(s)\n    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2\n",
    )
    _write(
        repo / "src" / "constants.py",
        "MAX_VALUE = 1000\nMIN_VALUE = -1000\nDEFAULT_PRECISION = 2\n",
    )
    _write(
        repo / "tests" / "test_calculator.py",
        "from src.calculator import add, multiply, divide, subtract\n\n"
        "def test_add():\n    assert add(2, 3) == 5\n\n"
        "def test_multiply():\n    assert multiply(3, 4) == 12\n\n"
        "def test_divide():\n    assert divide(10, 2) == 5.0\n\n"
        "def test_subtract():\n    assert subtract(5, 3) == 2\n",
    )
    _write(
        repo / "tests" / "test_statistics.py",
        "from src.statistics import mean, median\n\n"
        "def test_mean():\n    assert mean([1, 2, 3, 4, 5]) == 3.0\n\n"
        "def test_median_odd():\n    assert median([1, 2, 3]) == 2\n",
    )
    _write(
        repo / "lib" / "math_utils.js",
        "function sum(arr) { return arr.reduce((a, b) => a + b, 0); }\nmodule.exports = { sum };\n",
    )
    head_sha = _commit(
        repo,
        "Extend calculator, add statistics and constants modules",
        "2024-03-01T00:00:00+00:00",
    )

    _git(["checkout", "main"], repo)
    _git(
        [
            "merge",
            "--no-ff",
            "feature/extend-calculator",
            "-m",
            "Merge feature/extend-calculator",
        ],
        repo,
        extra_env={
            "GIT_AUTHOR_DATE": "2024-03-02T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2024-03-02T00:00:00+00:00",
        },
    )
    return {"base_sha": base_sha, "head_sha": head_sha}


# ── cassette helpers ──────────────────────────────────────────────────────────


def _pr_node(
    *,
    number: int,
    title: str,
    body: str,
    author_login: str,
    author_typename: str,
    base_sha: str,
    head_sha: str,
    created_at: str,
    merged_at: str,
    files: list[dict],
    closing_issues: list[dict] | None = None,
    labels: list[dict] | None = None,
) -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "url": f"https://github.com/fake/repo/pull/{number}",
        "baseRefOid": base_sha,
        "headRefOid": head_sha,
        "baseRefName": "main",
        "headRefName": f"feature/pr-{number}",
        "createdAt": created_at,
        "mergedAt": merged_at,
        "author": {"__typename": author_typename, "login": author_login},
        "files": {"nodes": files},
        "closingIssuesReferences": {"nodes": closing_issues or []},
        "labels": {"nodes": labels or []},
    }


def _file_node(
    path: str, additions: int, deletions: int, change_type: str = "MODIFIED"
) -> dict:
    return {
        "path": path,
        "additions": additions,
        "deletions": deletions,
        "changeType": change_type,
    }


def _github_response(pr_nodes: list[dict], primary_language: str = "Python") -> dict:
    return {
        "data": {
            "repository": {
                "primaryLanguage": {"name": primary_language},
                "pullRequests": {
                    "pageInfo": {"endCursor": None, "hasNextPage": False},
                    "nodes": pr_nodes,
                },
            }
        }
    }


def _write_cassette(name: str, data: dict) -> None:
    PLATFORM_DIR.mkdir(parents=True, exist_ok=True)
    path = PLATFORM_DIR / f"{name}_prs.json"
    path.write_text(json.dumps(data, indent=2))
    print(f"  wrote {path.relative_to(FIXTURES_DIR.parent.parent)}")


# ── cassette: no_prs ─────────────────────────────────────────────────────────


def _write_no_prs_cassette() -> None:
    _write_cassette("no_prs", _github_response([], "Python"))


# ── cassette: tiny_python_prs ─────────────────────────────────────────────────


def _write_tiny_python_cassette(shas: dict[str, str]) -> None:
    base_sha = shas["base_sha"]
    head_sha = shas["head_sha"]

    # PR #1: bot — rejected at bot_pr check
    pr_bot = _pr_node(
        number=1,
        title="chore: automated dependency update",
        body="Automated bump of dependencies.",
        author_login="dependabot[bot]",
        author_typename="Bot",
        base_sha="0000000000000000000000000000000000000001",
        head_sha="0000000000000000000000000000000000000002",
        created_at="2024-01-10T10:00:00Z",
        merged_at="2024-01-10T11:00:00Z",
        files=[_file_node("requirements.txt", 2, 2)],
    )

    # PR #2: too many changed files — rejected at too_many_changed_files
    pr_too_many_files = _pr_node(
        number=2,
        title="refactor: rename all variables",
        body="Massive rename across the entire codebase.",
        author_login="human-dev",
        author_typename="User",
        base_sha="0000000000000000000000000000000000000003",
        head_sha="0000000000000000000000000000000000000004",
        created_at="2024-01-15T10:00:00Z",
        merged_at="2024-01-15T12:00:00Z",
        files=[
            _file_node(f"src/module_{i}.py", 5, 3)
            for i in range(55)  # 55 non-test files > MAX_CHANGED_FILES=50
        ],
    )

    # PR #3: accepted — uses real SHAs, valid linked issue, sufficient test + source files
    valid_issue = {
        "__typename": "Issue",
        "number": 42,
        "state": "CLOSED",
        "body": (
            "We need a helpers module to provide shared utility functions like clamp, "
            "square and cube. These are used in both the main module and utils module "
            "but are currently duplicated. This PR adds the helpers module and "
            "updates the existing tests to use the new helpers."
        ),
    }
    pr_accepted = _pr_node(
        number=3,
        title="feat: add helpers module",
        body="Adds clamp, square, cube helpers and extends existing tests.",
        author_login="alice",
        author_typename="User",
        base_sha=base_sha,
        head_sha=head_sha,
        created_at="2024-02-01T09:00:00Z",
        merged_at="2024-02-02T09:00:00Z",
        files=[
            _file_node("src/helpers.py", 8, 0, "ADDED"),
            _file_node("src/main.py", 4, 1, "MODIFIED"),
            _file_node("src/utils.py", 4, 1, "MODIFIED"),
            _file_node("tests/test_helpers.py", 10, 0, "ADDED"),
            _file_node("tests/test_main.py", 4, 1, "MODIFIED"),
            _file_node("tests/test_utils.py", 4, 1, "MODIFIED"),
        ],
        closing_issues=[valid_issue],
    )

    _write_cassette(
        "tiny_python", _github_response([pr_bot, pr_too_many_files, pr_accepted])
    )


# ── cassette: multi_lang_prs ─────────────────────────────────────────────────


def _write_multi_lang_cassette(shas: dict[str, str]) -> None:
    base_sha = shas["base_sha"]
    head_sha = shas["head_sha"]

    # PR #1: bot
    pr_bot = _pr_node(
        number=1,
        title="chore: automated CI update",
        body="",
        author_login="github-actions[bot]",
        author_typename="Bot",
        base_sha="0000000000000000000000000000000000000011",
        head_sha="0000000000000000000000000000000000000012",
        created_at="2024-01-05T10:00:00Z",
        merged_at="2024-01-05T11:00:00Z",
        files=[_file_node(".github/workflows/ci.yml", 1, 1)],
    )

    # PR #2: fewer than min test files (0 test files, >5 total)
    pr_no_tests = _pr_node(
        number=2,
        title="refactor: reorganize source files",
        body="Moving source files around, no test changes.",
        author_login="bob",
        author_typename="User",
        base_sha="0000000000000000000000000000000000000013",
        head_sha="0000000000000000000000000000000000000014",
        created_at="2024-01-20T10:00:00Z",
        merged_at="2024-01-20T12:00:00Z",
        files=[_file_node(f"src/module_{i}.py", 10, 0, "ADDED") for i in range(6)],
    )

    # PR #3: difficulty_not_hard (total files <= 5)
    pr_too_easy = _pr_node(
        number=3,
        title="fix: typo in calculator",
        body="Fix a small typo.",
        author_login="carol",
        author_typename="User",
        base_sha="0000000000000000000000000000000000000015",
        head_sha="0000000000000000000000000000000000000016",
        created_at="2024-02-10T10:00:00Z",
        merged_at="2024-02-10T11:00:00Z",
        files=[
            _file_node("src/calculator.py", 1, 1),
            _file_node("tests/test_calculator.py", 1, 0),
        ],
    )

    # PR #4: no linked issue body (issue_word_count rejection via linked issue with short body)
    pr_bad_issue = _pr_node(
        number=4,
        title="feat: add logging module",
        body="Closes #99",
        author_login="dave",
        author_typename="User",
        base_sha="0000000000000000000000000000000000000017",
        head_sha="0000000000000000000000000000000000000018",
        created_at="2024-02-15T10:00:00Z",
        merged_at="2024-02-15T12:00:00Z",
        files=[_file_node(f"src/logging_{i}.py", 10, 0, "ADDED") for i in range(4)]
        + [_file_node(f"tests/test_logging_{i}.py", 8, 0, "ADDED") for i in range(3)],
        closing_issues=[
            {
                "__typename": "Issue",
                "number": 99,
                "state": "CLOSED",
                "body": "Add logging.",  # too few words (< MIN_ISSUE_WORDS=10)
            }
        ],
    )

    # PR #5: accepted — uses real SHAs
    valid_issue = {
        "__typename": "Issue",
        "number": 101,
        "state": "CLOSED",
        "body": (
            "The calculator module needs to support division and subtraction operations. "
            "We should also add a statistics module with mean and median functions, "
            "and a constants module for shared values. "
            "All new functions must have corresponding tests."
        ),
    }
    pr_accepted = _pr_node(
        number=5,
        title="feat: extend calculator with division and add statistics module",
        body="Adds divide/subtract to calculator, new statistics.py and constants.py.",
        author_login="eve",
        author_typename="User",
        base_sha=base_sha,
        head_sha=head_sha,
        created_at="2024-03-01T09:00:00Z",
        merged_at="2024-03-02T09:00:00Z",
        files=[
            _file_node("src/calculator.py", 8, 0, "MODIFIED"),
            _file_node("src/statistics.py", 6, 0, "ADDED"),
            _file_node("src/constants.py", 3, 0, "ADDED"),
            _file_node("lib/math_utils.js", 3, 0, "ADDED"),
            _file_node("tests/test_calculator.py", 8, 2, "MODIFIED"),
            _file_node("tests/test_statistics.py", 7, 0, "ADDED"),
        ],
        closing_issues=[valid_issue],
    )

    _write_cassette(
        "multi_lang",
        _github_response(
            [pr_bot, pr_no_tests, pr_too_easy, pr_bad_issue, pr_accepted], "Python"
        ),
    )


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    PLATFORM_DIR.mkdir(parents=True, exist_ok=True)

    print("Building tiny_python...")
    tiny_python_shas = _build_tiny_python()
    print(f"  base_sha={tiny_python_shas['base_sha']}")
    print(f"  head_sha={tiny_python_shas['head_sha']}")

    print("Building no_prs...")
    _build_no_prs()

    print("Building multi_lang_ci...")
    multi_lang_shas = _build_multi_lang_ci()
    print(f"  base_sha={multi_lang_shas['base_sha']}")
    print(f"  head_sha={multi_lang_shas['head_sha']}")

    print("Writing cassettes...")
    _write_tiny_python_cassette(tiny_python_shas)
    _write_no_prs_cassette()
    _write_multi_lang_cassette(multi_lang_shas)

    print("Done.")


if __name__ == "__main__":
    main()
