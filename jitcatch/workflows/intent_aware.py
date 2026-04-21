from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from .. import cache as risk_cache
from ..config import GeneratedTest
from ..llm import LLMClient


def run_intent_aware(
    llm: LLMClient,
    parent_source: str,
    diff: str,
    lang: str,
    hints: str,
    cache_repo: Optional[Path] = None,
    cache_model: str = "",
) -> Tuple[List[str], List[GeneratedTest]]:
    risks = _cached_risks_single(llm, parent_source, diff, lang, cache_repo, cache_model)
    tests = llm.generate_tests(
        parent_source=parent_source,
        diff=diff,
        lang=lang,
        hints=hints,
        risks=risks,
        mode="intent",
    )
    return risks, tests


def run_intent_aware_bundle(
    llm: LLMClient,
    bundle: str,
    lang: str,
    hints: str,
    cache_repo: Optional[Path] = None,
    cache_model: str = "",
) -> Tuple[List[str], List[GeneratedTest]]:
    risks = _cached_risks_bundle(llm, bundle, lang, cache_repo, cache_model)
    tests = llm.generate_tests_bundle(
        bundle=bundle,
        lang=lang,
        hints=hints,
        risks=risks,
        mode="intent",
    )
    return risks, tests


def _cached_risks_bundle(
    llm: LLMClient,
    bundle: str,
    lang: str,
    cache_repo: Optional[Path],
    cache_model: str,
) -> List[str]:
    if cache_repo is None:
        return llm.infer_risks_bundle(bundle, lang)
    key = risk_cache.make_risk_key(bundle, lang, cache_model)
    hit = risk_cache.risk_cache_get(cache_repo, key)
    if hit is not None:
        return hit
    risks = llm.infer_risks_bundle(bundle, lang)
    if risks:
        risk_cache.risk_cache_put(cache_repo, key, risks, cache_model, lang)
    return risks


def _cached_risks_single(
    llm: LLMClient,
    parent_source: str,
    diff: str,
    lang: str,
    cache_repo: Optional[Path],
    cache_model: str,
) -> List[str]:
    if cache_repo is None:
        return llm.infer_risks(diff, parent_source, lang)
    # Key on the concatenation so single-file and bundle paths never
    # collide. Single-file inputs are small enough that hashing both
    # parts is cheap.
    cache_input = f"--DIFF--\n{diff}\n--PARENT--\n{parent_source}"
    key = risk_cache.make_risk_key(cache_input, lang, cache_model)
    hit = risk_cache.risk_cache_get(cache_repo, key)
    if hit is not None:
        return hit
    risks = llm.infer_risks(diff, parent_source, lang)
    if risks:
        risk_cache.risk_cache_put(cache_repo, key, risks, cache_model, lang)
    return risks
