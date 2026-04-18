from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .adapters import Adapter, TestArtifact
from .config import GeneratedTest, TestResult


class WorktreeSandbox:
    """Creates two isolated git worktrees (parent + child) for a repo."""

    def __init__(self, repo: Path, parent_rev: str, child_rev: str) -> None:
        self.repo = repo.resolve()
        self.parent_rev = parent_rev
        self.child_rev = child_rev
        self._tmp: Optional[Path] = None
        self.parent_root: Optional[Path] = None
        self.child_root: Optional[Path] = None

    def __enter__(self) -> "WorktreeSandbox":
        self._tmp = Path(tempfile.mkdtemp(prefix="jitcatch_"))
        self.parent_root = self._tmp / "parent"
        self.child_root = self._tmp / "child"
        self._add_worktree(self.parent_root, self.parent_rev)
        self._add_worktree(self.child_root, self.child_rev)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for wt in (self.parent_root, self.child_root):
            if wt and wt.exists():
                self._remove_worktree(wt)
        self._run_git("worktree", "prune")
        if self._tmp and self._tmp.exists():
            shutil.rmtree(self._tmp, ignore_errors=True)

    def _add_worktree(self, path: Path, rev: str) -> None:
        self._run_git("worktree", "add", "--detach", str(path), rev)

    def _remove_worktree(self, path: Path) -> None:
        try:
            self._run_git("worktree", "remove", "--force", str(path))
        except RuntimeError:
            shutil.rmtree(path, ignore_errors=True)

    def _run_git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed: {proc.stderr.strip()}"
            )
        return proc.stdout


def evaluate_test(
    adapter: Adapter,
    sandbox: WorktreeSandbox,
    test: GeneratedTest,
    timeout: int = 60,
) -> tuple[TestResult, TestResult, TestArtifact]:
    """Write generated test into parent & child worktrees, run both. Return (parent, child, artifact)."""
    assert sandbox.parent_root is not None
    assert sandbox.child_root is not None
    parent_art = adapter.write_test(sandbox.parent_root, test.name, test.code)
    child_art = adapter.write_test(sandbox.child_root, test.name, test.code)
    parent_result = adapter.run_test(sandbox.parent_root, parent_art, timeout=timeout)
    child_result = adapter.run_test(sandbox.child_root, child_art, timeout=timeout)
    return parent_result, child_result, child_art
