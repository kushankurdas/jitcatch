from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jitcatch import report  # noqa: E402
from jitcatch.config import (  # noqa: E402
    CatchCandidate,
    GeneratedTest,
    ReviewFinding,
    TestResult,
)


def _weak_cand(name: str = "test_x", score: float = 0.85) -> CatchCandidate:
    c = CatchCandidate(
        workflow="intent_aware",
        test=GeneratedTest(name, "assert 1 == 2", "pin old"),
        target_files=["app.py"],
        risks=["[app.py:3] (value_mismatch) negated predicate"],
        parent_result=TestResult("pass", 0, "", ""),
        child_result=TestResult("fail", 1, "", "AssertionError"),
        judge_tp_prob=0.7,
        judge_bucket="High",
        judge_rationale="clear regression. New code flips the check.",
    )
    c.final_score = score
    return c


class WriteHtmlTest(unittest.TestCase):
    def test_self_contained_no_external_urls(self) -> None:
        with TemporaryDirectory() as d:
            out = Path(d) / "r.html"
            report.write_html([_weak_cand()], out)
            content = out.read_text()
            # Inlined style block.
            self.assertIn("<style>", content)
            self.assertIn("</style>", content)
            # No CDN / external fetches. No <link rel="stylesheet"> and no
            # http(s) URLs to css/js assets.
            self.assertNotIn("rel=\"stylesheet\"", content)
            self.assertNotIn("<script", content)
            self.assertNotIn("cdn.", content)

    def test_renders_weak_catch_details(self) -> None:
        with TemporaryDirectory() as d:
            out = Path(d) / "r.html"
            report.write_html(
                [_weak_cand("test_negation_regression")],
                out,
                meta={"command": "last", "repo": "/tmp/x"},
            )
            content = out.read_text()
            self.assertIn("JitCatch Report", content)
            self.assertIn("test_negation_regression", content)
            self.assertIn("High", content)  # severity badge label
            self.assertIn("clear regression", content)

    def test_empty_state(self) -> None:
        with TemporaryDirectory() as d:
            out = Path(d) / "r.html"
            report.write_html([], out)
            content = out.read_text()
            self.assertIn("No bugs surfaced", content)

    def test_review_only_finding_renders(self) -> None:
        with TemporaryDirectory() as d:
            out = Path(d) / "r.html"
            f = ReviewFinding(
                title="Credentials logged in plaintext",
                file="auth.py",
                line=12,
                severity="Critical",
                category="security",
                rationale="token written to stdout before hashing",
                confidence=0.9,
            )
            report.write_html([], out, findings=[f])
            content = out.read_text()
            self.assertIn("Credentials logged in plaintext", content)
            self.assertIn("Critical", content)
            self.assertIn("auth.py", content)

    def test_escapes_html_in_test_name(self) -> None:
        with TemporaryDirectory() as d:
            out = Path(d) / "r.html"
            c = _weak_cand("test_<script>alert(1)</script>")
            report.write_html([c], out)
            content = out.read_text()
            self.assertNotIn("<script>alert(1)</script>", content)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", content)

    def test_id_renders_in_html_header(self) -> None:
        """Stable id must appear in each finding header so users can
        copy it into `jitcatch explain`."""
        with TemporaryDirectory() as d:
            out = Path(d) / "r.html"
            c = _weak_cand("test_id_visible")
            report.write_html([c], out)
            content = out.read_text()
            sid = report.stable_id(c)
            self.assertIn(sid, content)

    def test_id_renders_in_markdown_header(self) -> None:
        with TemporaryDirectory() as d:
            out = Path(d) / "r.md"
            c = _weak_cand("test_id_visible_md")
            report.write_markdown([c], out)
            content = out.read_text()
            sid = report.stable_id(c)
            self.assertIn(sid, content)

    def test_false_positive_section_collapsed(self) -> None:
        """Trivial-severity candidates go into a collapsed <details>
        block. Keeps the main scroll clean while leaving evidence
        reachable."""
        with TemporaryDirectory() as d:
            out = Path(d) / "r.html"
            c = _weak_cand("test_low_signal", score=0.1)  # Trivial
            report.write_html([c], out)
            content = out.read_text()
            self.assertIn("Likely false positives", content)
            self.assertIn("<details", content)


class UsageRenderTest(unittest.TestCase):
    """Token/cost from UsageStats should surface in human-readable
    reports. The CLI stderr footer is ephemeral; shareable reports
    need the numbers inline."""

    def _stub_usage(self):
        from jitcatch.llm import UsageStats
        u = UsageStats()
        u.add("risks", "claude-sonnet-4-6", 1000, 200)
        u.add("tests", "claude-sonnet-4-6", 500, 400)
        return u

    def test_html_includes_cost_block(self) -> None:
        with TemporaryDirectory() as d:
            out = Path(d) / "r.html"
            report.write_html([_weak_cand()], out, usage=self._stub_usage())
            content = out.read_text()
            self.assertIn("LLM usage", content)
            self.assertIn("Cost (USD)", content)
            self.assertIn("risks=", content)

    def test_markdown_includes_cost_block(self) -> None:
        with TemporaryDirectory() as d:
            out = Path(d) / "r.md"
            report.write_markdown([_weak_cand()], out, usage=self._stub_usage())
            content = out.read_text()
            self.assertIn("## LLM usage", content)
            self.assertIn("Cost (USD)", content)

    def test_no_usage_block_when_no_calls(self) -> None:
        from jitcatch.llm import UsageStats
        with TemporaryDirectory() as d:
            out_html = Path(d) / "r.html"
            out_md = Path(d) / "r.md"
            report.write_html([_weak_cand()], out_html, usage=UsageStats())
            report.write_markdown([_weak_cand()], out_md, usage=UsageStats())
            self.assertNotIn("LLM usage", out_html.read_text())
            self.assertNotIn("LLM usage", out_md.read_text())


class FormatFlagTest(unittest.TestCase):
    """--format selects presentation formats on top of the always-on
    JSON report. Empty/unset → JSON only. 'json' is accepted as a no-op
    (writer is unconditional)."""

    def _parse(self, raw: str):
        from jitcatch import cli
        import argparse
        ns = argparse.Namespace(output_format=raw)
        return cli._formats(ns)

    def test_empty_means_no_presentation_formats(self) -> None:
        self.assertEqual(self._parse(""), set())

    def test_html_only(self) -> None:
        self.assertEqual(self._parse("html"), {"html"})

    def test_all_expands_to_presentation_formats(self) -> None:
        self.assertEqual(self._parse("all"), {"html", "md"})

    def test_json_is_noop(self) -> None:
        # JSON always written. Flag value accepted but ignored in the set.
        self.assertEqual(self._parse("json"), set())
        self.assertEqual(self._parse("html,json"), {"html"})

    def test_trims_whitespace_and_case(self) -> None:
        self.assertEqual(self._parse(" HTML , MD "), {"html", "md"})

    def test_unknown_format_exits(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse("xml")


if __name__ == "__main__":
    unittest.main()
