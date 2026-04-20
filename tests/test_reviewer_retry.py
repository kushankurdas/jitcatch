from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jitcatch import llm, report  # noqa: E402
from jitcatch.config import (  # noqa: E402
    CatchCandidate,
    GeneratedTest,
    ReviewFinding,
    TestResult,
)
from jitcatch.workflows import retry as retry_mod  # noqa: E402
from jitcatch.workflows.reviewer import run_agentic_reviewer  # noqa: E402


class _StubLLM:
    """Minimal stub for reviewer+retry tests. Records calls so assertions
    can check the flow, returns canned data via constructor."""

    def __init__(
        self,
        findings=None,
        validations=None,
        retries=None,
    ) -> None:
        self._findings = findings or []
        self._validations = validations
        self._retries = retries or []
        self.review_calls = 0
        self.validate_calls = 0
        self.retry_calls = 0
        self.last_gap: dict = {}

    def review_diff(self, bundle, lang):
        self.review_calls += 1
        return list(self._findings)

    def validate_findings(self, findings, bundle, lang):
        self.validate_calls += 1
        if self._validations is None:
            for f in findings:
                f.validator_verdict = "keep"
            return findings
        by_idx = {v["index"]: v for v in self._validations}
        kept = []
        for i, f in enumerate(findings):
            v = by_idx.get(i, {"verdict": "keep"})
            verdict = v.get("verdict", "keep")
            f.validator_verdict = verdict
            f.validator_note = v.get("note", "")
            if verdict != "drop":
                kept.append(f)
        return kept

    def retry_tests(self, bundle, lang, hints, gap):
        self.retry_calls += 1
        self.last_gap = dict(gap)
        return list(self._retries)


class ParseFindingsTest(unittest.TestCase):
    def test_parses_findings_array(self):
        raw = json.dumps({
            "findings": [
                {
                    "file": "a.js",
                    "line": 12,
                    "title": "auth bypass",
                    "rationale": "catch block swallows 401",
                    "severity": "High",
                    "category": "security",
                    "confidence": 0.9,
                }
            ]
        })
        out = llm._parse_findings(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].file, "a.js")
        self.assertEqual(out[0].severity, "High")
        self.assertAlmostEqual(out[0].confidence, 0.9, places=5)

    def test_tolerates_fenced_output(self):
        raw = (
            "Sure!\n```json\n"
            '[{"file":"x","line":null,"title":"t","rationale":"r",'
            '"severity":"Low","category":"validation","confidence":0.3}]\n```'
        )
        out = llm._parse_findings(raw)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0].line)

    def test_clamps_confidence(self):
        raw = '{"findings":[{"title":"t","rationale":"r","confidence":2.5}]}'
        out = llm._parse_findings(raw)
        self.assertEqual(out[0].confidence, 1.0)

    def test_returns_empty_on_garbage(self):
        self.assertEqual(llm._parse_findings("prose only"), [])


class ParseValidationsTest(unittest.TestCase):
    def test_basic(self):
        raw = '{"validations":[{"index":0,"verdict":"keep"},{"index":1,"verdict":"drop","note":"fp"}]}'
        out = llm._parse_validations(raw)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["verdict"], "drop")


class ReviewerWorkflowTest(unittest.TestCase):
    def test_keeps_validated_findings(self):
        stub = _StubLLM(
            findings=[
                ReviewFinding(file="a.js", line=5, title="bug1", rationale="r1"),
                ReviewFinding(file="b.js", line=10, title="bug2", rationale="r2"),
            ],
            validations=[
                {"index": 0, "verdict": "keep"},
                {"index": 1, "verdict": "drop", "note": "fp"},
            ],
        )
        out = run_agentic_reviewer(stub, bundle="...", lang="javascript")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].file, "a.js")
        self.assertEqual(stub.validate_calls, 1)

    def test_skip_validator_keeps_all(self):
        stub = _StubLLM(
            findings=[ReviewFinding(file="a.js", line=1, title="t", rationale="r")],
        )
        out = run_agentic_reviewer(
            stub, bundle="...", lang="javascript", skip_validator=True
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(stub.validate_calls, 0)
        self.assertEqual(out[0].validator_verdict, "keep")

    def test_empty_findings_skips_validator(self):
        stub = _StubLLM(findings=[])
        out = run_agentic_reviewer(stub, bundle="...", lang="python")
        self.assertEqual(out, [])
        self.assertEqual(stub.validate_calls, 0)


def _make_cand(name: str, rationale: str, parent_pass: bool, child_pass: bool) -> CatchCandidate:
    return CatchCandidate(
        workflow="intent_aware",
        test=GeneratedTest(name=name, code="// stub", rationale=rationale),
        risks=[],
        parent_result=TestResult(
            status="pass" if parent_pass else "fail", exit_code=0, stdout="", stderr=""
        ),
        child_result=TestResult(
            status="pass" if child_pass else "fail", exit_code=0, stdout="", stderr=""
        ),
        target_files=["x.js"],
    )


class RetryGapDetectionTest(unittest.TestCase):
    def test_weak_catch_closes_gap(self):
        risks = [
            "[a.js:10] (security) verifyApiKey returns 401 on invalid key",
            "[b.js:20] (validation) isInvalidDate rejects month 13",
        ]
        caught = _make_cand(
            "verifyApiKey returns 401 on invalid key",
            "verifyApiKey should 401",
            parent_pass=True,
            child_pass=False,
        )
        uncaught = _make_cand(
            "isInvalidDate rejects month 13",
            "month 13 should be invalid",
            parent_pass=True,
            child_pass=True,  # both pass = still a gap
        )
        gaps = retry_mod.find_gaps(risks, [caught, uncaught])
        self.assertEqual(len(gaps), 1)
        self.assertIn("isInvalidDate", gaps[0]["risk"])
        self.assertEqual(gaps[0]["failure_mode"], "both_passed")
        self.assertEqual(gaps[0]["prior_test_name"], "isInvalidDate rejects month 13")

    def test_failure_mode_classification(self):
        both_pass = _make_cand("t", "r", True, True)
        parent_fail = _make_cand("t", "r", False, True)
        both_fail = _make_cand("t", "r", False, False)
        self.assertEqual(retry_mod._failure_mode(both_pass), "both_passed")
        self.assertEqual(retry_mod._failure_mode(parent_fail), "parent_failed")
        self.assertEqual(retry_mod._failure_mode(both_fail), "both_failed")

    def test_retry_round_calls_llm_per_gap(self):
        stub = _StubLLM(
            retries=[GeneratedTest(name="retry_t", code="assert False", rationale="v2")],
        )
        gaps = [
            {"risk": "risk A", "failure_mode": "both_passed", "prior_test_code": "x", "prior_test_name": "n", "failure_output": ""},
            {"risk": "risk B", "failure_mode": "both_passed", "prior_test_code": "y", "prior_test_name": "m", "failure_output": ""},
        ]
        out = retry_mod.run_retry_round(
            stub, bundle="b", lang="js", hints="h", gaps=gaps, max_gaps=5
        )
        self.assertEqual(len(out), 2)
        self.assertEqual(stub.retry_calls, 2)
        self.assertEqual(out[0][0], "risk A")
        self.assertEqual(out[0][1].name, "retry_t")

    def test_retry_round_respects_max_gaps(self):
        stub = _StubLLM(retries=[GeneratedTest(name="t", code="...", rationale="")])
        gaps = [{"risk": f"r{i}", "failure_mode": "both_passed"} for i in range(5)]
        retry_mod.run_retry_round(stub, bundle="b", lang="js", hints="h", gaps=gaps, max_gaps=2)
        self.assertEqual(stub.retry_calls, 2)


class ReportWithFindingsTest(unittest.TestCase):
    def test_json_emits_findings(self):
        import tempfile
        findings = [
            ReviewFinding(
                file="a.js", line=5, title="bug", rationale="r",
                severity="High", category="security", confidence=0.9,
            )
        ]
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "r.json"
            report.write_json([], out, findings=findings)
            data = json.loads(out.read_text())
            self.assertEqual(data["summary"]["review_findings"], 1)
            self.assertEqual(len(data["review_findings"]), 1)
            self.assertEqual(data["review_findings"][0]["title"], "bug")

    def test_markdown_emits_review_section(self):
        import tempfile
        findings = [
            ReviewFinding(
                file="a.js", line=5, title="auth bypass", rationale="because",
                severity="High", category="security", confidence=0.9,
                validator_verdict="keep",
            )
        ]
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "r.md"
            report.write_markdown(
                [], out, meta={"repo": d}, file_diffs={}, findings=findings
            )
            body = out.read_text()
            self.assertIn("## Findings", body)
            self.assertIn("auth bypass", body)
            self.assertIn("security", body)
            self.assertIn("review-only", body)

    def test_markdown_empty_findings_omits_section(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "r.md"
            report.write_markdown([], out, meta={"repo": d}, file_diffs={}, findings=[])
            self.assertNotIn("## Findings", out.read_text())


class AnnotateFindingsTest(unittest.TestCase):
    def test_tags_findings_already_caught_by_test(self):
        cand = _make_cand(
            "manager: calculateSLA returns negative",
            "calculateSLA condition flipped",
            parent_pass=True,
            child_pass=False,
        )
        cand.risks = ["[app/modules/finding/manager.js:2224] (arithmetic) calculateSLA sign flip"]
        finding = ReviewFinding(
            file="app/modules/finding/manager.js", line=2224,
            title="calculateSLA sign flipped",
            rationale="< changed to >",
        )
        report._annotate_findings([finding], [cand])
        self.assertIn("already caught by a failing test", finding.validator_note)


if __name__ == "__main__":
    unittest.main()
