from __future__ import annotations

from typing import List

from ..config import ReviewFinding
from ..llm import LLMClient


def run_agentic_reviewer(
    llm: LLMClient,
    bundle: str,
    lang: str,
    skip_validator: bool = False,
) -> List[ReviewFinding]:
    """BugBot-style pass: aggressive reviewer flags suspicious diff hunks,
    validator drops obvious FPs. Returns only findings with
    validator_verdict in {'keep', 'downgrade'} (drops are filtered).

    The reviewer is intentionally a separate channel from test-gen — it
    surfaces bugs that test-gen misses because the test can't exercise
    the regression (mocks swallow it, env vars stub out the fallback,
    function is never called in any test)."""
    findings = llm.review_diff(bundle=bundle, lang=lang)
    if not findings:
        return []
    if skip_validator:
        for f in findings:
            if not f.validator_verdict:
                f.validator_verdict = "keep"
        return findings
    validated = llm.validate_findings(findings=findings, bundle=bundle, lang=lang)
    return [f for f in validated if f.validator_verdict != "drop"]
