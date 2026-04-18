from __future__ import annotations

from typing import List

from ..config import GeneratedTest
from ..llm import LLMClient


def run_dodgy_diff(
    llm: LLMClient,
    parent_source: str,
    diff: str,
    lang: str,
    hints: str,
) -> List[GeneratedTest]:
    return llm.generate_tests(
        parent_source=parent_source,
        diff=diff,
        lang=lang,
        hints=hints,
        risks=None,
        mode="dodgy",
    )


def run_dodgy_diff_bundle(
    llm: LLMClient,
    bundle: str,
    lang: str,
    hints: str,
) -> List[GeneratedTest]:
    return llm.generate_tests_bundle(
        bundle=bundle,
        lang=lang,
        hints=hints,
        risks=None,
        mode="dodgy",
    )
