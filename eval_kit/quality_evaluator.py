import json
import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from eval_kit.llm_client import call_llm

logger = logging.getLogger(__name__)


@dataclass
class QualityScores:
    gold_patch_clarity: int
    patch_to_issue_alignment: int
    test_clarity: int
    fn_score: int
    fp_score: int
    fn_label: str
    fp_label: str
    fn_rationale: str
    fp_rationale: str
    clarity_rationale: str
    alignment_rationale: str
    test_rationale: str
    # New rubrics
    issue_clarity: int
    issue_clarity_label: str
    issue_clarity_rationale: str
    test_to_issue_alignment: int
    test_to_issue_alignment_label: str
    test_to_issue_alignment_rationale: str
    task_difficulty: int
    task_difficulty_label: str
    task_difficulty_rationale: str
    # Optional fields
    inferred_problem_statement: Optional[str] = None
    inference_confidence: Optional[str] = None

    def passes_threshold(self, max_score: int = 1) -> bool:
        return (
            self.gold_patch_clarity <= max_score
            and self.patch_to_issue_alignment <= max_score
            and self.test_clarity <= max_score
            and self.fn_score <= max_score
            and self.fp_score <= max_score
            and self.issue_clarity <= max_score
            and self.test_to_issue_alignment <= max_score
        )

    def total_score(self) -> int:
        return (
            self.gold_patch_clarity
            + self.patch_to_issue_alignment
            + self.test_clarity
            + self.fn_score
            + self.fp_score
            + self.issue_clarity
            + self.test_to_issue_alignment
        )

    def get_recommendation(self) -> str:
        total = self.total_score()
        if total <= 5:
            return "strong_candidate"
        elif total <= 10:
            return "borderline"
        return "reject"

    def to_summary_dict(self) -> dict:
        return {
            "recommendation": self.get_recommendation(),
            "total_score": self.total_score(),
            "max_possible_score": 21,
            "dimensions": {
                "issue_clarity": {
                    "score": self.issue_clarity,
                    "max": 3,
                    "label": self.issue_clarity_label,
                    "rationale": self.issue_clarity_rationale,
                },
                "test_to_issue_alignment": {
                    "score": self.test_to_issue_alignment,
                    "max": 3,
                    "label": self.test_to_issue_alignment_label,
                    "rationale": self.test_to_issue_alignment_rationale,
                },
                "gold_patch_clarity": {
                    "score": self.gold_patch_clarity,
                    "max": 3,
                    "rationale": self.clarity_rationale,
                },
                "patch_to_issue_alignment": {
                    "score": self.patch_to_issue_alignment,
                    "max": 3,
                    "rationale": self.alignment_rationale,
                },
                "test_clarity": {
                    "score": self.test_clarity,
                    "max": 3,
                    "rationale": self.test_rationale,
                },
                "false_negative": {
                    "score": self.fn_score,
                    "max": 3,
                    "label": self.fn_label,
                    "rationale": self.fn_rationale,
                },
                "false_positive": {
                    "score": self.fp_score,
                    "max": 3,
                    "label": self.fp_label,
                    "rationale": self.fp_rationale,
                },
                "task_difficulty": {
                    "score": self.task_difficulty,
                    "max": 3,
                    "label": self.task_difficulty_label,
                    "rationale": self.task_difficulty_rationale,
                },
            },
            "inferred_problem": self.inferred_problem_statement,
            "inference_confidence": self.inference_confidence,
        }

    def to_trimmed_rubrics_dict(self) -> dict:
        """Trimmed benchmark rubrics: score + reasoning (labels where applicable)."""
        return {
            "issue_clarity": {
                "score": self.issue_clarity,
                "reasoning": self.issue_clarity_rationale,
            },
            "gold_patch_clarity": {
                "score": self.gold_patch_clarity,
                "reasoning": self.clarity_rationale,
            },
            "test_clarity": {
                "score": self.test_clarity,
                "reasoning": self.test_rationale,
            },
            "gold_patch_to_issue_alignment": {
                "score": self.patch_to_issue_alignment,
                "reasoning": self.alignment_rationale,
            },
            "test_to_issue_alignment": {
                "score": self.test_to_issue_alignment,
                "reasoning": self.test_to_issue_alignment_rationale,
                "label": self.test_to_issue_alignment_label,
            },
            "false_negatives": {
                "score": self.fn_score,
                "reasoning": self.fn_rationale,
                "label": self.fn_label,
            },
            "false_positives": {
                "score": self.fp_score,
                "reasoning": self.fp_rationale,
                "label": self.fp_label,
            },
        }


UNIFIED_EVALUATION_PROMPT = """You are an expert AI judge specializing in the evaluation of software engineering benchmarks.

Your task is to assess the quality of a benchmark instance based on the following criteria.
Analyze the following and score on ALL rubrics. Lower scores are better.

============================================================
PROBLEM STATEMENT
============================================================
{problem_statement}

============================================================
HINTS (if any)
============================================================
{hints}

============================================================
SOURCE CODE CHANGES (gold_src.diff)
============================================================
{src_diff}

============================================================
TEST CHANGES (gold_test.diff)
============================================================
{test_diff}

============================================================
EVALUATION CRITERIA
============================================================

**1. ISSUE CLARITY** (0-3): How clear is the problem statement?
- 0 = FULLY CLEAR: Well-specified; success criteria obvious (includes what must change and what counts as success).
- 1 = MOSTLY CLEAR: Some gaps, but a sensible solution is still inferable (you can describe a concrete fix with minor guesswork).
- 2 = VAGUE: Success criteria missing or incomplete (you cannot tell what exactly needs to be done, only the general area/problem).
- 3 = EXTREMELY UNCLEAR: Cannot be solved without major external information.

**Strict rules for Issue Clarity:**
- If issue body does NOT explicitly describe expected behavior or acceptance criteria → assign ≥2 (never 0/1).
- If statement only gives motivation, screenshots, or links without precise requirements → assign 2.
- If statement is a crash log/stack trace without context, but fix direction is guessable → assign 1.
- If nothing actionable is provided. → assign 3.

**Notes on the definition of actionable:**
- It has to be explicit not implicit.
- It has to require non-test changes requests.
- If the problem statement only requests test changes, then this is not actionable.

**2. TEST-TO-ISSUE ALIGNMENT** (0-3): How well do tests validate the stated issue?
- 0 = PERFECT: Tests directly check the required behavior in the issue, and nothing unrelated.
- 1 = GOOD: Tests cover majority of correct solutions, but some edge cases or alternative implementations may not be caught.
- 2 = PARTIAL: Tests validate *some aspect* of the issue, but add extra constraints or miss some reasonable correct solutions.
- 3 = POOR: Tests fail to check core requirement of the issue, or check something entirely unrelated.

**Strict rules for Test-to-Issue Alignment:**
- If tests do NOT exercise the main behavior or bug described → assign 3.
- If tests over-specify beyond issue scope (e.g., exact error message wording not specified, custom attributes not mentioned) → assign 2.
- If tests pass for solutions that don't actually fix the issue → lean toward 3.
- Never assign 0 unless tests directly validate the issue's stated fix.

**3. GOLD PATCH CLARITY** (0-3): How readable is the code change?
- 0 = CLEAR: Clean, focused, easy to follow. Minor quirks okay.
- 1 = UNDERSTANDABLE: Takes effort but you can figure it out. Some inconsistencies.
- 2 = VAGUE: Hard to follow, poor practices, lacks structure.
- 3 = UNCLEAR: Unreadable nightmare, no structure, no clarity.

**4. PATCH-TO-ISSUE ALIGNMENT** (0-3): Does the patch match the problem?
- 0 = ATOMIC: Fully and exactly addresses the problem—nothing more, nothing less.
- 1 = OVER-SCOPED: Solves problem but adds non-essential/unrelated changes.
- 2 = UNDER-SCOPED: Only partially addresses the problem, missing key elements.
- 3 = NON-ATOMIC: Doesn't address problem, or bundles unrelated changes (dev merge, multi-fix).

**5. TEST CLARITY** (0-3): Are the tests understandable?
- 0 = CLEAR: Focused, well-named, covers intent with no readability issues.
- 1 = UNDERSTANDABLE: Takes effort, weak naming, tests multiple things.
- 2 = VAGUE: Confusing setup, poor naming, unclear what's being tested.
- 3 = UNCLEAR: Unreadable noise, can't tell what's being tested or why.

**6. FALSE NEGATIVE (FN)** (0-3): Will tests reject valid alternative solutions?
- 0 = FULLY GENERALIZED: Accepts all correct implementations.
- 1 = GENERALIZED: Accepts most, rare edge case rejections.
- 2 = RESTRICTIVE: Rejects many valid alternatives.
- 3 = OVERLY RESTRICTIVE: Only accepts near-identical solutions.

**7. FALSE POSITIVE (FP)** (0-3): Will tests catch incorrect solutions?
- 0 = FULLY STRICT: Catches all bugs including edge cases.
- 1 = STRICT: Catches most bugs, misses some subtle ones.
- 2 = SUPERFICIAL: Only catches obvious bugs.
- 3 = OVERLY PERMISSIVE: Accepts clearly broken solutions.

**8. TASK DIFFICULTY** (0-3): How hard is this to fix?
- 0 = TRIVIAL: <15 min fix (e.g., adding assertions to a function).
- 1 = EASY: 15 min - 1 hour (small change requiring some thought).
- 2 = MEDIUM: 1-4 hours (substantial rewrite or multi-file edit).
- 3 = HARD: >4 hours (esoteric issue, >100 lines of code changes).

============================================================
RESPONSE FORMAT
============================================================
Respond with ONLY this JSON (no markdown, no extra text):

{{
  "issue_clarity": <0-3>,
  "issue_clarity_label": "<Fully Clear|Mostly Clear|Vague|Extremely Unclear>",
  "issue_clarity_rationale": "<brief explanation>",
  "test_to_issue_alignment": <0-3>,
  "test_to_issue_alignment_label": "<Perfect|Good|Partial|Poor>",
  "test_to_issue_alignment_rationale": "<brief explanation>",
  "gold_patch_clarity": <0-3>,
  "patch_to_issue_alignment": <0-3>,
  "test_clarity": <0-3>,
  "fn_score": <0-3>,
  "fp_score": <0-3>,
  "fn_label": "<Fully Generalized|Generalized|Restrictive|Overly Restrictive>",
  "fp_label": "<Fully Strict|Strict|Superficial|Overly Permissive>",
  "clarity_rationale": "<brief explanation>",
  "alignment_rationale": "<brief explanation>",
  "test_rationale": "<brief explanation>",
  "fn_rationale": "<brief explanation>",
  "fp_rationale": "<brief explanation>",
  "task_difficulty": <0-3>,
  "task_difficulty_label": "<Trivial|Easy|Medium|Hard>",
  "task_difficulty_rationale": "<brief explanation>"
}}
"""


# Used when there are no test changes (or no test files). We only ask the model
# to score NON-test rubrics, and we force all test-related rubrics to 3 in code.
NO_TESTS_EVALUATION_PROMPT = """You are an expert AI judge specializing in the evaluation of software engineering benchmarks.

Your task is to assess the quality of a benchmark instance based on the following criteria.
Analyze the following and score ONLY the requested rubrics. Lower scores are better.

============================================================
PROBLEM STATEMENT
============================================================
{problem_statement}

============================================================
HINTS (if any)
============================================================
{hints}

============================================================
SOURCE CODE CHANGES (gold_src.diff)
============================================================
{src_diff}

============================================================
EVALUATION CRITERIA
============================================================

**1. ISSUE CLARITY** (0-3): How clear is the problem statement?
- 0 = FULLY CLEAR: Well-specified; success criteria obvious (includes what must change and what counts as success).
- 1 = MOSTLY CLEAR: Some gaps, but a sensible solution is still inferable (you can describe a concrete fix with minor guesswork).
- 2 = VAGUE: Success criteria missing or incomplete (you cannot tell what exactly needs to be done, only the general area/problem).
- 3 = EXTREMELY UNCLEAR: Cannot be solved without major external information.

**Strict rules for Issue Clarity:**
- If issue body does NOT explicitly describe expected behavior or acceptance criteria → assign ≥2 (never 0/1).
- If statement only gives motivation, screenshots, or links without precise requirements → assign 2.
- If statement is a crash log/stack trace without context, but fix direction is guessable → assign 1.
- If nothing actionable is provided → assign 3.

**2. GOLD PATCH CLARITY** (0-3): How readable is the code change?
- 0 = CLEAR: Clean, focused, easy to follow. Minor quirks okay.
- 1 = UNDERSTANDABLE: Takes effort but you can figure it out. Some inconsistencies.
- 2 = VAGUE: Hard to follow, poor practices, lacks structure.
- 3 = UNCLEAR: Unreadable nightmare, no structure, no clarity.

**3. PATCH-TO-ISSUE ALIGNMENT** (0-3): Does the patch match the problem?
- 0 = ATOMIC: Fully and exactly addresses the problem—nothing more, nothing less.
- 1 = OVER-SCOPED: Solves problem but adds non-essential/unrelated changes.
- 2 = UNDER-SCOPED: Only partially addresses the problem, missing key elements.
- 3 = NON-ATOMIC: Doesn't address problem, or bundles unrelated changes (dev merge, multi-fix).

**4. TASK DIFFICULTY** (0-3): How hard is this to fix?
- 0 = TRIVIAL: <15 min fix (e.g., adding assertions to a function).
- 1 = EASY: 15 min - 1 hour (small change requiring some thought).
- 2 = MEDIUM: 1-4 hours (substantial rewrite or multi-file edit).
- 3 = HARD: >4 hours (esoteric issue, >100 lines of code changes).

============================================================
RESPONSE FORMAT
============================================================
Respond with ONLY this JSON (no markdown, no extra text):

{{
  "issue_clarity": <0-3>,
  "issue_clarity_label": "<Fully Clear|Mostly Clear|Vague|Extremely Unclear>",
  "issue_clarity_rationale": "<brief explanation>",
  "gold_patch_clarity": <0-3>,
  "patch_to_issue_alignment": <0-3>,
  "clarity_rationale": "<brief explanation>",
  "alignment_rationale": "<brief explanation>",
  "task_difficulty": <0-3>,
  "task_difficulty_label": "<Trivial|Easy|Medium|Hard>",
  "task_difficulty_rationale": "<brief explanation>"
}}
"""


F2P_P2P_CHECK_PROMPT = """Analyze if the test changes include both F2P (Fail-to-Pass) and P2P (Pass-to-Pass) tests.

**F2P tests**: NEW tests that would FAIL before the fix and PASS after. They directly test the bug/feature being fixed.
**P2P tests**: EXISTING tests that PASS both before and after the fix. They ensure no regressions. These are tests that existed before and still pass, or modifications to existing tests.

## Source Code Changes:
{src_diff}

## Test Changes:
{test_diff}

IMPORTANT:
- has_f2p MUST be false if estimated_f2p_tests is 0
- has_p2p MUST be false if estimated_p2p_tests is 0
- Be strict: if no clear evidence, set to false

Respond with ONLY this JSON:
{{
  "has_f2p": <true ONLY if estimated_f2p_tests > 0>,
  "has_p2p": <true ONLY if estimated_p2p_tests > 0>,
  "estimated_f2p_tests": <number of F2P tests, 0 if none>,
  "estimated_p2p_tests": <number of P2P tests, 0 if none>,
  "f2p_evidence": "<brief description or 'none found'>",
  "p2p_evidence": "<brief description or 'none found'>"
}}
"""


INFER_PROBLEM_PROMPT = """Analyze this code change and infer the problem statement.

**REJECT if:**
- Dev/feature branch merge (bundles unrelated changes)
- Release/version bump
- Config-only changes (.gitignore, CI, IDE files)

**Title/Message:**
{message}

**Files Changed:**
{file_list}

**Code Diff:**
{diff}

Write a clear problem statement as if it were a Github issue. Be specific and technical.

Respond with ONLY JSON (use "This change" not "This commit/PR" in rejection_reason):
{{
  "is_atomic": <true if single logical change, false otherwise>,
  "rejection_reason": "<if not atomic, explain why starting with 'This change...'>",
  "problem_statement": "<if atomic, describe the issue being fixed>",
  "title": "<short title>",
  "confidence": "high|medium|low"
}}
"""


FAIRNESS_EVAL_PROMPT = """\
You are evaluating a SWE benchmark task for fairness BEFORE any agent runs.
For each F2P (fail-to-pass) test listed, determine if an agent could reasonably pass it
given only the spec and hints — or if the test is unfair.

=== SPEC / ISSUE ===
{problem_statement}

{hints_section}

=== GOLD SOURCE DIFF ===
{src_diff}

=== GOLD TEST DIFF ===
{test_diff}

## CLASSIFICATION RULES

### UNFAIR when (tag each with one of):
- Spec gap: requirement completely absent from spec/hints — agent had no way to know
- Ambiguous spec: spec exists but vague; test enforces one specific interpretation
- Test too coupled to gold: test asserts internal implementation detail only visible in gold src (algorithm choice, internal variable names, specific library, exact arithmetic)
- Test contradicts spec: test enforces behavior that contradicts what spec explicitly states
- Hidden test: feature in test not mentioned in spec, hints, or gold src public interface
- Test quality flaw: wrong assertion, broken fixture, checks magic values not in spec

### FAIR when:
1. Spec explicitly states the requirement
2. Hints document the exact contract
3. Test checks observable public behavior any correct implementation must satisfy
4. Return type, function signature, field name visible in spec/hints
5. Standard coding practice (error handling, status codes, empty array returns)

### CASCADE RULE:
One root cause → many failures → all share same verdict

Respond with ONLY this JSON (no markdown, no extra text):

{{
  "fairness_rationale": "<one sentence summary>",
  "f2p_tests": [
    {{
      "test_name": "<short description max 40 chars>",
      "in_spec": <true or false>,
      "tests_good": <true or false>,
      "verdict": "<Fair or Unfair>",
      "notes": "<one phrase, name unfair tag if Unfair, max 60 chars>"
    }}
  ]
}}
"""


class FairnessEvaluator:
    def __init__(self, max_diff_lines: int = 1000):
        self.max_diff_lines = max_diff_lines

    def evaluate(
        self,
        src_diff: str,
        test_diff: str,
        problem_statement: str,
        hints: str = "",
    ) -> Optional[dict]:
        """Evaluate fairness of F2P tests. Returns fairness_score, fairness_rationale,
        and f2p_tests_fairness_analysis, or None on failure."""
        hints_section = f"=== HINTS ===\n{hints}" if hints else "(No hints provided)"

        prompt = FAIRNESS_EVAL_PROMPT.format(
            problem_statement=problem_statement or "(Not provided)",
            hints_section=hints_section,
            src_diff=self._truncate_diff(src_diff) or "(No source changes)",
            test_diff=self._truncate_diff(test_diff) or "(No test changes)",
        )

        data = self._parse_json_response(self._call_llm(prompt))
        if not data:
            return None

        f2p_tests = data.get("f2p_tests", [])
        fair_count = sum(1 for t in f2p_tests if t.get("verdict") == "Fair")
        total = len(f2p_tests)
        fairness_score = round(fair_count / total, 2) if total > 0 else 0.0

        return {
            "fairness_score": fairness_score,
            "fairness_rationale": data.get("fairness_rationale", ""),
            "f2p_tests_fairness_analysis": f2p_tests,
        }

    def _truncate_diff(self, diff: str) -> str:
        if not diff:
            return ""
        lines = diff.split("\n")
        if len(lines) <= self.max_diff_lines:
            return diff
        return (
            "\n".join(lines[: self.max_diff_lines])
            + f"\n\n... ({len(lines) - self.max_diff_lines} more lines truncated)"
        )

    def _call_llm(self, prompt: str) -> Optional[str]:
        messages = [
            {
                "role": "system",
                "content": "You are a code review assistant. Respond only with valid JSON.",
            },
            {"role": "user", "content": prompt},
        ]
        return call_llm(messages, temperature=0)

    def _parse_json_response(self, response: str) -> Optional[dict]:
        if not response:
            return None
        try:
            text = response.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                parts = text.split("```")
                if len(parts) >= 2:
                    text = parts[1].strip()
                    if text.startswith(("json", "JSON")):
                        text = text[4:].strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
            text = re.sub(r",\s*([}\]])", r"\1", text)
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                text = match.group(0)
            return json.loads(text)
        except (json.JSONDecodeError, IndexError) as e:
            logger.error(f"FairnessEvaluator failed to parse JSON: {e}")
            return None


class QualityEvaluator:
    def __init__(
        self,
        quality_threshold: int = 1,
        max_diff_lines: int = 1000,
    ):
        self.quality_threshold = quality_threshold
        self.max_diff_lines = max_diff_lines
        self.last_rejection_reason = None

    def check_f2p_p2p(
        self, src_diff: str, test_diff: str
    ) -> Tuple[bool, str, Optional[dict]]:
        if not test_diff or not test_diff.strip():
            return False, "No test changes found", None

        prompt = F2P_P2P_CHECK_PROMPT.format(
            src_diff=self._truncate_diff(src_diff, self.max_diff_lines // 2)
            or "(No source changes)",
            test_diff=self._truncate_diff(test_diff, self.max_diff_lines // 2),
        )

        data = self._parse_json_response(self._call_llm(prompt))
        if not data:
            return False, "Failed to analyze F2P/P2P tests", None

        f2p_evidence = data.get("f2p_evidence", "")
        p2p_evidence = data.get("p2p_evidence", "")
        estimated_f2p = data.get("estimated_f2p_tests", 0)
        estimated_p2p = data.get("estimated_p2p_tests", 0)

        # Cross-validate: override boolean if estimated count is 0
        has_f2p = data.get("has_f2p", False) and estimated_f2p > 0
        has_p2p = data.get("has_p2p", False) and estimated_p2p > 0

        stats = {
            "estimated_f2p_tests": estimated_f2p,
            "estimated_p2p_tests": estimated_p2p,
            "f2p_evidence": f2p_evidence,
            "p2p_evidence": p2p_evidence,
        }

        if not has_f2p and not has_p2p:
            return (
                False,
                f"No F2P or P2P tests detected. F2P: {f2p_evidence}, P2P: {p2p_evidence}",
                None,
            )
        if not has_f2p:
            return False, f"No F2P tests detected: {f2p_evidence}", None
        if not has_p2p:
            return False, f"No P2P tests detected: {p2p_evidence}", None

        return True, "", stats

    def evaluate_candidate(
        self,
        src_diff: str,
        test_diff: str,
        problem_statement: Optional[str] = None,
        hints: str = "",
        commit_message: str = "",
        files_changed: Optional[List[str]] = None,
    ) -> Tuple[bool, Optional[QualityScores]]:
        self.last_rejection_reason = None
        inference_confidence = None

        if not problem_statement or not problem_statement.strip():
            logger.info("No problem statement provided, inferring from diff...")
            inferred = self._infer_problem_statement(
                commit_message, files_changed or [], src_diff + "\n" + test_diff
            )
            if not inferred:
                self.last_rejection_reason = "Could not infer problem statement"
                return False, None

            # Atomicity check disabled - let quality rubrics handle this via patch_to_issue_alignment
            # if not inferred.get("is_atomic", True):
            #     reason = inferred.get("rejection_reason", "Non-atomic change detected")
            #     logger.info(f"Rejected: {reason}")
            #     self.last_rejection_reason = reason
            #     return False, None

            problem_statement = inferred.get("problem_statement")
            inference_confidence = inferred.get("confidence")

        else:
            pass  # problem_statement already provided

        # If there are no tests, avoid asking the model to score test-related rubrics.
        no_tests = (not test_diff) or (not test_diff.strip())
        if no_tests:
            scores = self._evaluate_quality_no_tests(problem_statement, hints, src_diff)
        else:
            scores = self._evaluate_quality(
                problem_statement, hints, src_diff, test_diff
            )
        if not scores:
            self.last_rejection_reason = "LLM evaluation failed to return scores"
            return False, None

        scores.inferred_problem_statement = problem_statement
        self.inferred_problem_statement = problem_statement
        scores.inference_confidence = inference_confidence

        passes = scores.passes_threshold(self.quality_threshold)
        if passes:
            logger.info(f"✅ Candidate PASSED (total score: {scores.total_score()})")
        else:
            logger.info(
                f"❌ Candidate FAILED (total score: {scores.total_score()}, threshold: all <= {self.quality_threshold})"
            )

        return passes, scores

    def _infer_problem_statement(
        self, message: str, files: List[str], diff: str
    ) -> Optional[dict]:
        diff_lines = diff.split("\n")
        if len(diff_lines) > self.max_diff_lines:
            diff = (
                "\n".join(diff_lines[: self.max_diff_lines])
                + f"\n\n... (truncated {len(diff_lines) - self.max_diff_lines} lines)"
            )

        prompt = INFER_PROBLEM_PROMPT.format(
            message=message or "(No message)",
            file_list="\n".join(f"- {f}" for f in files[:50]) or "(No files)",
            diff=diff,
        )
        return self._parse_json_response(self._call_llm(prompt))

    def _evaluate_quality_no_tests(
        self, problem_statement: str, hints: str, src_diff: str
    ) -> Optional[QualityScores]:
        """
        Evaluate only non-test rubrics. Test-related rubrics are forced to 3 with fixed rationales.
        """
        prompt = NO_TESTS_EVALUATION_PROMPT.format(
            problem_statement=problem_statement or "(Not provided)",
            hints=hints or "(No hints)",
            src_diff=self._truncate_diff(src_diff, self.max_diff_lines)
            or "(No source changes)",
        )

        data = self._parse_json_response(self._call_llm(prompt))
        if not data:
            return None

        try:
            issue_clarity = int(data.get("issue_clarity", 3))
            issue_clarity_label = data.get("issue_clarity_label", "Unknown")
            issue_clarity_rationale = data.get("issue_clarity_rationale", "")
            gold_patch_clarity = int(data.get("gold_patch_clarity", 3))
            patch_to_issue_alignment = int(data.get("patch_to_issue_alignment", 3))
            clarity_rationale = data.get("clarity_rationale", "")
            alignment_rationale = data.get("alignment_rationale", "")
            task_difficulty = int(data.get("task_difficulty", 2))
            task_difficulty_label = data.get("task_difficulty_label", "Unknown")
            task_difficulty_rationale = data.get("task_difficulty_rationale", "")

            # Force test-related rubrics to 3
            return QualityScores(
                gold_patch_clarity=gold_patch_clarity,
                patch_to_issue_alignment=patch_to_issue_alignment,
                test_clarity=3,
                fn_score=3,
                fp_score=3,
                fn_label="Overly Restrictive",
                fp_label="Overly Permissive",
                fn_rationale="No tests provided; cannot validate generalization.",
                fp_rationale="No tests provided; cannot validate strictness.",
                clarity_rationale=clarity_rationale,
                alignment_rationale=alignment_rationale,
                test_rationale="No test files/test diff found.",
                issue_clarity=issue_clarity,
                issue_clarity_label=issue_clarity_label,
                issue_clarity_rationale=issue_clarity_rationale,
                test_to_issue_alignment=3,
                test_to_issue_alignment_label="Poor",
                test_to_issue_alignment_rationale="No test files/test diff found.",
                task_difficulty=task_difficulty,
                task_difficulty_label=task_difficulty_label,
                task_difficulty_rationale=task_difficulty_rationale,
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"Failed to parse no-tests quality scores: {e}")
            return None

    def _evaluate_quality(
        self, problem_statement: str, hints: str, src_diff: str, test_diff: str
    ) -> Optional[QualityScores]:
        prompt = UNIFIED_EVALUATION_PROMPT.format(
            problem_statement=problem_statement or "(Not provided)",
            hints=hints or "(No hints)",
            src_diff=self._truncate_diff(src_diff, self.max_diff_lines // 2)
            or "(No source changes)",
            test_diff=self._truncate_diff(test_diff, self.max_diff_lines // 2)
            or "(No test changes)",
        )

        data = self._parse_json_response(self._call_llm(prompt))
        if not data:
            return None

        try:
            return QualityScores(
                # Existing rubrics
                gold_patch_clarity=int(data.get("gold_patch_clarity", 3)),
                patch_to_issue_alignment=int(data.get("patch_to_issue_alignment", 3)),
                test_clarity=int(data.get("test_clarity", 3)),
                fn_score=int(data.get("fn_score", 3)),
                fp_score=int(data.get("fp_score", 3)),
                fn_label=data.get("fn_label", "Unknown"),
                fp_label=data.get("fp_label", "Unknown"),
                fn_rationale=data.get("fn_rationale", ""),
                fp_rationale=data.get("fp_rationale", ""),
                clarity_rationale=data.get("clarity_rationale", ""),
                alignment_rationale=data.get("alignment_rationale", ""),
                test_rationale=data.get("test_rationale", ""),
                # New rubrics
                issue_clarity=int(data.get("issue_clarity", 3)),
                issue_clarity_label=data.get("issue_clarity_label", "Unknown"),
                issue_clarity_rationale=data.get("issue_clarity_rationale", ""),
                test_to_issue_alignment=int(data.get("test_to_issue_alignment", 3)),
                test_to_issue_alignment_label=data.get(
                    "test_to_issue_alignment_label", "Unknown"
                ),
                test_to_issue_alignment_rationale=data.get(
                    "test_to_issue_alignment_rationale", ""
                ),
                task_difficulty=int(data.get("task_difficulty", 2)),
                task_difficulty_label=data.get("task_difficulty_label", "Unknown"),
                task_difficulty_rationale=data.get("task_difficulty_rationale", ""),
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"Failed to parse quality scores: {e}")
            return None

    def _truncate_diff(self, diff: str, max_lines: int) -> str:
        if not diff:
            return ""
        lines = diff.split("\n")
        if len(lines) <= max_lines:
            return diff
        return (
            "\n".join(lines[:max_lines])
            + f"\n\n... ({len(lines) - max_lines} more lines truncated)"
        )

    def _call_llm(self, prompt: str) -> Optional[str]:
        return self._call_openai(prompt)

    def _call_openai(self, prompt: str) -> Optional[str]:
        messages = [
            {
                "role": "system",
                "content": "You are a code review assistant. Respond only with valid JSON.",
            },
            {"role": "user", "content": prompt},
        ]
        return call_llm(messages, temperature=0)

    def _parse_json_response(self, response: str) -> Optional[dict]:
        if not response:
            return None
        try:
            text = response.strip()

            # Extract JSON from markdown code blocks if present
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                parts = text.split("```")
                if len(parts) >= 2:
                    text = parts[1].strip()
                    if text.startswith(("json", "JSON")):
                        text = text[4:].strip()

            # Try direct parse first
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass

            # Fix common LLM JSON mistakes
            # Remove trailing commas before } or ]
            text = re.sub(r",\s*([}\]])", r"\1", text)

            # Try to extract just the JSON object if there's extra text
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                text = match.group(0)

            return json.loads(text)

        except (json.JSONDecodeError, IndexError) as e:
            logger.error(f"Failed to parse JSON: {e}")
            logger.debug(f"Raw response: {response[:500]}...")
            return None


def split_patch_by_test_files(
    full_patch: str, is_test_file_func, language_config: dict
) -> Tuple[str, str]:
    """Split a unified diff into source and test diffs."""
    if not full_patch:
        return "", ""

    src_lines = []
    test_lines = []
    current_file_lines = []
    current_is_test = False

    for line in full_patch.split("\n"):
        if line.startswith("diff --git"):
            if current_file_lines:
                (test_lines if current_is_test else src_lines).extend(
                    current_file_lines
                )
                current_file_lines = []
            match = re.search(r"diff --git a/(.+?) b/", line)
            if match:
                current_is_test = is_test_file_func(match.group(1), language_config)
        current_file_lines.append(line)

    if current_file_lines:
        (test_lines if current_is_test else src_lines).extend(current_file_lines)

    return "\n".join(src_lines), "\n".join(test_lines)
