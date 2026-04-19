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


def _file_link(path: str, line: Optional[int], meta: Dict[str, str]) -> str:
    """Build a local file:// link for a repo-relative path. Label is always
    just the path (CLI users read by path, not path:line). Line anchor is
    appended to the URL when known; most markdown previews that honor
    file:// ignore it anyway, but editors like VS Code pick it up."""
    label_md = f"`{path}`"
    repo = meta.get("repo")
    if not repo:
        return label_md
    url = f"file://{repo.rstrip('/')}/{path}"
    if line:
        url += f"#L{line}"
    return f"[{label_md}]({url})"


def _severity_from_score(score: float) -> str:
    if score >= 0.95:
        return "Critical"
    if score >= 0.80:
        return "High"
    if score >= 0.50:
        return "Medium"
    if score >= 0.20:
        return "Low"
    if score >= 0.00:
        return "Trivial"
    return "Info"


_SEVERITY_BADGES = {
    "Critical": "🔴",
    "High": "🟠",
    "Medium": "🟡",
    "Low": "🟢",
    "Trivial": "⚪",
    "Info": "🔵",
}


def _severity_md(severity: str) -> str:
    """Colored severity label via emoji — renders in every markdown viewer
    (GitHub, VS Code, plain text), unlike inline HTML styles which most
    previewers strip."""
    if not severity:
        return "-"
    badge = _SEVERITY_BADGES.get(severity, "")
    return f"{badge} **{severity}**".strip()


def _pretty_flag(flag: str) -> str:
    """`tp:value_mismatch` -> `Value Mismatch`. Strips the tp:/fp: prefix
    and title-cases the remainder so readers don't need to decode our
    internal taxonomy."""
    token = flag.split(":", 1)[1] if ":" in flag else flag
    return token.replace("_", " ").title()


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


def _dedup_key(cand: CatchCandidate) -> Tuple:
    """Collapse weak catches that describe the same regression. Key on the
    risk file only — line numbers drift between tests that catch the same
    mutation (one test omits the line, another picks the + side), so
    keying on line splits duplicates. Falls back to sorted target_files
    when risks didn't parse a file."""
    file, _line, _cls, _body = _risk_meta_for(cand)
    if file:
        return ("risk", file)
    return ("target", tuple(sorted(cand.target_files)))


def _dedup_weak(weak: List[CatchCandidate]) -> Tuple[List[CatchCandidate], Dict[int, int]]:
    """Keep the highest-scoring candidate per dedup key, preserving the
    input order. Returns (deduped_list, {id(rep) -> group_size})."""
    groups: Dict[Tuple, List[CatchCandidate]] = {}
    order: List[Tuple] = []
    for c in weak:
        k = _dedup_key(c)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(c)
    deduped: List[CatchCandidate] = []
    sizes: Dict[int, int] = {}
    for k in order:
        cands = sorted(groups[k], key=lambda c: c.final_score, reverse=True)
        rep = cands[0]
        deduped.append(rep)
        sizes[id(rep)] = len(cands)
    deduped.sort(key=lambda c: c.final_score, reverse=True)
    return deduped, sizes


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
    weak_all = [c for c in candidates if c.is_weak_catch]
    weak, sizes = _dedup_weak(weak_all)
    lines: list[str] = []
    lines.append(f"Total generated: {len(candidates)}")
    lines.append(f"Weak catches:    {len(weak)}"
                 + (f" (deduped from {len(weak_all)})" if len(weak) != len(weak_all) else ""))
    lines.append("")
    if not weak:
        lines.append("No weak catches found.")
        return "\n".join(lines)
    lines.append("=" * 70)
    lines.append("RANKED WEAK CATCHES (higher score = likelier true regression)")
    lines.append("=" * 70)
    for i, c in enumerate(weak, 1):
        dupe = sizes.get(id(c), 1)
        extra = f"  ×{dupe}" if dupe > 1 else ""
        lines.append(f"\n#{i}  score={c.final_score:+.2f}  workflow={c.workflow}{extra}")
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


_DIFF_PREAMBLE_PREFIXES = ("diff --git ", "index ", "--- ", "+++ ", "new file mode", "deleted file mode", "similarity index", "rename ")


def _strip_hunk_header(body: str) -> str:
    """Drop the `diff --git` / `index` / `---` / `+++` preamble and the
    leading `@@ -a,b +c,d @@` line. The file link above the block carries
    the line-number context; headers are visual noise."""
    out: List[str] = []
    for ln in body.splitlines():
        if ln.startswith(_DIFF_PREAMBLE_PREFIXES):
            continue
        if _HUNK_HEADER_RE.match(ln):
            continue
        out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


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

    weak_all = [c for c in candidates if c.is_weak_catch]
    weak, _ = _dedup_weak(weak_all)

    md: List[str] = []
    md.append("# JitCatch Report")
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

    # total = len(candidates)
    n_weak = len(weak)
    # md.append(f"**Generated:** {total} tests &nbsp;•&nbsp; **Weak catches:** {n_weak}")
    # md.append("")

    if n_weak == 0:
        md.append("_No weak catches found — no test passed on parent and failed on child._")
        md.append("")
    else:
        meta_cache: Dict[int, Tuple[Optional[str], Optional[int], str, str]] = {
            id(c): _risk_meta_for(c) for c in weak
        }

        # md.append("## Findings")
        # md.append("")
        # hit_files: list[str] = []
        # for c in weak:
        #     for f in c.target_files:
        #         if f not in hit_files:
        #             hit_files.append(f)
        # md.append(
        #     f"{n_weak} likely regression{'s' if n_weak != 1 else ''} "
        #     f"across {len(hit_files)} file{'s' if len(hit_files) != 1 else ''}:"
        # )
        # for f in hit_files:
        #     md.append(f"- {_file_link(f, None, meta)}")
        # md.append("")

    # Weak catches — ranked.
    if weak:
        md.append("<details>")
        # md.append("<summary><strong>Weak catches (ranked)</strong> — full tests, judge rationales, child failure logs</summary>")
        md.append("")
        for i, c in enumerate(weak, 1):
            md.append(f"### {i}. {c.test.name}")
            md.append("")

            risk_file, risk_line, _risk_cls, risk_body = meta_cache.get(
                id(c), (None, None, "", "")
            )

            # Diff that triggered this — filename is a clickable link into
            # source at the child rev (GitHub blob when available).
            if c.target_files and file_diffs:
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
                    md.append(_file_link(focus_file, focus_line, meta))
                    md.append("")
                    md.append("```diff")
                    md.append(_strip_hunk_header(body))
                    md.append("```")
                    md.append("")
                    severity = _severity_from_score(c.final_score) or "-"
                    flags_text = (
                        ", ".join(_pretty_flag(f) for f in c.rule_flags)
                        if c.rule_flags else "-"
                    )
                    md.append(
                        f"**Score:** `{c.final_score:+.2f}` &nbsp;•&nbsp; "
                        f"**Severity:** {_severity_md(severity)} &nbsp;•&nbsp; "
                        f"**Flags:** {flags_text}"
                    )
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

            # Generated test code.
            md.append("**Unit Test**")
            md.append("")
            md.append(f"```{_lang_hint(c.target_files[0]) if c.target_files else ''}")
            md.append(c.test.code.rstrip())
            md.append("```")
            md.append("")

        md.append("</details>")
        md.append("")

    out_path.write_text("\n".join(md))
