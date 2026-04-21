from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import CatchCandidate, ReviewFinding


def stable_id(cand: CatchCandidate) -> str:
    """Stable short hash identifying a candidate across runs. Keys on
    workflow + test name + target files so the same catch produced by
    two runs resolves to the same id (enabling `jitcatch explain`).
    Uses sha256 truncated to 12 hex chars — 48 bits — effectively
    collision-free at the handful-per-run scale this operates at."""
    h = hashlib.sha256()
    h.update((cand.workflow or "").encode())
    h.update(b"\0")
    h.update((cand.test.name or "").encode())
    h.update(b"\0")
    for f in sorted(cand.target_files or []):
        h.update(f.encode())
        h.update(b"\0")
    return h.hexdigest()[:12]


_RISK_PREFIX_RE = re.compile(
    r"^\[(?P<file>[^\]:]+)(?::(?P<line>\d+))?\]\s*"
    r"(?:\((?P<cls>[^)]+)\)\s*)?(?P<body>.*)$"
)


def _file_link(path: str, line: Optional[int], meta: Dict[str, str]) -> str:
    """Build a clickable link to the source file. Prefers a relative
    path from the report's own directory — VS Code's built-in markdown
    preview blocks `file://` links for security, but renders relative
    paths. Falls back to `file://` when we don't know the output dir,
    and to a bare code span when we don't even know the repo."""
    label = f"{path}:{line}" if line else path
    repo = meta.get("repo")
    if not repo:
        return f"`{label}`"
    abs_src = os.path.join(repo.rstrip("/"), path)
    out_dir = meta.get("out_dir")
    if out_dir:
        href = os.path.relpath(abs_src, out_dir)
    else:
        href = f"file://{abs_src}"
    if line:
        href += f"#L{line}"
    return f"[{label}]({href})"


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
    d["id"] = stable_id(cand)
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


def _group_sort_key(g: Dict) -> Tuple[int, int, float]:
    """Primary key `has_test` (0 when a failing test backs the group, 1
    when the group is review-only) guarantees executable evidence ranks
    above LLM opinion regardless of severity. A `Critical` review-only
    finding without a failing test should never outrank a `High`
    test-backed catch — the whole product is built on that inversion
    being wrong."""
    f = g["finding"]
    tests = g["tests"]
    has_test = 0 if tests else 1
    if f:
        sev = _SEV_ORDER.get(f.severity, 9)
        score = tests[0].final_score if tests else f.confidence
    else:
        c = tests[0]
        sev = _SEV_ORDER.get(_severity_from_score(c.final_score), 9)
        score = c.final_score
    return (has_test, sev, -score)


def write_json(
    candidates: List[CatchCandidate],
    out_path: Path,
    findings: Optional[List[ReviewFinding]] = None,
    usage=None,
) -> None:
    """Order candidates so JSON readers see the same ranking as the md
    report: weak catches first by `final_score` desc (likely regressions
    at top, fp-flagged entries at the bottom), then non-weak candidates
    (tests that passed or failed on both revs) appended in input order."""
    findings = findings or []
    ordered = sorted(
        candidates,
        key=lambda c: (not c.is_weak_catch, -c.final_score),
    )
    summary: Dict = {
        "total": len(candidates),
        "weak_catches": sum(1 for c in candidates if c.is_weak_catch),
        "review_findings": len(findings),
    }
    if usage is not None and hasattr(usage, "to_dict"):
        summary["usage"] = usage.to_dict()
    payload = {
        "summary": summary,
        "candidates": [to_dict(c) for c in ordered],
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


_OLD_NEW_RE = re.compile(r"(?<=[\.\?!])\s+(New\b|After\b|Before\b)")


def _format_rationale(text: str) -> str:
    """Put Old/New (and Before/After) comparison sentences on separate
    paragraphs so the reader can compare them side-by-side. Splits on
    a sentence boundary (`.`, `?`, `!`) followed by New/After/Before —
    handles bare `New:`, qualified forms like `New code:` / `New
    guard:`, and unpunctuated intros like `New code returns count:` /
    `After fix the caller…`. Single newline inside a blockquote
    renders as a soft wrap in most previews (including Cursor) — need
    a blank blockquote line (rendered as `>\n`) to force a real
    paragraph break."""
    return _OLD_NEW_RE.sub(r"\n\n\1", text)


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


_FP_SEVERITIES = {"Trivial", "Info"}


def _group_severity(g: Dict) -> str:
    f = g["finding"]
    tests = g["tests"]
    if f:
        return f.severity or "Medium"
    return _severity_from_score(tests[0].final_score)


def _is_likely_fp(g: Dict) -> bool:
    """Bucket a group into the collapsed FP section when the severity
    resolves to Trivial/Info. Test-only groups drop here when their
    final_score < 0.2 (severity mapping in `_severity_from_score`).
    Review-only findings never land here — the LLM only assigns
    Critical/High/Medium/Low — so opinion flags always stay visible in
    the main list."""
    return _group_severity(g) in _FP_SEVERITIES


def _group_metadata(
    g: Dict,
) -> Tuple[str, str, Optional[int], str, str, float, str]:
    """Resolve per-group display fields once so overview table and
    per-finding render agree: (title, loc_path, line, sev, cat, conf, sid).
    sid is empty when no failing test backs the group."""
    f = g["finding"]
    tests = g["tests"]
    title = f.title if f else tests[0].test.name
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
    sid = stable_id(tests[0]) if tests else ""
    return title, loc_path, line, sev, cat, conf, sid


def _render_group(
    g: Dict,
    i: int,
    md: List[str],
    meta: Dict[str, str],
    file_diffs: Dict[str, str],
) -> None:
    f = g["finding"]
    tests = g["tests"]
    title, loc_path, line, sev, cat, conf, sid = _group_metadata(g)
    # Explicit HTML anchor — most MD renderers honor it; heading-slug
    # anchors break on punctuation ("1." becomes unreliable).
    md.append(f'<a id="finding-{i}"></a>')
    md.append(f"### {i}. {title}")
    md.append("")

    loc_md = _file_link(loc_path, line, meta) if loc_path else "`(no file)`"
    loc_line = (
        f"**Location:** {loc_md} &nbsp;•&nbsp; "
        f"**Severity:** {_severity_md(sev)} &nbsp;•&nbsp; "
        f"**Category:** `{cat}` &nbsp;•&nbsp; "
        f"**Confidence:** `{conf:.2f}`"
    )
    if sid:
        loc_line += f" &nbsp;•&nbsp; **ID:** `{sid}`"
    md.append(loc_line)
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
        formatted = _format_rationale(rationale.strip())
        for ln in formatted.split("\n"):
            md.append(f"> {ln}" if ln else ">")
        md.append("")

    if f and f.validator_note and "already caught" not in f.validator_note:
        md.append(f"_Expert says: {f.validator_note}_")
        md.append("")

    if tests:
        md.append("<details>")
        label = f"Unit test{'s' if len(tests) > 1 else ''} ({len(tests)})"
        md.append(f"<summary>{label}</summary>")
        md.append("")
        multi = len(tests) > 1
        for idx, t in enumerate(tests, 1):
            lang = _lang_hint(t.target_files[0]) if t.target_files else ""
            prefix = f"{idx}. " if multi else ""
            md.append(f"**{prefix}`{t.test.name}`**")
            md.append("")
            md.append(f"```{lang}")
            md.append(t.test.code.rstrip())
            md.append("```")
            md.append("")
        md.append("</details>")
        md.append("")


def _overview_md(
    entries: List[Tuple[int, Dict]], meta: Dict[str, str]
) -> List[str]:
    """High-level table over every finding. Each row links to the
    per-finding anchor rendered further down — useful for long reports."""
    if not entries:
        return []
    out = [
        "## Overview",
        "",
        "| # | Title | Location | Severity | Category | Conf. | ID |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, g in entries:
        title, loc_path, line, sev, cat, conf, sid = _group_metadata(g)
        loc = _file_link(loc_path, line, meta) if loc_path else "`(no file)`"
        # Pipe breaks the table — escape any pipes inside the title.
        safe_title = (title or "").replace("|", "\\|")
        sid_cell = f"`{sid}`" if sid else "-"
        out.append(
            f"| [{i}](#finding-{i}) | [{safe_title}](#finding-{i}) | {loc} | "
            f"{_severity_md(sev)} | `{cat}` | `{conf:.2f}` | {sid_cell} |"
        )
    out.append("")
    return out


def _overview_html(
    entries: List[Tuple[int, Dict]], meta: Dict[str, str]
) -> List[str]:
    if not entries:
        return []
    out = [
        "<h2>Overview</h2>",
        '<table class="overview">',
        "<thead><tr><th>#</th><th>Title</th><th>Location</th>"
        "<th>Severity</th><th>Category</th><th>Conf.</th><th>ID</th></tr></thead>",
        "<tbody>",
    ]
    for i, g in entries:
        title, loc_path, line, sev, cat, conf, sid = _group_metadata(g)
        loc_html = (
            _html_file_link(loc_path, line, meta)
            if loc_path
            else "<code>(no file)</code>"
        )
        sid_cell = f"<code>{_html_escape(sid)}</code>" if sid else "-"
        out.append(
            f'<tr><td><a href="#finding-{i}">{i}</a></td>'
            f'<td><a href="#finding-{i}">{_html_escape(title)}</a></td>'
            f"<td>{loc_html}</td>"
            f'<td><span class="badge {_html_escape(sev)}">{_html_escape(sev)}</span></td>'
            f"<td><code>{_html_escape(cat)}</code></td>"
            f"<td><code>{conf:.2f}</code></td>"
            f"<td>{sid_cell}</td></tr>"
        )
    out.append("</tbody></table>")
    return out


def _usage_stage_parts(usage) -> List[str]:
    parts = []
    for stage, s in sorted(usage.by_stage.items()):
        p = f"{stage}={s['input_tokens']:,}/{s['output_tokens']:,}"
        if s.get("cost_usd", 0) > 0:
            p += f" (${s['cost_usd']:.4f})"
        parts.append(p)
    return parts


def _usage_md_lines(usage) -> List[str]:
    if usage is None or getattr(usage, "calls", 0) == 0:
        return []
    lines = [
        "| Metric | Value |",
        "| --- | --- |",
        f"| **LLM calls** | {usage.calls} |",
        f"| **Tokens** | in={usage.input_tokens:,} out={usage.output_tokens:,} |",
        f"| **Cost (USD)** | ${usage.cost_usd:.4f} |",
    ]
    if usage.by_stage:
        parts = _usage_stage_parts(usage)
        lines.append(f"| **Stages (in/out)** | {' • '.join(parts)} |")
    lines.append("")
    return lines


def write_markdown(
    candidates: List[CatchCandidate],
    out_path: Path,
    meta: Optional[Dict[str, str]] = None,
    file_diffs: Optional[Dict[str, str]] = None,
    findings: Optional[List[ReviewFinding]] = None,
    usage=None,
) -> None:
    """Human-readable report. `meta` carries run context (command, revs,
    repo). `file_diffs` maps repo-relative path → unified diff text so
    the report can show hunk headers (line numbers) and +/- lines.
    `findings` are agentic-reviewer outputs — a separate evidence channel
    from failing-test weak catches."""
    meta = dict(meta or {})
    meta.setdefault("out_dir", str(out_path.parent))
    file_diffs = file_diffs or {}
    findings = list(findings or [])

    weak_all = [c for c in candidates if c.is_weak_catch]
    weak, _ = _dedup_weak(weak_all)
    _annotate_findings(findings, weak)

    md: List[str] = []
    md.append("# JitCatch Report")
    md.append("")

    # Header metadata.
    if meta or file_diffs:
        md.append("| Field | Value |")
        md.append("| --- | --- |")
        for k in ("command", "repo", "parent", "child", "base"):
            v = meta.get(k)
            if v:
                md.append(f"| **{k}** | `{v}` |")
        if file_diffs:
            files_cell = "<br>".join(
                _file_link(p, None, meta) for p in sorted(file_diffs)
            )
            md.append(f"| **changed files** ({len(file_diffs)}) | {files_cell} |")
        md.append("")

    groups = _group_evidence(weak, findings)
    groups.sort(key=_group_sort_key)

    if not groups:
        md.append("_No bugs surfaced — no failing tests and no review findings._")
        md.append("")
        usage_md = _usage_md_lines(usage)
        if usage_md:
            md.append("## LLM usage")
            md.append("")
            md.extend(usage_md)
        out_path.write_text("\n".join(md))
        return

    main_groups = [g for g in groups if not _is_likely_fp(g)]
    fp_groups = [g for g in groups if _is_likely_fp(g)]

    n_test_only = sum(1 for g in groups if not g["finding"])
    n_review_only = sum(1 for g in groups if g["finding"] and not g["tests"])
    n_both = sum(1 for g in groups if g["finding"] and g["tests"])

    entries: List[Tuple[int, Dict]] = []
    for i, g in enumerate(main_groups, 1):
        entries.append((i, g))
    start = len(main_groups) + 1
    for offset, g in enumerate(fp_groups):
        entries.append((start + offset, g))
    md.extend(_overview_md(entries, meta))

    md.append("## Findings")
    md.append("")
    summary = (
        f"_{len(groups)} bug{'s' if len(groups) != 1 else ''} "
        f"— {n_both} with test+review, {n_test_only} test-only, "
        f"{n_review_only} review-only"
    )
    if fp_groups:
        summary += (
            f". {len(fp_groups)} likely false positive"
            f"{'s' if len(fp_groups) != 1 else ''} collapsed below._"
        )
    else:
        summary += "._"
    md.append(summary)
    md.append("")

    if not main_groups:
        md.append("_No high-signal bugs — everything scored into the false-positive bucket. Expand the section below to review._")
        md.append("")

    for i, g in enumerate(main_groups, 1):
        _render_group(g, i, md, meta, file_diffs)

    if fp_groups:
        md.append("<details>")
        md.append(
            f"<summary><strong>Likely false positives ({len(fp_groups)})</strong>"
            " — low score, skim only</summary>"
        )
        md.append("")
        for i, g in entries[len(main_groups):]:
            _render_group(g, i, md, meta, file_diffs)
        md.append("</details>")
        md.append("")

    usage_md = _usage_md_lines(usage)
    if usage_md:
        md.append("## LLM usage")
        md.append("")
        md.extend(usage_md)

    out_path.write_text("\n".join(md))


_HTML_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;
     margin:0;padding:24px;max-width:1400px;margin-left:auto;margin-right:auto;
     color:#1f2328;background:#fff;line-height:1.5}
h1{margin:0 0 16px;font-size:28px}
h2{margin:32px 0 12px;font-size:20px;border-bottom:1px solid #d0d7de;padding-bottom:6px}
h3{margin:24px 0 8px;font-size:16px}
a{color:#0969da;text-decoration:none}
a:hover{text-decoration:underline}
.meta{border:1px solid #d0d7de;border-radius:6px;padding:12px 16px;background:#f6f8fa;
      margin-bottom:16px;font-size:14px}
.meta .row{display:flex;gap:8px;margin:2px 0}
.meta .k{font-weight:600;min-width:110px;color:#57606a}
.group{border:1px solid #d0d7de;border-radius:6px;padding:16px 20px;margin:12px 0;
       background:#fff}
.loc{font-size:13px;color:#57606a;margin:4px 0 12px}
.loc .sep{margin:0 8px;color:#8b949e}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600;
       margin-right:4px}
.badge.Critical{background:#cf222e;color:#fff}
.badge.High{background:#fb8500;color:#fff}
.badge.Medium{background:#d4a72c;color:#1f2328}
.badge.Low{background:#1a7f37;color:#fff}
.badge.Trivial{background:#8b949e;color:#fff}
.badge.Info{background:#0969da;color:#fff}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
     background:#f6f8fa;border:1px solid #d0d7de;border-radius:4px;padding:1px 5px;font-size:13px}
pre{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
    background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:12px;
    overflow-x:auto;font-size:13px;margin:8px 0}
pre code{background:none;border:none;padding:0}
.diff{white-space:pre;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
      background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:12px;
      overflow-x:auto;font-size:13px;margin:8px 0}
.diff-add{background:#dafbe1;color:#1a7f37;display:block}
.diff-del{background:#ffebe9;color:#cf222e;display:block}
blockquote{border-left:4px solid #d0d7de;margin:8px 0;padding:4px 12px;color:#57606a;
           background:transparent}
blockquote p{margin:6px 0}
details{margin:12px 0}
summary{cursor:pointer;font-weight:600;padding:6px 0}
.fp{border:1px dashed #8b949e;border-radius:6px;padding:12px 16px;margin-top:24px}
.fp summary{color:#8b949e}
.count{color:#8b949e;font-weight:400;font-size:13px}
.empty{font-style:italic;color:#8b949e}
.overview{border-collapse:collapse;width:100%;margin:12px 0 24px;font-size:13px}
.overview th,.overview td{border:1px solid #d0d7de;padding:6px 10px;text-align:left;vertical-align:top}
.overview th{background:#f6f8fa;font-weight:600}
.overview tbody tr:hover td{background:#f6f8fa}
.overview td a{font-weight:600}
.group{scroll-margin-top:12px}
"""


def _html_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _html_file_link(path: str, line: Optional[int], meta: Dict[str, str]) -> str:
    if not path:
        return '<code>(no file)</code>'
    label = f"{path}:{line}" if line else path
    repo = meta.get("repo")
    if not repo:
        return f"<code>{_html_escape(label)}</code>"
    abs_src = os.path.join(repo.rstrip("/"), path)
    out_dir = meta.get("out_dir")
    href = os.path.relpath(abs_src, out_dir) if out_dir else f"file://{abs_src}"
    if line:
        href += f"#L{line}"
    return f'<a href="{_html_escape(href)}"><code>{_html_escape(label)}</code></a>'


def _html_diff_block(body: str) -> str:
    """Color add/del lines in a diff block. Preserves exact whitespace —
    uses a <div class="diff"> with white-space:pre rather than <pre> so
    per-line background colors span the full content width."""
    out: List[str] = ['<div class="diff">']
    for ln in body.splitlines():
        esc = _html_escape(ln)
        if ln.startswith("+") and not ln.startswith("+++"):
            out.append(f'<span class="diff-add">{esc}</span>')
        elif ln.startswith("-") and not ln.startswith("---"):
            out.append(f'<span class="diff-del">{esc}</span>')
        else:
            out.append(esc)
    out.append("</div>")
    return "\n".join(out)


def _render_group_html(
    g: Dict,
    i: int,
    html: List[str],
    meta: Dict[str, str],
    file_diffs: Dict[str, str],
) -> None:
    f = g["finding"]
    tests = g["tests"]
    title, loc_path, line, sev, cat, conf, sid = _group_metadata(g)

    html.append(f'<div class="group" id="finding-{i}">')
    html.append(f"<h3>{i}. {_html_escape(title)}</h3>")
    html.append('<div class="loc">')
    html.append(f'<strong>Location:</strong> {_html_file_link(loc_path, line, meta)}')
    html.append('<span class="sep">•</span>')
    html.append(f'<strong>Severity:</strong> <span class="badge {_html_escape(sev)}">{_html_escape(sev)}</span>')
    html.append('<span class="sep">•</span>')
    html.append(f'<strong>Category:</strong> <code>{_html_escape(cat)}</code>')
    html.append('<span class="sep">•</span>')
    html.append(f'<strong>Confidence:</strong> <code>{conf:.2f}</code>')
    if sid:
        html.append('<span class="sep">•</span>')
        html.append(f'<strong>ID:</strong> <code>{_html_escape(sid)}</code>')
    html.append('</div>')

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
        body = _strip_hunk_header(body)
        if body.strip():
            html.append(_html_diff_block(body))

    rationale = ""
    if f:
        rationale = f.rationale or ""
    elif tests:
        rationale = tests[0].judge_rationale or tests[0].test.rationale or ""
    if rationale.strip():
        html.append("<p><strong>Why this is a bug</strong></p>")
        html.append("<blockquote>")
        for para in _format_rationale(rationale.strip()).split("\n\n"):
            if para.strip():
                html.append(f"<p>{_html_escape(para)}</p>")
        html.append("</blockquote>")

    if f and f.validator_note and "already caught" not in f.validator_note:
        html.append(f"<p><em>Expert says: {_html_escape(f.validator_note)}</em></p>")

    if tests:
        label = f"Unit test{'s' if len(tests) > 1 else ''} ({len(tests)})"
        html.append("<details>")
        html.append(f"<summary>{label}</summary>")
        multi = len(tests) > 1
        for idx, t in enumerate(tests, 1):
            prefix = f"{idx}. " if multi else ""
            html.append(f"<p>{prefix}<code>{_html_escape(t.test.name)}</code></p>")
            html.append(f"<pre><code>{_html_escape(t.test.code.rstrip())}</code></pre>")
        html.append("</details>")
    html.append("</div>")


def _usage_html_block(usage) -> List[str]:
    if usage is None or getattr(usage, "calls", 0) == 0:
        return []
    rows = [
        ("LLM calls", str(usage.calls)),
        ("Tokens", f"in={usage.input_tokens:,} out={usage.output_tokens:,}"),
        ("Cost (USD)", f"${usage.cost_usd:.4f}"),
    ]
    if usage.by_stage:
        rows.append(("Stages (in/out)", " • ".join(_usage_stage_parts(usage))))
    out = ['<div class="meta">']
    for k, v in rows:
        out.append(
            f'<div class="row"><span class="k">{_html_escape(k)}</span>'
            f"<span>{_html_escape(v)}</span></div>"
        )
    out.append("</div>")
    return out


def write_html(
    candidates: List[CatchCandidate],
    out_path: Path,
    meta: Optional[Dict[str, str]] = None,
    file_diffs: Optional[Dict[str, str]] = None,
    findings: Optional[List[ReviewFinding]] = None,
    usage=None,
) -> None:
    """Self-contained HTML report. Pre-bundled CSS inline — no CDN, no
    external fetches, works offline. Same grouping/ranking as markdown."""
    meta = dict(meta or {})
    meta.setdefault("out_dir", str(out_path.parent))
    file_diffs = file_diffs or {}
    findings = list(findings or [])

    weak_all = [c for c in candidates if c.is_weak_catch]
    weak, _ = _dedup_weak(weak_all)
    _annotate_findings(findings, weak)

    html: List[str] = []
    html.append("<!doctype html>")
    html.append('<html lang="en"><head><meta charset="utf-8">')
    html.append("<title>JitCatch Report</title>")
    html.append(f"<style>{_HTML_CSS}</style>")
    html.append("</head><body>")
    html.append("<h1>JitCatch Report</h1>")

    if meta or file_diffs:
        html.append('<div class="meta">')
        for k in ("command", "repo", "parent", "child", "base"):
            v = meta.get(k)
            if v:
                html.append(
                    f'<div class="row"><span class="k">{_html_escape(k)}</span>'
                    f'<code>{_html_escape(str(v))}</code></div>'
                )
        if file_diffs:
            links = " ".join(
                _html_file_link(p, None, meta) for p in sorted(file_diffs)
            )
            html.append(
                f'<div class="row"><span class="k">changed files ({len(file_diffs)})</span>'
                f"<span>{links}</span></div>"
            )
        html.append("</div>")

    groups = _group_evidence(weak, findings)
    groups.sort(key=_group_sort_key)

    if not groups:
        html.append(
            '<p class="empty">No bugs surfaced — no failing tests and no review findings.</p>'
        )
        usage_html = _usage_html_block(usage)
        if usage_html:
            html.append("<h2>LLM usage</h2>")
            html.extend(usage_html)
        html.append("</body></html>")
        out_path.write_text("\n".join(html))
        return

    main_groups = [g for g in groups if not _is_likely_fp(g)]
    fp_groups = [g for g in groups if _is_likely_fp(g)]

    n_test_only = sum(1 for g in groups if not g["finding"])
    n_review_only = sum(1 for g in groups if g["finding"] and not g["tests"])
    n_both = sum(1 for g in groups if g["finding"] and g["tests"])

    entries: List[Tuple[int, Dict]] = []
    for i, g in enumerate(main_groups, 1):
        entries.append((i, g))
    start = len(main_groups) + 1
    for offset, g in enumerate(fp_groups):
        entries.append((start + offset, g))
    html.extend(_overview_html(entries, meta))

    html.append("<h2>Findings</h2>")
    summary = (
        f'{len(groups)} bug{"s" if len(groups) != 1 else ""} '
        f"— {n_both} with test+review, {n_test_only} test-only, "
        f"{n_review_only} review-only"
    )
    if fp_groups:
        summary += (
            f". {len(fp_groups)} likely false positive"
            f"{'s' if len(fp_groups) != 1 else ''} collapsed below."
        )
    else:
        summary += "."
    html.append(f'<p class="count"><em>{_html_escape(summary)}</em></p>')

    if not main_groups:
        html.append(
            '<p class="empty">No high-signal bugs — everything scored into the '
            "false-positive bucket. Expand the section below to review.</p>"
        )

    for i, g in enumerate(main_groups, 1):
        _render_group_html(g, i, html, meta, file_diffs)

    if fp_groups:
        html.append('<details class="fp">')
        html.append(
            f"<summary><strong>Likely false positives ({len(fp_groups)})</strong>"
            " — low score, skim only</summary>"
        )
        for i, g in entries[len(main_groups):]:
            _render_group_html(g, i, html, meta, file_diffs)
        html.append("</details>")

    usage_html = _usage_html_block(usage)
    if usage_html:
        html.append("<h2>LLM usage</h2>")
        html.extend(usage_html)

    html.append("</body></html>")
    out_path.write_text("\n".join(html))
