from __future__ import annotations

import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
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
        self._link_node_modules(path)

    def _link_node_modules(self, worktree: Path) -> None:
        src = self.repo / "node_modules"
        if not src.exists():
            return
        dst = worktree / "node_modules"
        if dst.exists() or dst.is_symlink():
            return
        try:
            dst.symlink_to(src, target_is_directory=True)
        except OSError:
            pass

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


def rerun_child(
    adapter: Adapter,
    sandbox: WorktreeSandbox,
    artifact: TestArtifact,
    timeout: int,
    n: int,
) -> list[TestResult]:
    """Re-run the child-worktree test `n` times against the existing
    artifact. Used by the runtime flake detector: a test that fails on
    one run but passes on another is non-deterministic and should be
    flagged `fp:flake_runtime` — not a real regression catch."""
    assert sandbox.child_root is not None
    results: list[TestResult] = []
    for _ in range(max(0, n)):
        results.append(adapter.run_test(sandbox.child_root, artifact, timeout=timeout))
    return results


def evaluate_test(
    adapter: Adapter,
    sandbox: WorktreeSandbox,
    test: GeneratedTest,
    timeout: int = 60,
) -> tuple[TestResult, TestResult, TestArtifact]:
    """Write generated test into parent & child worktrees, run both in
    parallel. Return (parent, child, artifact). Parent and child subprocesses
    execute in separate worktree directories, so there is no shared filesystem
    state between them — running both concurrently halves wall-clock time
    on tests where both revs take non-trivial time to execute."""
    assert sandbox.parent_root is not None
    assert sandbox.child_root is not None
    parent_art = adapter.write_test(sandbox.parent_root, test.name, test.code)
    child_art = adapter.write_test(sandbox.child_root, test.name, test.code)
    with ThreadPoolExecutor(max_workers=2) as pool:
        parent_fut = pool.submit(
            adapter.run_test, sandbox.parent_root, parent_art, timeout=timeout
        )
        child_fut = pool.submit(
            adapter.run_test, sandbox.child_root, child_art, timeout=timeout
        )
        parent_result = parent_fut.result()
        child_result = child_fut.result()
    return parent_result, child_result, child_art
