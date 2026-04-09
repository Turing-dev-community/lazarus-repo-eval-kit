# Repository Evaluator

A tool developped by Turing, that helps figure out the quality of repositories. It looks at both the repository structure and pull request history to give you insights into code quality, test coverage, and how the project is maintained.

## What It Does

This tool analyzes repositories from GitHub or Bitbucket and tells you:

- **What's in the repo**: File structure, lines of code, programming languages used
- **Testing setup**: Whether tests exist, what test frameworks are used, and coverage if available
- **CI/CD**: Whether there's automated testing and deployment
- **PR quality**: How many pull requests meet certain quality criteria, and why some don't
- **Project activity**: Recent commits and how issues are tracked


You can find more information about the Lazarus project in here https://lazarus.turing.com/ 

## Features

- Works with both GitHub and Bitbucket repositories
- Automatically clones repos if you don't have them locally
- Handles network issues with automatic retries
- Outputs results in human-readable format or JSON
- Supports filtering PRs by date and limiting how many to analyze

## What You Need

- Python 3.6 or newer
- The `requests`, `openai`, and `python-dotenv` libraries (install via `requirements.txt`)
- Git installed on your system
- An API token from GitHub or Bitbucket (helps avoid rate limits and access private repos)
- An **OpenAI API key** (required)

## Getting Started

### Installation

Install the required dependencies:

```bash
pip install -r requirements.txt
```

### Environment Setup

An OpenAI API key is required to run the script. Without it, the script will exit with an error.

1. Copy the example env file:
   ```bash
   cp .env.example .env
   ```
2. Edit `.env` and add your OpenAI API key:
   ```
   OPENAI_API_KEY=sk-your-actual-key-here
   ```
3. Get an API key from [OpenAI's platform](https://platform.openai.com/api-keys)

The cost for one repository run is typically $1–$5 based on past experience; very large repositories may cost more.

### Optional LLM tuning

These variables can be added to your `.env` file to control how the tool talks to OpenAI:

| Variable | Default | What it does |
|---|---|---|
| `LLM_CONCURRENCY` | `4` | How many LLM calls to make at the same time. Higher = faster, but more likely to hit rate limits. Lower = safer. |
| `LLM_MAX_RETRIES` | `8` | How many times a failed LLM call is retried before giving up. |
| `LLM_BACKOFF_BASE_DELAY` | `5.0` | How many seconds to wait before the first retry. The wait doubles each time: 5 s → 10 s → 20 s → … Raise this if you keep hitting rate limit errors. |

Example:
```
LLM_CONCURRENCY=2
LLM_MAX_RETRIES=10
LLM_BACKOFF_BASE_DELAY=5.0
```


### Basic Usage

The simplest way to use it:

```bash
# GitHub repository
python repo_evaluator.py owner/repo-name --token $GITHUB_TOKEN

# Bitbucket repository
python repo_evaluator.py bitbucket:owner/repo-name --token $BITBUCKET_TOKEN --platform bitbucket

# Or just use the full URL and it'll figure out the platform
python repo_evaluator.py https://bitbucket.org/owner/repo --token $BITBUCKET_TOKEN
```

### Examples

Here are some real examples:

```bash
# Check out a GitHub repo
python repo_evaluator.py microsoft/vscode --token $GITHUB_TOKEN

# Bitbucket repo with explicit platform
python repo_evaluator.py bitbucket:owner/repo --token $BITBUCKET_TOKEN --platform bitbucket

# Use a local copy instead of cloning
python repo_evaluator.py owner/repo --repo-path /path/to/local/repo --token $GITHUB_TOKEN
```


## Quality Checks

The evaluator can run three additional quality checks on repositories:

1. **Vibecode Check** — Detects signs of AI-generated "vibe coding" in commit history
2. **Security Check** — Looks for common security issues and anti-patterns
3. **Production Quality Check** — Assesses overall code quality and maintainability

Each check produces two outputs: a **critical** summary and detailed **signals**.

### Running Quality Checks

By default, quality checks run automatically with LLM analysis (requires `OPENAI_API_KEY` — see Environment Setup above):

```bash
python repo_evaluator.py owner/repo --token $GITHUB_TOKEN --json
```


## JavaScript/TypeScript test execution (Jest) configuration

By default, the evaluator tries to run Jest in a **structured JSON mode** for the most accurate
test counting and F2P/P2P computation. To improve out-of-the-box compatibility with repos that
rely on project-specific `npm test` behavior or environment setup, there are a few optional knobs.

### Recommended defaults (no CLI env required)

You can **check in** small configuration files in the repo under test:

- **`repo_evaluator_test_env.json`**: environment variables to inject during tests (JSON object)
- **`repo_evaluator_write_empty_json_files.txt`**: list of JSON files to create as `{}` if missing (newline-separated)

Example `repo_evaluator_test_env.json`:

```json
{
  "FIREBASE_ENV": "development",
  "DB_HOST": "localhost",
  "DB_PORT": "5432"
}
```

Example `repo_evaluator_write_empty_json_files.txt`:

```text
# Files tests expect to exist; evaluator will create them as {} if missing
firebase.secrets.json
```

Notes:
- The runner always injects **`CI=true`** as a safe baseline.
- These files should contain **test-safe dummy values**, not real secrets.



## Understanding the Output

### Human-Readable Output

When you run it normally, you'll see:
- Repository name and primary programming language
- File counts and lines of code breakdown
- Test coverage information (if available)
- CI/CD status
- Test frameworks detected
- Git activity stats
- PR analysis showing how many passed/failed the quality checks
- Breakdown of why PRs were rejected (if any)

### JSON Output

Add `--json` to get machine-readable output that's easy to parse or process:

```bash
python repo_evaluator.py owner/repo --token $TOKEN --json
```

## Why PRs Get Rejected

The tool checks PRs against several criteria. Here's what it looks for and why PRs might not make the cut:

- `fewer_than_min_test_files` - Not enough test files in the PR
- `more_than_max_non-test-files` - Too many non-test files changed
- `code_changes_not_sufficient` - Not enough actual code changes
- `issue_is_a_pr` - The linked "issue" is actually another PR
- `issue_is_not_closed` - The linked issue isn't closed
- `issue_word_count` - Issue description is too short or too long
- `content_not_in_english` - PR title/description doesn't appear to be in English
- `rust_embedded_tests` - Rust source files contain embedded tests (not allowed)
- `merge_date` - PR was merged before your specified start date
- `full_patch_retrieval` - Couldn't download the full diff

## Getting API Tokens

### GitHub

1. Go to your GitHub Settings → Developer settings → Personal access tokens
2. Click "Generate new token"
3. Give it the `repo` scope
4. Copy the token and use it with `--token`

### Bitbucket

1. Go to your repository's Settings → Access tokens
2. Create a new access token
3. Make sure it has repository read permissions
4. Use the token with `--token`

**Why you need tokens:** They let you access private repos and give you higher rate limits.

## Common Issues

### Network Problems

If you see DNS or connection errors, the tool will automatically retry a few times. If it keeps failing:
- Check your internet connection
- Make sure the repository URL is correct
- Wait a bit and try again

### Rate Limits

If you hit rate limits:
- Make sure you're using `--token` with a valid API token
- Wait a few minutes before trying again



## Limitations

- Needs internet access to fetch data from GitHub/Bitbucket APIs
- Can take a while for repos with lots of PRs and large codebase
- Some detection depends on common project structures and naming conventions
- Bitbucket support is good but might have some differences compared to GitHub


## How to submit your repository results:
Once you are done and have the output csv, visit https://lazarus.turing.com/ and send us your csv along some information asked.