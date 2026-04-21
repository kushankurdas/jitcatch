from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jitcatch import cli  # noqa: E402
from jitcatch.config import CatchCandidate, GeneratedTest, TestResult  # noqa: E402
from jitcatch.report import stable_id  # noqa: E402


def _write_report(out_dir: Path, candidates: list) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "jitcatch-20260422-000000.json"
    payload = {"summary": {}, "candidates": candidates, "review_findings": []}
    path.write_text(json.dumps(payload))
    return path


class StableIdTest(unittest.TestCase):
    def test_same_inputs_produce_same_id(self) -> None:
        c1 = CatchCandidate(workflow="intent_aware", test=GeneratedTest("t", "code"), target_files=["a.py"])
        c2 = CatchCandidate(workflow="intent_aware", test=GeneratedTest("t", "code"), target_files=["a.py"])
        self.assertEqual(stable_id(c1), stable_id(c2))

    def test_different_name_produces_different_id(self) -> None:
        c1 = CatchCandidate(workflow="intent_aware", test=GeneratedTest("a", "x"), target_files=["f.py"])
        c2 = CatchCandidate(workflow="intent_aware", test=GeneratedTest("b", "x"), target_files=["f.py"])
        self.assertNotEqual(stable_id(c1), stable_id(c2))

    def test_target_file_order_ignored(self) -> None:
        c1 = CatchCandidate(workflow="w", test=GeneratedTest("t", "x"), target_files=["a.py", "b.py"])
        c2 = CatchCandidate(workflow="w", test=GeneratedTest("t", "x"), target_files=["b.py", "a.py"])
        self.assertEqual(stable_id(c1), stable_id(c2))


class ExplainSubcommandTest(unittest.TestCase):
    def _run(self, argv: list) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_prints_detail_for_matching_id(self) -> None:
        with TemporaryDirectory() as d:
            repo = Path(d)
            (repo / ".git").mkdir()
            cand = CatchCandidate(
                workflow="intent_aware",
                test=GeneratedTest("test_X", "assert 1 == 2", "pin old"),
                target_files=["app.py"],
                parent_result=TestResult("pass", 0, "", ""),
                child_result=TestResult("fail", 1, "", "AssertionError"),
                judge_tp_prob=0.7,
                judge_bucket="High",
                judge_rationale="clear regression",
            )
            cand.final_score = 0.7
            cid = stable_id(cand)
            _write_report(
                repo / ".jitcatch" / "output",
                [{**cand.__dict__, "id": cid, "is_weak_catch": True,
                  "test": {"name": cand.test.name, "code": cand.test.code, "rationale": cand.test.rationale},
                  "parent_result": cand.parent_result.__dict__,
                  "child_result": cand.child_result.__dict__}],
            )
            rc, stdout, _ = self._run(["explain", str(repo), cid[:6]])
            self.assertEqual(rc, 0)
            self.assertIn(cid, stdout)
            self.assertIn("test_X", stdout)
            self.assertIn("clear regression", stdout)
            self.assertIn("assert 1 == 2", stdout)
            self.assertIn("AssertionError", stdout)

    def test_no_match_returns_nonzero(self) -> None:
        with TemporaryDirectory() as d:
            repo = Path(d)
            (repo / ".git").mkdir()
            _write_report(repo / ".jitcatch" / "output", [{"id": "abcd1234", "test": {"name": "t"}}])
            rc, _, err = self._run(["explain", str(repo), "zzzz"])
            self.assertEqual(rc, 1)
            self.assertIn("no candidate", err)

    def test_ambiguous_prefix_errors(self) -> None:
        with TemporaryDirectory() as d:
            repo = Path(d)
            (repo / ".git").mkdir()
            _write_report(
                repo / ".jitcatch" / "output",
                [
                    {"id": "abcd1111", "test": {"name": "x"}},
                    {"id": "abcd2222", "test": {"name": "y"}},
                ],
            )
            rc, _, err = self._run(["explain", str(repo), "abcd"])
            self.assertEqual(rc, 1)
            self.assertIn("ambiguous", err)

    def test_missing_report_errors(self) -> None:
        with TemporaryDirectory() as d:
            repo = Path(d)
            (repo / ".git").mkdir()
            rc, _, err = self._run(["explain", str(repo), "deadbeef"])
            self.assertEqual(rc, 2)
            self.assertIn("no JSON report found", err)

    def test_short_prefix_rejected(self) -> None:
        with TemporaryDirectory() as d:
            repo = Path(d)
            (repo / ".git").mkdir()
            _write_report(repo / ".jitcatch" / "output", [{"id": "abcd1234", "test": {"name": "t"}}])
            rc, _, err = self._run(["explain", str(repo), "abc"])
            self.assertEqual(rc, 2)
            self.assertIn("at least 4", err)


class ExplainChatTest(unittest.TestCase):
    def _write_cand(self, repo: Path) -> str:
        _write_report(
            repo / ".jitcatch" / "output",
            [{"id": "abcd1234", "test": {"name": "t", "code": "x"},
              "parent_result": {}, "child_result": {},
              "is_weak_catch": True, "final_score": 0.5,
              "judge_rationale": "r"}],
        )
        return "abcd1234"

    def test_chat_loop_uses_stub(self) -> None:
        with TemporaryDirectory() as d:
            repo = Path(d)
            (repo / ".git").mkdir()
            (repo / ".jitcatch_stub.json").write_text(json.dumps({
                "chat_replies": ["one", "two"],
            }))
            cid = self._write_cand(repo)
            stdin = io.StringIO("why did this fail?\nand what now?\nexit\n")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err), \
                 patch.object(sys, "stdin", stdin), \
                 patch.object(sys.stdin, "isatty", lambda: True, create=True):
                rc = cli.main(["explain", str(repo), cid, "--stub"])
            self.assertEqual(rc, 0)
            stdout = out.getvalue()
            self.assertIn("jitcatch explain", stdout)
            self.assertIn("llm", stdout)
            self.assertIn("one", stdout)
            self.assertIn("two", stdout)

    def test_no_chat_flag_skips_repl(self) -> None:
        with TemporaryDirectory() as d:
            repo = Path(d)
            (repo / ".git").mkdir()
            cid = self._write_cand(repo)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err), \
                 patch.object(sys.stdin, "isatty", lambda: True, create=True):
                rc = cli.main(["explain", str(repo), cid, "--no-chat"])
            self.assertEqual(rc, 0)
            self.assertNotIn("[chat]", out.getvalue())

    def test_non_tty_skips_repl(self) -> None:
        with TemporaryDirectory() as d:
            repo = Path(d)
            (repo / ".git").mkdir()
            cid = self._write_cand(repo)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err), \
                 patch.object(sys.stdin, "isatty", lambda: False, create=True):
                rc = cli.main(["explain", str(repo), cid])
            self.assertEqual(rc, 0)
            self.assertNotIn("[chat]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
