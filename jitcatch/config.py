from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TestResult:
    status: str  # "pass" | "fail" | "error"
    exit_code: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.status == "pass"


@dataclass
class GeneratedTest:
    name: str
    code: str
    rationale: str = ""


@dataclass
class CatchCandidate:
    workflow: str
    test: GeneratedTest
    risks: List[str] = field(default_factory=list)
    parent_result: Optional[TestResult] = None
    child_result: Optional[TestResult] = None
    judge_tp_prob: float = 0.0
    judge_bucket: str = ""
    judge_rationale: str = ""
    judge_raw: str = ""
    rule_flags: List[str] = field(default_factory=list)
    final_score: float = 0.0
    target_files: List[str] = field(default_factory=list)

    @property
    def is_weak_catch(self) -> bool:
        return (
            self.parent_result is not None
            and self.parent_result.passed
            and self.child_result is not None
            and not self.child_result.passed
        )
