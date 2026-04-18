from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def resolve_rev(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", rev).strip()


def get_diff(repo: Path, parent: str, child: str, path: str) -> str:
    return _git(repo, "diff", f"{parent}..{child}", "--", path)


def read_file_at_rev(repo: Path, rev: str, path: str) -> str:
    try:
        return _git(repo, "show", f"{rev}:{path}")
    except GitError:
        return ""


def changed_files(repo: Path, parent: str, child: str) -> List[str]:
    out = _git(repo, "diff", "--name-only", f"{parent}..{child}")
    return [line.strip() for line in out.splitlines() if line.strip()]


def ensure_clean_repo(repo: Path) -> None:
    status = _git(repo, "status", "--porcelain")
    if status.strip():
        raise GitError(
            f"repo {repo} has uncommitted changes; commit or stash first"
        )
