from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(cwd: Path, *args: str) -> None:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "jitcatch")
    env.setdefault("GIT_AUTHOR_EMAIL", "jc@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "jitcatch")
    env.setdefault("GIT_COMMITTER_EMAIL", "jc@example.com")
    subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env, capture_output=True)


A_PARENT = """module.exports.add = function(a, b) { return a + b; };
module.exports.sub = function(a, b) { return a - b; };
"""

# Rename add -> plus in a.js. b.js still calls .add, which will blow up.
A_CHILD = """module.exports.plus = function(a, b) { return a + b; };
module.exports.sub = function(a, b) { return a - b; };
"""

B_PARENT = """const a = require('./a');
module.exports.run = function() { return a.add(2, 3); };
"""

# b.js is touched (trivial edit) so PR mode sees both files in the diff.
# Point of the test: a generated test that imports BOTH files catches the
# cross-file breaking rename in a.js via b.js's call site.
B_CHILD = "// touched by PR\n" + B_PARENT


STUB = {
    "risks": ["add() may have been renamed"],
    "bundle_intent_tests": [
        {
            "name": "b_still_works",
            "code": (
                "const { test } = require('node:test');\n"
                "const assert = require('node:assert/strict');\n"
                "const b = require('./b');\n"
                "test('b.run returns 5', () => {\n"
                "  assert.strictEqual(b.run(), 5);\n"
                "});\n"
            ),
            "rationale": "cross-file check: b.run depends on a.add",
            "target_files": ["a.js", "b.js"],
        }
    ],
    "bundle_dodgy_tests": [],
    "judge": {"tp_prob": 0.9, "bucket": "High", "rationale": "cross-file rename"},
}


class PRModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc_pr_"))
        self.repo = self.tmp / "r"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_repo(self) -> None:
        self.repo.mkdir(parents=True)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "commit.gpgsign", "false")
        # CJS repo — no "type": "module" in package.json
        (self.repo / "package.json").write_text('{"name":"pr-fixture","version":"0.0.0"}\n')
        (self.repo / ".jitcatch_stub.json").write_text(json.dumps(STUB, indent=2))
        (self.repo / "a.js").write_text(A_PARENT)
        (self.repo / "b.js").write_text(B_PARENT)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "parent")
        (self.repo / "a.js").write_text(A_CHILD)
        (self.repo / "b.js").write_text(B_CHILD)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "child (renames add to plus)")

    def _run_cli(self, *extra_args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "jitcatch.cli", *extra_args],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_pr_mode_catches_cross_file_bug_via_last(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node not installed")
        self._make_repo()
        proc = self._run_cli(
            "last", str(self.repo), "--stub", "--no-judge",
            "--filename", "report", "--format", "all",
        )
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out = self.repo / ".jitcatch" / "output" / "report.json"
        data = json.loads(out.read_text())
        self.assertGreaterEqual(data["summary"]["weak_catches"], 1, msg=proc.stdout)
        weak = [c for c in data["candidates"] if c["is_weak_catch"]]
        self.assertTrue(any(set(c["target_files"]) >= {"a.js", "b.js"} for c in weak))
        self.assertTrue(all(c["parent_result"]["status"] == "pass" for c in weak))
        self.assertTrue(all(c["child_result"]["status"] == "fail" for c in weak))


if __name__ == "__main__":
    unittest.main()
