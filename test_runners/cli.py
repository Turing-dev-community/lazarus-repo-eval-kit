#!/usr/bin/env python3
"""
CLI for F2P/P2P test analysis.

Usage:
    python -m test_runners.cli /path/to/repo --base abc123 --head def456

    # Pre-flight check only
    python -m test_runners.cli /path/to/repo --preflight

    # With PR info
    python -m test_runners.cli /path/to/repo --base abc123 --head def456 --pr 123 --title "Fix bug"
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from .analyzer import analyze_f2p_p2p, preflight_check
from .registry import list_available_runners, get_runner


def main():
    parser = argparse.ArgumentParser(
        description="Analyze repository for F2P/P2P test coverage"
    )
    parser.add_argument(
        "repo_path",
        help="Path to the repository"
    )
    parser.add_argument(
        "--base",
        help="Base commit SHA (before the PR)"
    )
    parser.add_argument(
        "--head",
        help="Head commit SHA (after the PR)"
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=0,
        help="PR number"
    )
    parser.add_argument(
        "--title",
        default="",
        help="PR title"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Test timeout in seconds (default: 600)"
    )
    parser.add_argument(
        "--language",
        help="Language hint for runner detection"
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Only run pre-flight check"
    )
    parser.add_argument(
        "--list-runners",
        action="store_true",
        help="List available test runners"
    )
    parser.add_argument(
        "--detect",
        action="store_true",
        help="Detect test runner for repository"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output"
    )

    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    # List runners
    if args.list_runners:
        runners = list_available_runners()
        if args.json:
            print(json.dumps(runners, indent=2))
        else:
            print("Available test runners:")
            for r in runners:
                print(f"  {r['name']:15} ({r['language']})")
        return 0

    # Validate repo path
    repo_path = Path(args.repo_path)
    if not repo_path.exists():
        print(f"Error: Repository path does not exist: {repo_path}", file=sys.stderr)
        return 1

    # Detect runner
    if args.detect:
        runner = get_runner(repo_path, args.language)
        if runner:
            result = {
                "name": runner.name,
                "language": runner.language,
                "runtime_available": runner.check_runtime()[0],
            }
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"Detected runner: {runner.name} ({runner.language})")
                runtime_ok, runtime_msg = runner.check_runtime()
                print(f"Runtime: {'✅' if runtime_ok else '❌'} {runtime_msg}")
        else:
            if args.json:
                print(json.dumps({"error": "No test runner detected"}, indent=2))
            else:
                print("No test runner detected for this repository")
            return 1
        return 0

    # Pre-flight check
    if args.preflight:
        result = preflight_check(str(repo_path), args.language)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            status = "✅ Ready" if result["can_run"] else "❌ Cannot run"
            print(f"Pre-flight check: {status}")

            if result["detected"]:
                print(f"\nDetected:")
                for k, v in result["detected"].items():
                    print(f"  {k}: {v}")

            if result["blockers"]:
                print(f"\nBlockers:")
                for b in result["blockers"]:
                    print(f"  ❌ [{b['code']}] {b['message']}")

            if result["warnings"]:
                print(f"\nWarnings:")
                for w in result["warnings"]:
                    print(f"  ⚠️  [{w['code']}] {w['message']}")

        return 0 if result["can_run"] else 1

    # Full analysis requires base and head commits
    if not args.base or not args.head:
        parser.error("--base and --head are required for F2P/P2P analysis")

    # Run F2P/P2P analysis
    result = analyze_f2p_p2p(
        repo_path=str(repo_path),
        base_sha=args.base,
        head_sha=args.head,
        pr_number=args.pr,
        pr_title=args.title,
        timeout=args.timeout,
        language_hint=args.language,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"F2P/P2P Analysis Result")
        print(f"{'='*60}")

        if args.pr:
            print(f"PR #{result.pr_number}: {result.pr_title}")
        print(f"Base: {result.base_sha[:8]}")
        print(f"Head: {result.head_sha[:8]}")
        print()

        if result.success:
            print(f"✅ Analysis completed successfully")
            print()
            print(f"F2P Tests (Fail→Pass): {len(result.f2p_tests)}")
            if result.f2p_tests:
                for t in result.f2p_tests[:10]:
                    print(f"  • {t}")
                if len(result.f2p_tests) > 10:
                    print(f"  ... and {len(result.f2p_tests) - 10} more")

            print()
            print(f"P2P Tests (Pass→Pass): {len(result.p2p_tests)}")
            if result.p2p_tests and args.verbose:
                for t in result.p2p_tests[:10]:
                    print(f"  • {t}")
                if len(result.p2p_tests) > 10:
                    print(f"  ... and {len(result.p2p_tests) - 10} more")

            print()
            print(f"Verdict: {result.verdict}")
            if result.has_valid_f2p and result.has_valid_p2p:
                print("✅ PR has valid F2P and P2P tests - ACCEPTED")
            elif not result.has_valid_f2p:
                print("❌ PR has no F2P tests - REJECTED")
            else:
                print("❌ PR has no P2P tests - REJECTED")
        else:
            print(f"❌ Analysis failed")
            print(f"Error: {result.error}")
            if result.error_code:
                print(f"Code: {result.error_code}")
            if result.suggested_action:
                print(f"Suggested action: {result.suggested_action}")

    return 0 if result.success and result.has_valid_f2p and result.has_valid_p2p else 1


if __name__ == "__main__":
    sys.exit(main())
