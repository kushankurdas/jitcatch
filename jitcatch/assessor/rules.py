from __future__ import annotations

import re
from typing import List

from ..config import CatchCandidate


FP_REFLECTION_PAT = re.compile(
    r"\b(getattr|hasattr|setattr|inspect\.|__dict__|Object\.getPrototypeOf|Reflect\.)",
)
# Mock usage on its own is not a false-positive signal. Middleware and
# handler tests legitimately stub transitive deps (axios, jwt, db drivers)
# to drive the function under test into its failure branch. A test that
# mocks everything to the point of tautology still passes on both revs
# and never becomes a weak catch, so no static penalty is needed.
FP_FLAKY_PAT = re.compile(
    r"\b(time\.sleep|datetime\.now|random\.|Math\.random|requests\.(get|post)|fetch\s*\(|axios\.)",
)
FP_UNDEF_VAR_PAT = re.compile(r"\b(NameError|ReferenceError)\b")
FP_IMPORT_ERR_PAT = re.compile(
    r"(ModuleNotFoundError|ImportError|SyntaxError|collection errors?|ERR_MODULE_NOT_FOUND)",
)

TP_NULL_PAT = re.compile(r"\b(NoneType|TypeError:.*None|Cannot read propert|is not a function)\b")
TP_VALUE_MISMATCH_PAT = re.compile(r"(AssertionError|assert\.strictEqual|Expected:|Received:)")


def apply_rules(cand: CatchCandidate) -> List[str]:
    flags: List[str] = []
    test_code = cand.test.code
    child = cand.child_result
    parent = cand.parent_result

    if _match(FP_REFLECTION_PAT, test_code):
        flags.append("fp:reflection")
    if _match(FP_FLAKY_PAT, test_code):
        flags.append("fp:flakiness")

    if child and _match(FP_UNDEF_VAR_PAT, child.stdout + child.stderr):
        flags.append("fp:undefined_variable")
    if child and _match(FP_IMPORT_ERR_PAT, child.stdout + child.stderr):
        flags.append("fp:broken_test_runner")
    # Only penalize as flake if parent fails but child passes. When both
    # fail the regression may be real and masked by a pre-existing error.
    if parent and not parent.passed and child and child.passed:
        flags.append("fp:parent_unstable")

    if child and _match(TP_NULL_PAT, child.stdout + child.stderr):
        flags.append("tp:null_value")
    if child and _match(TP_VALUE_MISMATCH_PAT, child.stdout + child.stderr):
        flags.append("tp:value_mismatch")

    # Both-fail case: if child stderr introduces an assertion line the
    # parent stderr doesn't carry, treat that as new failure mode (TP).
    if parent and not parent.passed and child and not child.passed:
        p_lines = set((parent.stdout + parent.stderr).splitlines())
        for line in (child.stdout + child.stderr).splitlines():
            if line and line not in p_lines and _match(TP_VALUE_MISMATCH_PAT, line):
                flags.append("tp:new_failure_mode")
                break

    return flags


def score_candidate(cand: CatchCandidate) -> float:
    base = cand.judge_tp_prob
    n_fp = sum(1 for f in cand.rule_flags if f.startswith("fp:"))
    n_tp = sum(1 for f in cand.rule_flags if f.startswith("tp:"))
    score = base - 0.3 * n_fp + 0.1 * n_tp
    return max(-1.0, min(1.0, score))


def _match(pat: re.Pattern[str], text: str) -> bool:
    return bool(text and pat.search(text))
