from __future__ import annotations

import json
from pathlib import Path

from ..config import TestResult
from .base import Adapter, TestArtifact, run_subprocess


class JavaScriptAdapter(Adapter):
    lang = "javascript"
    exts = (".js", ".mjs", ".cjs")

    def prompt_hints(self, module_rel: str) -> str:
        is_esm = self._looks_esm(module_rel)
        mod = "./" + module_rel
        if is_esm:
            import_line = f"import * as mod from '{mod}';"
        else:
            import_line = f"const mod = require('{mod}');"
        return (
            "Use node's built-in test runner. "
            "Start the file with:\n"
            "  import {{ test }} from 'node:test';\n"
            "  import assert from 'node:assert/strict';\n"
            f"and import the module under test with:\n  {import_line}\n"
            "Each test is `test('name', () => {{ ... }})`. Use `assert.strictEqual` / "
            "`assert.deepStrictEqual`. Tests must be hermetic and deterministic. "
            "Emit ES module syntax (the generated test file ends in .mjs)."
        )

    def write_test(self, repo_root: Path, test_name: str, code: str) -> TestArtifact:
        safe = _safe_name(test_name)
        rel = f"_jc_test_{safe}.test.mjs"
        path = repo_root / rel
        path.write_text(code)
        return TestArtifact(path=path, rel_path=rel)

    def run_test(self, repo_root: Path, artifact: TestArtifact, timeout: int = 60) -> TestResult:
        code, out, err = run_subprocess(
            ["node", "--test", artifact.rel_path],
            cwd=repo_root,
            timeout=timeout,
        )
        if code == 0:
            status = "pass"
        elif code == 1:
            status = "fail"
        else:
            status = "error"
        return TestResult(status=status, exit_code=code, stdout=out, stderr=err)

    def _looks_esm(self, module_rel: str) -> bool:
        if module_rel.endswith(".mjs"):
            return True
        if module_rel.endswith(".cjs"):
            return False
        return True


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)[:60] or "t"


def detect_runner(repo_root: Path) -> str:
    pkg = repo_root / "package.json"
    if not pkg.exists():
        return "node"
    try:
        data = json.loads(pkg.read_text())
    except json.JSONDecodeError:
        return "node"
    deps = {}
    for key in ("dependencies", "devDependencies"):
        deps.update(data.get(key, {}) or {})
    if "vitest" in deps:
        return "vitest"
    if "jest" in deps:
        return "jest"
    return "node"
