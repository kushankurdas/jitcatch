from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from ..config import CatchCandidate, GeneratedTest, TestResult
from ..llm import LLMClient


_RISK_PREFIX_RE = re.compile(
    r"^\[(?P<file>[^\]:]+)(?::(?P<line>\d+))?\]\s*"
    r"(?:\((?P<cls>[^)]+)\)\s*)?(?P<body>.*)$"
)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def _tokens(s: str) -> Set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(s or "") if len(t) >= 4}


def _risk_key(risk: str) -> str:
    """Stable identifier for a risk so we can tell which risks already
    produced a weak catch and which still need another shot."""
    m = _RISK_PREFIX_RE.match(risk.strip())
    if not m:
        return risk.strip()
    file = (m.group("file") or "").strip()
    line = m.group("line")
    body = (m.group("body") or "").strip()[:80]
    return f"{file}:{line or '?'}:{body}"


def _failure_mode(cand: CatchCandidate) -> str:
    p = cand.parent_result
    c = cand.child_result
    if p is None or c is None:
        return "unknown"
    if p.passed and c.passed:
        return "both_passed"
    if not p.passed and not c.passed:
        return "both_failed"
    if not p.passed and c.passed:
        return "parent_failed"
    return "weak_catch"  # not a gap


def _best_candidate_for_risk(
    risk_key: str, risk: str, candidates: List[CatchCandidate]
) -> Optional[CatchCandidate]:
    """Pick the candidate most likely aimed at this risk. Highest token
    overlap between the risk body and the test name + rationale."""
    r_tokens = _tokens(risk)
    best: Optional[Tuple[int, CatchCandidate]] = None
    for c in candidates:
        if c.is_weak_catch:
            continue  # already caught; ignore
        overlap = len(r_tokens & (_tokens(c.test.name) | _tokens(c.test.rationale)))
        if overlap == 0:
            continue
        if best is None or overlap > best[0]:
            best = (overlap, c)
    return best[1] if best else None


def find_gaps(
    risks: List[str], candidates: List[CatchCandidate]
) -> List[Dict[str, str]]:
    """Return a gap dict per risk that has no weak catch. Each dict
    carries the feedback needed for `llm.retry_tests`."""
    caught: Set[str] = set()
    for c in candidates:
        if not c.is_weak_catch:
            continue
        c_tokens = _tokens(c.test.name) | _tokens(c.test.rationale)
        for r in risks:
            if len(c_tokens & _tokens(r)) >= 2:
                caught.add(_risk_key(r))
    gaps: List[Dict[str, str]] = []
    for r in risks:
        rk = _risk_key(r)
        if rk in caught:
            continue
        prior = _best_candidate_for_risk(rk, r, candidates)
        failure_mode = _failure_mode(prior) if prior else "no_prior"
        fail_output = ""
        if prior and prior.child_result:
            fail_output = (prior.child_result.stdout + "\n" + prior.child_result.stderr).strip()
        gaps.append({
            "risk_key": rk,
            "risk": r,
            "failure_mode": failure_mode,
            "prior_test_name": prior.test.name if prior else "",
            "prior_test_code": prior.test.code if prior else "",
            "failure_output": fail_output,
        })
    return gaps


def run_retry_round(
    llm: LLMClient,
    bundle: str,
    lang: str,
    hints: str,
    gaps: List[Dict[str, str]],
    max_gaps: int = 8,
) -> List[Tuple[str, GeneratedTest]]:
    """Regenerate one follow-up test per gap. Returns (risk_text, test)
    pairs ready for evaluation. Caps at `max_gaps` to bound cost."""
    out: List[Tuple[str, GeneratedTest]] = []
    for gap in gaps[:max_gaps]:
        try:
            tests = llm.retry_tests(bundle=bundle, lang=lang, hints=hints, gap=gap)
        except Exception:  # noqa: BLE001
            tests = []
        for t in tests:
            out.append((gap["risk"], t))
    return out
