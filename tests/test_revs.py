from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jitcatch import revs  # noqa: E402


def _git(cwd: Path, *args: str, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "jitcatch")
    env.setdefault("GIT_AUTHOR_EMAIL", "jc@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "jitcatch")
    env.setdefault("GIT_COMMITTER_EMAIL", "jc@example.com")
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=env,
        capture_output=True,
        text=True,
        input=input_text,
        check=check,
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "commit.gpgsign", "false")


def _commit(root: Path, path: str, content: str, msg: str) -> str:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(content)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


class RevsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jc_revs_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _new_repo(self) -> Path:
        repo = self.tmp / "r"
        _init_repo(repo)
        return repo

    def test_resolve_last(self) -> None:
        repo = self._new_repo()
        parent = _commit(repo, "a.txt", "one\n", "p")
        child = _commit(repo, "a.txt", "two\n", "c")
        with revs.resolve_last(repo) as pair:
            self.assertEqual(pair.parent, parent)
            self.assertEqual(pair.child, child)

    def test_resolve_pr_explicit_base(self) -> None:
        repo = self._new_repo()
        base = _commit(repo, "a.txt", "one\n", "p")
        _git(repo, "branch", "feature")
        _commit(repo, "b.txt", "two\n", "main advance")  # main moves
        _git(repo, "checkout", "-q", "feature")
        feat_head = _commit(repo, "a.txt", "two\n", "feature work")
        with revs.resolve_pr(repo, base="main") as pair:
            self.assertEqual(pair.parent, base)
            self.assertEqual(pair.child, feat_head)

    def test_resolve_pr_autodetect_via_symbolic_ref(self) -> None:
        repo = self._new_repo()
        base = _commit(repo, "a.txt", "one\n", "p")
        _commit(repo, "a.txt", "two\n", "c")
        # simulate origin/main pointing at base, origin/HEAD -> origin/main
        _git(repo, "update-ref", "refs/remotes/origin/main", base)
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        with revs.resolve_pr(repo) as pair:
            self.assertEqual(pair.parent, base)

    def test_resolve_staged_preserves_user_state(self) -> None:
        repo = self._new_repo()
        _commit(repo, "a.txt", "parent\n", "p")
        (repo / "a.txt").write_text("child\n")
        _git(repo, "add", "a.txt")
        status_before = _git(repo, "status", "--porcelain").stdout
        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

        with revs.resolve_staged(repo) as pair:
            # child commit should contain the staged content
            shown = _git(repo, "show", f"{pair.child}:a.txt").stdout
            self.assertEqual(shown, "child\n")
            self.assertNotEqual(pair.child, pair.parent)

        # user's index + working tree unchanged
        self.assertEqual(_git(repo, "status", "--porcelain").stdout, status_before)
        self.assertEqual(_git(repo, "rev-parse", "HEAD").stdout.strip(), head_before)
        # scratch worktree cleaned up
        wtlist = _git(repo, "worktree", "list").stdout
        self.assertNotIn("jitcatch_scratch_", wtlist)

    def test_resolve_working_preserves_user_state(self) -> None:
        repo = self._new_repo()
        _commit(repo, "a.txt", "parent\n", "p")
        (repo / "a.txt").write_text("wc\n")  # unstaged
        status_before = _git(repo, "status", "--porcelain").stdout
        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

        with revs.resolve_working(repo) as pair:
            shown = _git(repo, "show", f"{pair.child}:a.txt").stdout
            self.assertEqual(shown, "wc\n")
            self.assertNotEqual(pair.child, pair.parent)

        self.assertEqual(_git(repo, "status", "--porcelain").stdout, status_before)
        self.assertEqual(_git(repo, "rev-parse", "HEAD").stdout.strip(), head_before)
        wtlist = _git(repo, "worktree", "list").stdout
        self.assertNotIn("jitcatch_scratch_", wtlist)

    def test_staged_raises_when_nothing_staged(self) -> None:
        repo = self._new_repo()
        _commit(repo, "a.txt", "parent\n", "p")
        with self.assertRaises(revs.RevError):
            revs.resolve_staged(repo)

    def test_default_branch_detection_failure(self) -> None:
        repo = self._new_repo()
        _commit(repo, "a.txt", "p\n", "p")
        with self.assertRaises(revs.RevError):
            revs.detect_default_branch(repo)


if __name__ == "__main__":
    unittest.main()
