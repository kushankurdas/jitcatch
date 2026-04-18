from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import CatchCandidate


_RISK_PREFIX_RE = re.compile(
    r"^\[(?P<file>[^\]:]+)(?::(?P<line>\d+))?\]\s*"
    r"(?:\((?P<cls>[^)]+)\)\s*)?(?P<body>.*)$"
)


def _severity_from_score(score: float) -> str:
    if score >= 0.90:
        return "High"
    if score >= 0.50:
        return "Medium"
    if score >= 0.00:
        return "Low"
    return ""


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def _tokens(s: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(s) if len(t) >= 4}


def _risk_meta_for(cand: CatchCandidate) -> Tuple[Optional[str], Optional[int], str, str]:
    """Pick the risk that best matches this test by identifier-token overlap
    with the test name. Bundle workflow lumps every changed file into
    `target_files`, so file-level matching is too coarse — we need to
    disambiguate by function/symbol name carried in the test name.
    Returns (file, line, class, one_liner)."""
    name_tokens = _tokens(cand.test.name)
    parsed: List[Tuple[str, Optional[int], str, str]] = []
    for r in cand.risks:
        if not isinstance(r, str):
            continue
        m = _RISK_PREFIX_RE.match(r.strip())
        if not m:
            parsed.append(("", None, "", r.strip()))
            continue
        file = (m.group("file") or "").strip()
        line = int(m.group("line")) if m.group("line") else None
        cls = (m.group("cls") or "").strip()
        body = (m.group("body") or "").strip() or r.strip()
        parsed.append((file, line, cls, body))
    # Score each parsed risk by token overlap with the test name.
    best: Optional[Tuple[int, Tuple[str, Optional[int], str, str]]] = None
    for entry in parsed:
        _file, _line, _cls, body = entry
        overlap = len(name_tokens & _tokens(body))
        if best is None or overlap > best[0]:
            best = (overlap, entry)
    if best and best[0] > 0:
        file, line, cls, body = best[1]
        return (file or None), line, cls, body
    if parsed:
        file, line, cls, body = parsed[0]
        return (file or None), line, cls, body
    rat = (cand.judge_rationale or "").strip()
    return None, None, "", rat[:160]


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


_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _split_hunks(diff: str) -> List[Tuple[int, int, str]]:
    """Split unified diff into (new_start, new_len, body) triples. `body`
    includes the `@@` header line. Non-hunk preamble is dropped."""
    hunks: List[Tuple[int, int, str]] = []
    buf: List[str] = []
    start = 0
    length = 0
    in_hunk = False
    for ln in diff.splitlines():
        m = _HUNK_HEADER_RE.match(ln)
        if m:
            if in_hunk:
                hunks.append((start, length, "\n".join(buf)))
            start = int(m.group(1))
            length = int(m.group(2) or 1)
            buf = [ln]
            in_hunk = True
        elif in_hunk:
            buf.append(ln)
    if in_hunk and buf:
        hunks.append((start, length, "\n".join(buf)))
    return hunks


def _hunk_around(diff: str, line: Optional[int]) -> str:
    """Return only the hunk covering `line` in the new file. Falls back
    to the closest hunk, or full diff when no line is known."""
    if not diff.strip():
        return diff
    if line is None:
        return diff
    hunks = _split_hunks(diff)
    if not hunks:
        return diff
    for start, length, body in hunks:
        if start <= line <= start + max(length, 1):
            return body
    closest = min(hunks, key=lambda h: abs(h[0] - line))
    return closest[2]


def _best_hunk_by_tokens(diff: str, tokens: set[str]) -> str:
    """When no risk line is known, pick the hunk whose body shares the
    most identifier tokens with the test. Keeps bundle-workflow catches
    from dumping every hunk in the file."""
    if not diff.strip() or not tokens:
        return diff
    hunks = _split_hunks(diff)
    if len(hunks) <= 1:
        return diff
    best_body = hunks[0][2]
    best_overlap = -1
    for _start, _len, body in hunks:
        overlap = len(tokens & _tokens(body))
        if overlap > best_overlap:
            best_overlap = overlap
            best_body = body
    return best_body


def _best_diff_file(
    cand: CatchCandidate, file_diffs: Dict[str, str]
) -> Optional[str]:
    """When the risk parser couldn't nail a file, score each target_file's
    diff body against tokens from the test identity (name + rationale +
    code). Bundle workflow lumps every changed file into `target_files`,
    so unscored we'd dump 11 file diffs per catch."""
    tokens = _tokens(cand.test.name) | _tokens(cand.test.rationale or "")
    tokens |= _tokens(cand.test.code or "")
    candidates = [f for f in cand.target_files if file_diffs.get(f)]
    if not candidates:
        return None
    if not tokens:
        return candidates[0]
    best = candidates[0]
    best_overlap = -1
    for f in candidates:
        overlap = len(tokens & _tokens(file_diffs[f]))
        if overlap > best_overlap:
            best_overlap = overlap
            best = f
    return best


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
        # Review summary — severity-tagged one-liners above everything else.
        # Group by (risk_file, line) so each unique bug gets one row, even
        # when the bundle workflow lumps every changed file into target_files.
        groups: Dict[Tuple[Optional[str], Optional[int]], List[CatchCandidate]] = {}
        order: List[Tuple[Optional[str], Optional[int]]] = []
        meta_cache: Dict[int, Tuple[Optional[str], Optional[int], str, str]] = {}
        for c in weak:
            file, line, cls, body = _risk_meta_for(c)
            meta_cache[id(c)] = (file, line, cls, body)
            key = (file, line)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(c)

        md.append("## Review summary")
        md.append("")
        md.append("| Severity | File | Class | Risk | Score | |")
        md.append("| --- | --- | --- | --- | --- | --- |")
        for key in order:
            cands = groups[key]
            # Representative = highest-scoring candidate in the group.
            cands.sort(key=lambda c: c.final_score, reverse=True)
            rep = cands[0]
            file, line, cls, body = meta_cache[id(rep)]
            severity = _severity_from_score(rep.final_score)
            if not severity:
                continue  # skip negative-score rows from the summary
            target = file or (rep.target_files[0] if rep.target_files else "-")
            file_cell = f"`{target}:{line}`" if line else f"`{target}`"
            cls_cell = f"`{cls}`" if cls else "-"
            risk_cell = body.replace("|", "\\|")[:160] or rep.test.name
            score_cell = f"`{rep.final_score:+.2f}`"
            badge = f"×{len(cands)}" if len(cands) > 1 else ""
            md.append(
                f"| **{severity}** | {file_cell} | {cls_cell} | {risk_cell} "
                f"| {score_cell} | {badge} |"
            )
        md.append("")

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
        md.append("<details>")
        md.append("<summary><strong>Changed files</strong> — diffs with line numbers</summary>")
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
        md.append("</details>")
        md.append("")

    # Weak catches — ranked.
    if weak:
        md.append("<details>")
        md.append("<summary><strong>Weak catches (ranked)</strong> — full tests, judge rationales, child failure logs</summary>")
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

            # Per-target-file diff excerpt — narrow to a single file and
            # a single hunk. Risk file+line wins when available; otherwise
            # score target_files by token overlap with the test identity.
            if c.target_files and file_diffs:
                risk_file, risk_line, _, _ = meta_cache.get(
                    id(c), (None, None, "", "")
                )
                focus_file: Optional[str] = None
                focus_line: Optional[int] = None
                if risk_file and file_diffs.get(risk_file):
                    focus_file = risk_file
                    focus_line = risk_line
                else:
                    focus_file = _best_diff_file(c, file_diffs)
                if focus_file:
                    diff = file_diffs[focus_file]
                    if focus_line is not None:
                        body = _hunk_around(diff, focus_line)
                    else:
                        tokens = _tokens(c.test.name) | _tokens(
                            c.test.rationale or ""
                        )
                        body = _best_hunk_by_tokens(diff, tokens)
                    md.append("**Diff that triggered this**")
                    md.append("")
                    md.append(f"*{focus_file}*")
                    md.append("")
                    md.append("```diff")
                    md.append(body.rstrip())
                    md.append("```")
                    md.append("")

            # Generated test code.
            md.append("**Test**")
            md.append("")
            md.append(f"```{_lang_hint(c.target_files[0]) if c.target_files else ''}")
            md.append(c.test.code.rstrip())
            md.append("```")
            md.append("")

        md.append("</details>")
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
