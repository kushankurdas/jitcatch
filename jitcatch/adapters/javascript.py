from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from ..config import TestResult
from .base import Adapter, TestArtifact, run_subprocess


class JavaScriptAdapter(Adapter):
    lang = "javascript"
    exts = (".js", ".mjs", ".cjs")

    def prompt_hints(self, module_rel: str, repo_root: Path | None = None) -> str:
        is_esm = self._is_esm_target(module_rel, repo_root)
        mod = _normalize_relative(module_rel)
        if is_esm:
            import_line = f"import * as mod from '{mod}';"
            ext_note = "the generated test file ends in .mjs and must use ES module syntax"
            header = (
                "  import {{ test }} from 'node:test';\n"
                "  import assert from 'node:assert/strict';\n"
            )
        else:
            import_line = f"const mod = require('{mod}');"
            ext_note = "the generated test file ends in .cjs and must use CommonJS (`require`)"
            header = (
                "  const { test } = require('node:test');\n"
                "  const assert = require('node:assert/strict');\n"
            )
        return (
            "Use node's built-in test runner. "
            "Start the file with:\n"
            + header
            + f"and import the module under test with:\n  {import_line}\n"
            "Each test is `test('name', () => {{ ... }})`. Use `assert.strictEqual` / "
            "`assert.deepStrictEqual`. Tests must be hermetic and deterministic. "
            f"Emit the expected module syntax ({ext_note})."
        )

    def write_test(self, repo_root: Path, test_name: str, code: str, is_esm: bool | None = None) -> TestArtifact:
        safe = _safe_name(test_name)
        if is_esm is None:
            is_esm = _project_is_esm(repo_root)
        rel = f"_jc_test_{safe}.test.{'mjs' if is_esm else 'cjs'}"
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

    def _is_esm_target(self, module_rel: str, repo_root: Path | None) -> bool:
        if module_rel.endswith(".mjs"):
            return True
        if module_rel.endswith(".cjs"):
            return False
        # .js -> depends on package.json "type"
        return _project_is_esm(repo_root)


def _normalize_relative(rel: str) -> str:
    """Return a canonical './...' spec for a repo-relative module path.

    Fix for the double-'./' bug: `pathlib.PurePosixPath` strips redundant
    leading './' segments.
    """
    posix = PurePosixPath(rel.lstrip("/"))
    while posix.parts and posix.parts[0] == ".":
        if len(posix.parts) == 1:
            return "./"
        posix = PurePosixPath(*posix.parts[1:])
    return "./" + str(posix)


def _project_is_esm(repo_root: Path | None) -> bool:
    if repo_root is None:
        return True
    pkg = repo_root / "package.json"
    if not pkg.exists():
        return True
    try:
        data = json.loads(pkg.read_text())
    except (OSError, json.JSONDecodeError):
        return True
    return str(data.get("type", "")).lower() == "module"


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
