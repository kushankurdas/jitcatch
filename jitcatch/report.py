from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import CatchCandidate, ReviewFinding


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

# Domain-generic words that appear in most test names / finding titles.
# Filtered out of token sets used for cross-matching so two unrelated bugs
# don't collide on words like "default" or "from".
_STOP_TOKENS = {
    "from", "when", "should", "test", "tests", "verify", "verifies",
    "parent", "child", "diff", "code", "codes", "with", "this", "that",
    "into", "value", "values", "call", "calls", "does", "change", "changed",
    "changes", "default", "defaults", "onto", "only", "would", "allow",
    "allows", "through", "behaviour", "behavior", "return", "returns",
    "passes", "passed", "fails", "failed", "ensure", "ensures",
    "correct", "correctly", "exact", "exactly",
}


def _tokens(s: str) -> set[str]:
    return {
        t.lower()
        for t in _TOKEN_RE.findall(s)
        if len(t) >= 4 and t.lower() not in _STOP_TOKENS
    }


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


_SIG_NONWORD_RE = re.compile(r"[^a-z0-9]+")


def _body_signature(body: str) -> str:
    """Lowercase, alphanumeric-only, first 80 chars. Collapses wording
    drift between tests catching the same mutation while keeping distinct
    mutations apart (different bugs have different risk bodies)."""
    return _SIG_NONWORD_RE.sub(" ", (body or "").lower()).strip()[:80]


def _dedup_key(cand: CatchCandidate) -> Tuple:
    """Collapse only true duplicates of the same regression. Key on
    (file, line, body_signature). File-only keying (previous behavior)
    over-collapsed bundle-workflow runs where N distinct bugs live in the
    same file. Falls back to body signature alone when file is unknown
    (dodgy_diff workflow has no structured risks), and finally to the
    test name when there's nothing to key on."""
    file, line, _cls, body = _risk_meta_for(cand)
    sig = _body_signature(body)
    if file or line is not None or sig:
        return ("risk", file or "", line or 0, sig)
    return ("name", cand.test.name.strip().lower())


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


def _finding_to_dict(f: ReviewFinding) -> dict:
    d = asdict(f)
    return d


def _caught_locations(weak: List[CatchCandidate]) -> set[Tuple[str, Optional[int]]]:
    locs: set[Tuple[str, Optional[int]]] = set()
    for c in weak:
        rf, rl, _, _ = _risk_meta_for(c)
        if rf:
            locs.add((rf, rl))
    return locs


def _annotate_findings(
    findings: List[ReviewFinding], weak: List[CatchCandidate]
) -> None:
    """Tag findings already caught by a failing test so the reader can
    skip past them — the test is stronger evidence than a reasoning
    flag. We don't drop them; cross-source agreement is a useful
    signal."""
    caught = _caught_locations(weak)
    for f in findings:
        if not f.file:
            continue
        if (f.file, f.line) in caught or (f.file, None) in caught:
            if not f.validator_note:
                f.validator_note = "(already caught by a failing test)"
            elif "already caught" not in f.validator_note:
                f.validator_note = f.validator_note + " — also caught by test"


_SEV_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Trivial": 4, "Info": 5}


def _parsed_risks(c: CatchCandidate) -> List[Tuple[str, str]]:
    """Only structured `[file:line] (cls) body` risks — no judge_rationale
    fallback. Fallback pollutes matching with generic prose tokens."""
    out: List[Tuple[str, str]] = []
    for r in c.risks or []:
        m = _RISK_PREFIX_RE.match((r or "").strip())
        if not m:
            continue
        rf = (m.group("file") or "").strip()
        rb = (m.group("body") or "").strip()
        out.append((rf, rb))
    return out


def _group_evidence(
    weak: List[CatchCandidate], findings: List[ReviewFinding]
) -> List[Dict]:
    """Union tests + findings into per-bug groups. Matches require
    (test name + test rationale) shares >=2 identifier tokens with the
    finding TITLE. For tests with parsed risks (intent_aware workflow),
    additionally require the finding's file to appear in the test's
    parsed risk files. The finding's rationale is intentionally excluded
    — it tends to carry generic prose ("config", "credentials", "when")
    that over-matches across distinct bugs. Orphan tests form their own
    groups. Cross-source matches keep both evidence lanes for the same
    bug in one entry."""
    used: set[int] = set()
    groups: List[Dict] = []
    pre = [(c, _parsed_risks(c)) for c in weak]
    for f in findings:
        f_title = _tokens(f.title)
        matches: List[int] = []
        for i, (c, parsed) in enumerate(pre):
            if i in used:
                continue
            name_tokens = _tokens(c.test.name) | _tokens(c.test.rationale or "")
            matched = False
            if parsed:
                risk_files = {rf for rf, _ in parsed if rf}
                file_ok = (not f.file) or (f.file in risk_files)
                if file_ok and len(f_title & name_tokens) >= 2:
                    matched = True
            else:
                if len(f_title & name_tokens) >= 2:
                    matched = True
            if matched:
                matches.append(i)
        for i in matches:
            used.add(i)
        groups.append({"finding": f, "tests": [weak[i] for i in matches]})
    for i, c in enumerate(weak):
        if i not in used:
            groups.append({"finding": None, "tests": [c]})
    return groups


def _group_sort_key(g: Dict) -> Tuple[int, float]:
    f = g["finding"]
    tests = g["tests"]
    if f:
        sev = _SEV_ORDER.get(f.severity, 9)
        score = tests[0].final_score if tests else f.confidence
    else:
        c = tests[0]
        sev = _SEV_ORDER.get(_severity_from_score(c.final_score), 9)
        score = c.final_score
    return (sev, -score)


def write_json(
    candidates: List[CatchCandidate],
    out_path: Path,
    findings: Optional[List[ReviewFinding]] = None,
) -> None:
    findings = findings or []
    payload = {
        "summary": {
            "total": len(candidates),
            "weak_catches": sum(1 for c in candidates if c.is_weak_catch),
            "review_findings": len(findings),
        },
        "candidates": [to_dict(c) for c in candidates],
        "review_findings": [_finding_to_dict(f) for f in findings],
    }
    out_path.write_text(json.dumps(payload, indent=2))


def render_text(
    candidates: List[CatchCandidate],
    findings: Optional[List[ReviewFinding]] = None,
) -> str:
    findings = findings or []
    weak_all = [c for c in candidates if c.is_weak_catch]
    weak, sizes = _dedup_weak(weak_all)
    lines: list[str] = []
    lines.append(f"Total generated: {len(candidates)}")
    lines.append(f"Weak catches:    {len(weak)}"
                 + (f" (deduped from {len(weak_all)})" if len(weak) != len(weak_all) else ""))
    lines.append(f"Review findings: {len(findings)}")
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
    if findings:
        lines.append("")
        lines.append("=" * 70)
        lines.append("REVIEW FINDINGS (LLM reasoning — no failing test)")
        lines.append("=" * 70)
        for i, f in enumerate(findings, 1):
            loc = f.file + (f":{f.line}" if f.line else "")
            lines.append(f"\n#{i}  {f.severity}  {loc}  ({f.category})")
            lines.append(f"    title:     {f.title}")
            if f.rationale:
                lines.append(f"    rationale: {f.rationale[:260]}")
            if f.validator_note:
                lines.append(f"    validator: {f.validator_note}")
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
    findings: Optional[List[ReviewFinding]] = None,
) -> None:
    """Human-readable report. `meta` carries run context (command, revs,
    repo). `file_diffs` maps repo-relative path → unified diff text so
    the report can show hunk headers (line numbers) and +/- lines.
    `findings` are agentic-reviewer outputs — a separate evidence channel
    from failing-test weak catches."""
    meta = meta or {}
    file_diffs = file_diffs or {}
    findings = list(findings or [])

    weak_all = [c for c in candidates if c.is_weak_catch]
    weak, _ = _dedup_weak(weak_all)
    _annotate_findings(findings, weak)

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

    groups = _group_evidence(weak, findings)
    groups.sort(key=_group_sort_key)

    if not groups:
        md.append("_No bugs surfaced — no failing tests and no review findings._")
        md.append("")
        out_path.write_text("\n".join(md))
        return

    n_test_only = sum(1 for g in groups if not g["finding"])
    n_review_only = sum(1 for g in groups if g["finding"] and not g["tests"])
    n_both = sum(1 for g in groups if g["finding"] and g["tests"])

    md.append("## Findings")
    md.append("")
    md.append(
        f"_{len(groups)} bug{'s' if len(groups) != 1 else ''} "
        f"— {n_both} with test+review, {n_test_only} test-only, "
        f"{n_review_only} review-only._"
    )
    md.append("")

    for i, g in enumerate(groups, 1):
        f = g["finding"]
        tests = g["tests"]
        title = f.title if f else tests[0].test.name
        md.append(f"### {i}. {title}")
        md.append("")

        if f:
            loc_path = f.file or (
                tests[0].target_files[0] if tests and tests[0].target_files else ""
            )
            line = f.line
            sev = f.severity or "Medium"
            cat = f.category or "-"
            conf = f.confidence
        else:
            c = tests[0]
            rf, rl, cls, _body = _risk_meta_for(c)
            loc_path = rf or (c.target_files[0] if c.target_files else "")
            line = rl
            sev = _severity_from_score(c.final_score)
            cat = cls or "-"
            conf = c.judge_tp_prob

        evidence: List[str] = []
        if tests:
            evidence.append(
                f"{len(tests)} failing test" + ("s" if len(tests) > 1 else "")
            )
        if f:
            evidence.append("LLM review")
        evidence_md = " + ".join(evidence) if evidence else "-"

        loc_md = _file_link(loc_path, line, meta) if loc_path else "`(no file)`"
        line_str = f":{line}" if line else ""
        md.append(
            f"**Location:** {loc_md}{line_str} &nbsp;•&nbsp; "
            f"**Severity:** {_severity_md(sev)} &nbsp;•&nbsp; "
            f"**Category:** `{cat}` &nbsp;•&nbsp; "
            f"**Evidence:** {evidence_md} &nbsp;•&nbsp; "
            f"**Confidence:** `{conf:.2f}`"
        )
        md.append("")

        diff_file: Optional[str] = None
        if loc_path and file_diffs.get(loc_path):
            diff_file = loc_path
        elif tests:
            diff_file = _best_diff_file(tests[0], file_diffs)
        if diff_file and file_diffs.get(diff_file):
            if line is not None:
                body = _hunk_around(file_diffs[diff_file], line)
            else:
                tok_source = f.title + " " + (f.rationale or "") if f else (
                    tests[0].test.name + " " + (tests[0].test.rationale or "")
                )
                body = _best_hunk_by_tokens(file_diffs[diff_file], _tokens(tok_source))
            if body.strip():
                md.append("```diff")
                md.append(_strip_hunk_header(body))
                md.append("```")
                md.append("")

        rationale = ""
        if f:
            rationale = f.rationale or ""
        elif tests:
            rationale = tests[0].judge_rationale or tests[0].test.rationale or ""
        if rationale.strip():
            md.append("**Why this is a bug**")
            md.append("")
            md.append(f"> {rationale.strip()}")
            md.append("")

        if tests:
            md.append("<details>")
            label = f"Unit test{'s' if len(tests) > 1 else ''} ({len(tests)})"
            md.append(f"<summary>{label}</summary>")
            md.append("")
            for t in tests:
                lang = _lang_hint(t.target_files[0]) if t.target_files else ""
                md.append(f"**`{t.test.name}`**")
                md.append("")
                md.append(f"```{lang}")
                md.append(t.test.code.rstrip())
                md.append("```")
                md.append("")
            md.append("</details>")
            md.append("")

        if f and f.validator_note and "already caught" not in f.validator_note:
            md.append(f"_Validator: {f.validator_note}_")
            md.append("")

    out_path.write_text("\n".join(md))
