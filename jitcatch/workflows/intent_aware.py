from __future__ import annotations

from typing import List, Tuple

from ..config import GeneratedTest
from ..llm import LLMClient


def run_intent_aware(
    llm: LLMClient,
    parent_source: str,
    diff: str,
    lang: str,
    hints: str,
) -> Tuple[List[str], List[GeneratedTest]]:
    risks = llm.infer_risks(diff, parent_source, lang)
    tests = llm.generate_tests(
        parent_source=parent_source,
        diff=diff,
        lang=lang,
        hints=hints,
        risks=risks,
        mode="intent",
    )
    return risks, tests
