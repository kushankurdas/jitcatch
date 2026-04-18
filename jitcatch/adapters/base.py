from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..config import TestResult


@dataclass
class TestArtifact:
    path: Path  # where test file was written (absolute)
    rel_path: str  # repo-relative


class Adapter(ABC):
    lang: str
    exts: tuple[str, ...] = ()

    def detect(self, source_rel: str) -> bool:
        return source_rel.lower().endswith(self.exts)

    @abstractmethod
    def prompt_hints(self, module_rel: str, repo_root: Path | None = None) -> str: ...

    @abstractmethod
    def write_test(self, repo_root: Path, test_name: str, code: str) -> TestArtifact: ...

    @abstractmethod
    def run_test(self, repo_root: Path, artifact: TestArtifact, timeout: int = 60) -> TestResult: ...


def run_subprocess(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", (e.stderr or "") + "\n[timeout]"
    except FileNotFoundError as e:
        return 127, "", f"command not found: {e}"
