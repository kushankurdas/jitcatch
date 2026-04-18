from __future__ import annotations

from ..config import CatchCandidate
from ..llm import LLMClient


def judge_candidate(
    llm: LLMClient,
    cand: CatchCandidate,
    parent_source: str,
    diff: str,
    lang: str,
) -> None:
    """Mutates cand with judge scores."""
    failure = ""
    if cand.child_result is not None:
        failure = (cand.child_result.stdout + "\n" + cand.child_result.stderr).strip()
    verdict = llm.judge(
        test_code=cand.test.code,
        parent_source=parent_source,
        diff=diff,
        failure=failure,
        lang=lang,
    )
    cand.judge_tp_prob = float(verdict.get("tp_prob", 0.0))
    cand.judge_bucket = str(verdict.get("bucket", "Low"))
    cand.judge_rationale = str(verdict.get("rationale", ""))
