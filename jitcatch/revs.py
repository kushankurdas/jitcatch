"""Automated (parent_rev, child_rev) resolvers for jitcatch.

Each resolver returns a `RevPair` and, optionally, manages a scratch
worktree that must be cleaned up via `RevPair.close()`. Use them
through the `resolve` dispatcher or the convenience functions.

Resolvers:
- last    -> (HEAD~1, HEAD)
- pr      -> (merge_base(base_or_default, HEAD), HEAD)
- staged  -> (HEAD, scratch commit of `git diff --cached`)
- working -> (HEAD, scratch commit of working tree)

`staged` and `working` create a detached scratch worktree at HEAD,
apply the user's patch there, commit it, and return that worktree
head as the child rev. The user's index and working tree are never
touched. `close()` removes the scratch worktree.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


class RevError(RuntimeError):
    pass


@dataclass
class RevPair:
    parent: str
    child: str
    description: str = ""
    _cleanup: Optional["_ScratchWorktree"] = field(default=None, repr=False)

    def close(self) -> None:
        if self._cleanup is not None:
            self._cleanup.cleanup()
            self._cleanup = None

    def __enter__(self) -> "RevPair":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise RevError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def resolve_last(repo: Path) -> RevPair:
    parent = _git(repo, "rev-parse", "HEAD~1").strip()
    child = _git(repo, "rev-parse", "HEAD").strip()
    return RevPair(parent=parent, child=child, description="HEAD~1..HEAD")


def resolve_pr(repo: Path, base: Optional[str] = None) -> RevPair:
    ref = base if base else detect_default_branch(repo)
    try:
        merge_base = _git(repo, "merge-base", ref, "HEAD").strip()
    except RevError as e:
        raise RevError(f"could not find merge-base between {ref} and HEAD: {e}") from e
    child = _git(repo, "rev-parse", "HEAD").strip()
    return RevPair(parent=merge_base, child=child, description=f"merge-base({ref}, HEAD)..HEAD")


def resolve_staged(repo: Path) -> RevPair:
    parent = _git(repo, "rev-parse", "HEAD").strip()
    patch = _git(repo, "diff", "--cached", "--binary")
    if not patch.strip():
        raise RevError("no staged changes (git diff --cached is empty)")
    scratch = _ScratchWorktree(repo, parent)
    scratch.setup()
    try:
        scratch.apply_patch(patch, staged=True)
        child = scratch.commit("jitcatch: staged")
    except Exception:
        scratch.cleanup()
        raise
    return RevPair(parent=parent, child=child, description="HEAD..<staged>", _cleanup=scratch)


def resolve_working(repo: Path) -> RevPair:
    parent = _git(repo, "rev-parse", "HEAD").strip()
    staged = _git(repo, "diff", "--cached", "--binary")
    unstaged = _git(repo, "diff", "--binary")
    if not staged.strip() and not unstaged.strip():
        raise RevError("no working-tree changes (staged or unstaged)")
    scratch = _ScratchWorktree(repo, parent)
    scratch.setup()
    try:
        if staged.strip():
            scratch.apply_patch(staged, staged=False)
        if unstaged.strip():
            scratch.apply_patch(unstaged, staged=False)
        child = scratch.commit("jitcatch: working")
    except Exception:
        scratch.cleanup()
        raise
    return RevPair(parent=parent, child=child, description="HEAD..<working>", _cleanup=scratch)


def resolve(repo: Path, mode: str, base: Optional[str] = None) -> RevPair:
    if mode == "last":
        return resolve_last(repo)
    if mode == "pr":
        return resolve_pr(repo, base=base)
    if mode == "staged":
        return resolve_staged(repo)
    if mode == "working":
        return resolve_working(repo)
    raise RevError(f"unknown rev mode: {mode}")


def detect_default_branch(repo: Path) -> str:
    """Best-effort remote default branch.

    Tries `origin/HEAD` symbolic-ref first, then falls back to
    origin/main, origin/master, origin/develop. Raises if none
    resolve.
    """
    out = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", check=False).strip()
    if out:
        # e.g. "refs/remotes/origin/main"
        name = out.rsplit("/", 1)[-1]
        return f"origin/{name}"
    for candidate in ("origin/main", "origin/master", "origin/develop"):
        probe = _git(repo, "rev-parse", "--verify", "--quiet", candidate, check=False).strip()
        if probe:
            return candidate
    raise RevError(
        "could not detect default branch; pass --base explicitly "
        "(e.g. --base origin/main)"
    )


class _ScratchWorktree:
    """A detached worktree at <rev> used for synthesizing throwaway child commits.

    We add a detached worktree at `parent_rev`, apply the user's
    patch (staged or working-tree), and commit it. The resulting
    commit SHA is the synthetic child. On cleanup we remove the
    worktree and prune.

    The user's repo state (index + working tree) is never mutated,
    because git worktree add is strictly additive.
    """

    def __init__(self, repo: Path, parent_rev: str) -> None:
        self.repo = repo.resolve()
        self.parent_rev = parent_rev
        self.tmp: Optional[Path] = None
        self.worktree: Optional[Path] = None

    def setup(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jitcatch_scratch_"))
        self.worktree = self.tmp / "w"
        _git(self.repo, "worktree", "add", "--detach", str(self.worktree), self.parent_rev)

    def apply_patch(self, patch: str, staged: bool) -> None:
        assert self.worktree is not None
        # git apply reads patch from stdin via `-`. We use --index when
        # we want the apply to stage (so a subsequent commit captures it).
        args = ["apply", "--index" if staged else "--index", "--whitespace=nowarn", "-"]
        # Both staged and working-tree apply go through --index so the
        # follow-up commit picks up the changes. (We commit from the
        # scratch worktree directly, so index = the scratch one.)
        proc = subprocess.run(
            ["git", "-C", str(self.worktree), *args],
            input=patch,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RevError(f"git apply failed in scratch worktree: {proc.stderr.strip()}")

    def commit(self, message: str) -> str:
        assert self.worktree is not None
        env = dict(os.environ)
        env.setdefault("GIT_AUTHOR_NAME", "jitcatch")
        env.setdefault("GIT_AUTHOR_EMAIL", "jitcatch@example.com")
        env.setdefault("GIT_COMMITTER_NAME", "jitcatch")
        env.setdefault("GIT_COMMITTER_EMAIL", "jitcatch@example.com")
        # Include untracked files the patch may have added.
        subprocess.run(
            ["git", "-C", str(self.worktree), "add", "-A"],
            check=True,
            env=env,
            capture_output=True,
        )
        proc = subprocess.run(
            ["git", "-C", str(self.worktree), "commit", "-q", "--no-gpg-sign", "-m", message],
            env=env,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RevError(f"git commit failed in scratch worktree: {proc.stderr.strip()}")
        sha = _git(self.worktree, "rev-parse", "HEAD").strip()
        return sha

    def cleanup(self) -> None:
        if self.worktree and self.worktree.exists():
            subprocess.run(
                ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(self.worktree)],
                capture_output=True,
            )
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "prune"],
            capture_output=True,
        )
        if self.tmp and self.tmp.exists():
            import shutil
            shutil.rmtree(self.tmp, ignore_errors=True)
        self.worktree = None
        self.tmp = None


__all__ = [
    "RevError",
    "RevPair",
    "detect_default_branch",
    "resolve",
    "resolve_last",
    "resolve_pr",
    "resolve_staged",
    "resolve_working",
]
