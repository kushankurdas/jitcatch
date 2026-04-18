from __future__ import annotations

import re
from typing import List

from ..config import CatchCandidate


FP_REFLECTION_PAT = re.compile(
    r"\b(getattr|hasattr|setattr|inspect\.|__dict__|Object\.getPrototypeOf|Reflect\.)",
)
FP_MOCK_PAT = re.compile(
    r"\b(unittest\.mock|MagicMock|mock\.patch|jest\.fn|jest\.mock|sinon\.)",
)
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
    if _match(FP_MOCK_PAT, test_code):
        flags.append("fp:mock_usage")
    if _match(FP_FLAKY_PAT, test_code):
        flags.append("fp:flakiness")

    if child and _match(FP_UNDEF_VAR_PAT, child.stdout + child.stderr):
        flags.append("fp:undefined_variable")
    if child and _match(FP_IMPORT_ERR_PAT, child.stdout + child.stderr):
        flags.append("fp:broken_test_runner")
    if parent and not parent.passed:
        flags.append("fp:parent_unstable")

    if child and _match(TP_NULL_PAT, child.stdout + child.stderr):
        flags.append("tp:null_value")
    if child and _match(TP_VALUE_MISMATCH_PAT, child.stdout + child.stderr):
        flags.append("tp:value_mismatch")

    return flags


def score_candidate(cand: CatchCandidate) -> float:
    base = cand.judge_tp_prob
    n_fp = sum(1 for f in cand.rule_flags if f.startswith("fp:"))
    n_tp = sum(1 for f in cand.rule_flags if f.startswith("tp:"))
    score = base - 0.3 * n_fp + 0.1 * n_tp
    return max(-1.0, min(1.0, score))


def _match(pat: re.Pattern[str], text: str) -> bool:
    return bool(text and pat.search(text))
