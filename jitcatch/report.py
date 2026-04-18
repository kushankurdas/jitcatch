from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import List

from .config import CatchCandidate


def to_dict(cand: CatchCandidate) -> dict:
    d = asdict(cand)
    d["is_weak_catch"] = cand.is_weak_catch
    return d


def write_json(candidates: List[CatchCandidate], out_path: Path) -> None:
    payload = {
        "summary": {
            "total": len(candidates),
            "weak_catches": sum(1 for c in candidates if c.is_weak_catch),
        },
        "candidates": [to_dict(c) for c in candidates],
    }
    out_path.write_text(json.dumps(payload, indent=2))


def render_text(candidates: List[CatchCandidate]) -> str:
    weak = [c for c in candidates if c.is_weak_catch]
    weak.sort(key=lambda c: c.final_score, reverse=True)
    lines: list[str] = []
    lines.append(f"Total generated: {len(candidates)}")
    lines.append(f"Weak catches:    {len(weak)}")
    lines.append("")
    if not weak:
        lines.append("No weak catches found.")
        return "\n".join(lines)
    lines.append("=" * 70)
    lines.append("RANKED WEAK CATCHES (higher score = likelier true regression)")
    lines.append("=" * 70)
    for i, c in enumerate(weak, 1):
        lines.append(f"\n#{i}  score={c.final_score:+.2f}  workflow={c.workflow}")
        lines.append(f"    test:    {c.test.name}")
        lines.append(f"    judge:   tp_prob={c.judge_tp_prob:+.2f}  bucket={c.judge_bucket}")
        if c.judge_rationale:
            lines.append(f"    why:     {c.judge_rationale[:200]}")
        if c.rule_flags:
            lines.append(f"    flags:   {', '.join(c.rule_flags)}")
        if c.risks:
            lines.append(f"    risks:   {', '.join(c.risks[:3])}")
        if c.child_result:
            snippet = (c.child_result.stdout + c.child_result.stderr).strip().splitlines()[:6]
            if snippet:
                lines.append("    child failure:")
                for ln in snippet:
                    lines.append(f"      | {ln}")
    return "\n".join(lines)
