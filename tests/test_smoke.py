from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PY_PARENT_CALC = """def add(a, b):
    return a + b


def sub(a, b):
    return a - b
"""

PY_CHILD_CALC = """def add(a, b):
    return a - b


def sub(a, b):
    return a - b
"""

PY_STUB = {
    "risks": ["operator flipped from + to - in add()"],
    "intent_tests": [
        {
            "name": "add_basic",
            "code": (
                "from calc import add\n"
                "def test_add_basic():\n"
                "    assert add(2, 3) == 5\n"
                "    assert add(10, -4) == 6\n"
            ),
            "rationale": "checks add returns sum",
        }
    ],
    "dodgy_tests": [
        {
            "name": "add_zero",
            "code": (
                "from calc import add\n"
                "def test_add_zero():\n"
                "    assert add(0, 7) == 7\n"
            ),
            "rationale": "trivial identity for add",
        }
    ],
    "judge": {"tp_prob": 0.9, "bucket": "High", "rationale": "sign flip is a clear bug"},
}

JS_PARENT_CALC = """export function add(a, b) {
  return a + b;
}
export function sub(a, b) {
  return a - b;
}
"""

JS_CHILD_CALC = """export function add(a, b) {
  return a - b;
}
export function sub(a, b) {
  return a - b;
}
"""

JS_STUB = {
    "risks": ["operator flipped from + to - in add()"],
    "intent_tests": [
        {
            "name": "add_basic",
            "code": (
                "import { test } from 'node:test';\n"
                "import assert from 'node:assert/strict';\n"
                "import * as mod from './calc.mjs';\n"
                "test('add basic', () => {\n"
                "  assert.strictEqual(mod.add(2, 3), 5);\n"
                "  assert.strictEqual(mod.add(10, -4), 6);\n"
                "});\n"
            ),
            "rationale": "add returns sum",
        }
    ],
    "dodgy_tests": [],
    "judge": {"tp_prob": 0.9, "bucket": "High", "rationale": "sign flip is a clear bug"},
}


def _git(cwd: Path, *args: str) -> None:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "jitcatch")
    env.setdefault("GIT_AUTHOR_EMAIL", "jc@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "jitcatch")
    env.setdefault("GIT_COMMITTER_EMAIL", "jc@example.com")
    subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env, capture_output=True)


def make_repo(root: Path, filename: str, parent_src: str, child_src: str, stub: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".jitcatch_stub.json").write_text(json.dumps(stub, indent=2))
    (root / filename).write_text(parent_src)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "parent")
    (root / filename).write_text(child_src)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "child (buggy)")


class SmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc_smoke_"))
        self.repo_root = Path(__file__).resolve().parents[1]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_cli(self, repo: Path, filename: str, report_name: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.repo_root) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [
                sys.executable, "-m", "jitcatch.cli", "run", str(repo),
                "--file", filename,
                "--stub", "--no-judge",
                "--filename", report_name,
            ],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_reviewer_findings_surface_in_report(self) -> None:
        """End-to-end: agentic reviewer emits a finding via stub, report
        files contain it even when test-gen doesn't produce a weak
        catch. Exercises the BugBot-parity channel introduced for
        regressions tests can't exercise (mocks, env-coupled paths)."""
        repo = self.tmp / "rev"
        stub = dict(PY_STUB)
        stub["review_findings"] = [
            {
                "file": "calc.py",
                "line": 2,
                "title": "add: + flipped to -",
                "rationale": "Operator changed from + to - in add(); diff at line 2.",
                "severity": "High",
                "category": "arithmetic",
                "confidence": 0.95,
            }
        ]
        make_repo(repo, "calc.py", PY_PARENT_CALC, PY_CHILD_CALC, stub)
        proc = self._run_cli(repo, "calc.py", "rev_report")
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")
        json_path = repo / ".jitcatch" / "output" / "rev_report.json"
        data = json.loads(json_path.read_text())
        self.assertEqual(data["summary"]["review_findings"], 1)
        self.assertEqual(data["review_findings"][0]["title"], "add: + flipped to -")
        md_body = (repo / ".jitcatch" / "output" / "rev_report.md").read_text()
        self.assertIn("## Findings", md_body)
        self.assertIn("add: + flipped to -", md_body)

    def test_python_fixture_detects_bug(self) -> None:
        repo = self.tmp / "py"
        make_repo(repo, "calc.py", PY_PARENT_CALC, PY_CHILD_CALC, PY_STUB)
        proc = self._run_cli(repo, "calc.py", "py_report")
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out = repo / ".jitcatch" / "output" / "py_report.json"
        data = json.loads(out.read_text())
        self.assertGreaterEqual(data["summary"]["weak_catches"], 1, msg=proc.stdout)
        weak = [c for c in data["candidates"] if c["is_weak_catch"]]
        self.assertTrue(all(c["parent_result"]["status"] == "pass" for c in weak))
        self.assertTrue(all(c["child_result"]["status"] == "fail" for c in weak))

    def test_javascript_fixture_detects_bug(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node not installed")
        repo = self.tmp / "js"
        make_repo(repo, "calc.mjs", JS_PARENT_CALC, JS_CHILD_CALC, JS_STUB)
        proc = self._run_cli(repo, "calc.mjs", "js_report")
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out = repo / ".jitcatch" / "output" / "js_report.json"
        data = json.loads(out.read_text())
        self.assertGreaterEqual(data["summary"]["weak_catches"], 1, msg=proc.stdout)
        weak = [c for c in data["candidates"] if c["is_weak_catch"]]
        self.assertTrue(all(c["parent_result"]["status"] == "pass" for c in weak))
        self.assertTrue(all(c["child_result"]["status"] == "fail" for c in weak))


if __name__ == "__main__":
    unittest.main()
