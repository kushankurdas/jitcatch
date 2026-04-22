from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jitcatch.assessor.rules import apply_rules, score_candidate  # noqa: E402
from jitcatch.config import CatchCandidate, GeneratedTest  # noqa: E402
from jitcatch.config import TestResult as _TestResult  # noqa: E402. Underscore hides it from pytest collection


def _cand(parent: _TestResult, child: _TestResult, code: str = "expect(1).toBe(1)") -> CatchCandidate:
    return CatchCandidate(
        workflow="intent_aware",
        test=GeneratedTest(name="t", code=code),
        parent_result=parent,
        child_result=child,
    )


def _pass() -> _TestResult:
    return _TestResult(status="pass", exit_code=0, stdout="", stderr="")


def _fail(msg: str = "AssertionError: expected X") -> _TestResult:
    return _TestResult(status="fail", exit_code=1, stdout="", stderr=msg)


class ParentUnstableRuleTest(unittest.TestCase):
    def test_flake_pattern_flagged(self) -> None:
        flags = apply_rules(_cand(_fail(), _pass()))
        self.assertIn("fp:parent_unstable", flags)

    def test_both_fail_not_flagged_as_flake(self) -> None:
        flags = apply_rules(_cand(_fail("boom old"), _fail("boom old")))
        self.assertNotIn("fp:parent_unstable", flags)

    def test_new_failure_mode_flagged_when_child_diverges(self) -> None:
        parent = _fail("AssertionError: expected 1")
        child = _fail("AssertionError: expected 2")
        flags = apply_rules(_cand(parent, child))
        self.assertIn("tp:new_failure_mode", flags)
        self.assertNotIn("fp:parent_unstable", flags)


class ScoreCandidateTest(unittest.TestCase):
    def test_tp_new_failure_mode_boosts_score(self) -> None:
        parent = _fail("AssertionError: expected 1")
        child = _fail("AssertionError: expected 2")
        cand = _cand(parent, child)
        cand.judge_tp_prob = 0.0
        cand.rule_flags = apply_rules(cand)
        self.assertGreater(score_candidate(cand), 0.0)


if __name__ == "__main__":
    unittest.main()
