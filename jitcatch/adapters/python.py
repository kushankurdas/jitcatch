from __future__ import annotations

import sys
from pathlib import Path

from ..config import TestResult
from .base import Adapter, TestArtifact, run_subprocess


class PythonAdapter(Adapter):
    lang = "python"
    exts = (".py",)

    def prompt_hints(self, module_rel: str) -> str:
        module = module_rel.replace("/", ".").removesuffix(".py")
        return (
            f"Use pytest. Import the module under test via `from {module} import ...` "
            f"or `import {module}`. Each test is a top-level function named "
            f"`test_<descriptive>`. Use plain `assert` statements. Do not patch or "
            f"mock unless strictly necessary. Keep tests hermetic and deterministic."
        )

    def write_test(self, repo_root: Path, test_name: str, code: str) -> TestArtifact:
        safe = _safe_name(test_name)
        rel = f"_jc_test_{safe}.py"
        path = repo_root / rel
        path.write_text(code)
        return TestArtifact(path=path, rel_path=rel)

    def run_test(self, repo_root: Path, artifact: TestArtifact, timeout: int = 60) -> TestResult:
        code, out, err = run_subprocess(
            [sys.executable, "-m", "pytest", artifact.rel_path, "-q", "--no-header", "-p", "no:cacheprovider"],
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


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)[:60] or "t"
