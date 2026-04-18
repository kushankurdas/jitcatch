from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

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
        if c.target_files:
            lines.append(f"    files:   {', '.join(c.target_files)}")
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


_LANG_BY_EXT = {
    ".js": "javascript",
    ".cjs": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".py": "python",
    ".json": "json",
}


def _lang_hint(path: str) -> str:
    for ext, lang in _LANG_BY_EXT.items():
        if path.endswith(ext):
            return lang
    return ""


def _fail_excerpt(text: str, limit_lines: int = 20) -> str:
    lines = text.strip().splitlines()
    if len(lines) <= limit_lines:
        return "\n".join(lines)
    return "\n".join(lines[:limit_lines] + [f"... ({len(lines) - limit_lines} more lines)"])


def write_markdown(
    candidates: List[CatchCandidate],
    out_path: Path,
    meta: Optional[Dict[str, str]] = None,
    file_diffs: Optional[Dict[str, str]] = None,
) -> None:
    """Human-readable report. `meta` carries run context (command, revs,
    repo). `file_diffs` maps repo-relative path → unified diff text so
    the report can show hunk headers (line numbers) and +/- lines."""
    meta = meta or {}
    file_diffs = file_diffs or {}

    weak = [c for c in candidates if c.is_weak_catch]
    weak.sort(key=lambda c: c.final_score, reverse=True)

    md: List[str] = []
    md.append("# jitcatch report")
    md.append("")

    # Header metadata.
    if meta:
        md.append("| Field | Value |")
        md.append("| --- | --- |")
        for k in ("command", "repo", "parent", "child", "base"):
            v = meta.get(k)
            if v:
                md.append(f"| **{k}** | `{v}` |")
        md.append("")

    total = len(candidates)
    n_weak = len(weak)
    md.append(f"**Generated:** {total} tests &nbsp;•&nbsp; **Weak catches:** {n_weak}")
    md.append("")

    if n_weak == 0:
        md.append("_No weak catches found — no test passed on parent and failed on child._")
        md.append("")
    else:
        md.append("## TL;DR")
        md.append("")
        hit_files: list[str] = []
        for c in weak:
            for f in c.target_files:
                if f not in hit_files:
                    hit_files.append(f)
        md.append(
            f"{n_weak} likely regression{'s' if n_weak != 1 else ''} "
            f"across {len(hit_files)} file{'s' if len(hit_files) != 1 else ''}:"
        )
        for f in hit_files:
            md.append(f"- `{f}`")
        md.append("")

    # Changed files + diffs (with line numbers via @@ hunk headers).
    if file_diffs:
        md.append("## Changed files")
        md.append("")
        for rel, diff in file_diffs.items():
            if not diff.strip():
                continue
            md.append(f"### `{rel}`")
            md.append("")
            # Line-number anchors are in the diff hunk headers already.
            md.append("```diff")
            md.append(diff.rstrip())
            md.append("```")
            md.append("")

    # Weak catches — ranked.
    if weak:
        md.append("## Weak catches (ranked)")
        md.append("")
        for i, c in enumerate(weak, 1):
            md.append(f"### {i}. {c.test.name}")
            md.append("")
            md.append("| Field | Value |")
            md.append("| --- | --- |")
            md.append(f"| **Score** | `{c.final_score:+.2f}` |")
            md.append(f"| **Workflow** | `{c.workflow}` |")
            if c.target_files:
                md.append(f"| **Target files** | {', '.join(f'`{f}`' for f in c.target_files)} |")
            md.append(f"| **Judge** | tp_prob=`{c.judge_tp_prob:+.2f}` bucket=`{c.judge_bucket or '-'}` |")
            if c.rule_flags:
                md.append(f"| **Flags** | {', '.join(f'`{f}`' for f in c.rule_flags)} |")
            md.append("")

            if c.test.rationale:
                md.append("**Why this matters**")
                md.append("")
                md.append(f"> {c.test.rationale.strip()}")
                md.append("")

            if c.judge_rationale:
                md.append("**Judge says**")
                md.append("")
                md.append(f"> {c.judge_rationale.strip()}")
                md.append("")

            if c.risks:
                md.append("**Risks flagged**")
                md.append("")
                for r in c.risks:
                    md.append(f"- {r}")
                md.append("")

            # Per-target-file diff excerpts (line-numbered via @@).
            if c.target_files and file_diffs:
                relevant = [f for f in c.target_files if file_diffs.get(f)]
                if relevant:
                    md.append("**Diff that triggered this**")
                    md.append("")
                    for f in relevant:
                        md.append(f"*{f}*")
                        md.append("")
                        md.append("```diff")
                        md.append(file_diffs[f].rstrip())
                        md.append("```")
                        md.append("")

            # Generated test code.
            md.append("**Test**")
            md.append("")
            md.append(f"```{_lang_hint(c.target_files[0]) if c.target_files else ''}")
            md.append(c.test.code.rstrip())
            md.append("```")
            md.append("")

            # Child failure (what actually broke).
            if c.child_result:
                fail_text = (c.child_result.stdout + "\n" + c.child_result.stderr).strip()
                if fail_text:
                    md.append("**Child failure**")
                    md.append("")
                    md.append("```")
                    md.append(_fail_excerpt(fail_text))
                    md.append("```")
                    md.append("")

    # Also list tests that didn't catch — helpful signal for noise.
    non_weak = [c for c in candidates if not c.is_weak_catch]
    if non_weak:
        md.append("## Generated tests that did not catch (noise check)")
        md.append("")
        md.append("| # | Workflow | Parent | Child | Test |")
        md.append("| --- | --- | --- | --- | --- |")
        for i, c in enumerate(non_weak, 1):
            p = c.parent_result.status if c.parent_result else "-"
            ch = c.child_result.status if c.child_result else "-"
            md.append(f"| {i} | `{c.workflow}` | `{p}` | `{ch}` | {c.test.name} |")
        md.append("")

    out_path.write_text("\n".join(md))
