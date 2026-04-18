"""Bundle context + caller discovery for multi-file jitcatch runs.

The bundle builder assembles a single LLM prompt containing:
- Per-file parent sources (truncated to hunk windows when large).
- Per-file diffs.
- A list of USAGE-CONTEXT files (callers, not test targets).

Callers are discovered by a best-effort text grep over adapter-registered
file extensions — no import-graph resolution, no TS paths, no webpack
aliases. That's explicitly out of scope.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Dict, List, Sequence, Tuple


MAX_FILES_DEFAULT = 20
MAX_BYTES_DEFAULT = 200_000
LARGE_FILE_BYTES = 40_000
HUNK_CONTEXT_LINES = 50


def find_callers(
    repo: Path,
    target_rel: str,
    lang: str,
    max_results: int = 5,
) -> List[str]:
    """Return repo-relative paths of files that import/require the target.

    Best-effort text grep. JS: matches require/import of relative paths
    resolving to target_rel. Python: matches import of the dotted module.
    """
    repo = repo.resolve()
    target_rel = target_rel.lstrip("./")
    if lang == "javascript":
        return _find_js_callers(repo, target_rel, max_results)
    if lang == "python":
        return _find_py_callers(repo, target_rel, max_results)
    return []


def _find_js_callers(repo: Path, target_rel: str, limit: int) -> List[str]:
    target_abs = (repo / target_rel).resolve()
    target_posix = PurePosixPath(target_rel)
    target_noext = target_posix.with_suffix("")

    # Candidate spec strings a caller's import/require could plausibly use.
    target_keys = {str(target_posix), str(target_noext)}
    target_keys |= {t.lstrip("./") for t in list(target_keys)}

    import_pat = re.compile(
        r"""(?:require|from)\s*\(?\s*['"]([^'"]+)['"]""",
    )
    hits: List[str] = []
    for path in _iter_js_files(repo):
        if path == target_abs:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for match in import_pat.findall(text):
            spec = match.strip()
            if not spec.startswith((".", "/")):
                continue
            resolved = _resolve_js_spec(path.parent, spec)
            if resolved is None:
                continue
            if resolved == target_abs:
                rel = str(PurePosixPath(path.relative_to(repo)))
                if rel not in hits:
                    hits.append(rel)
                break
            # Also match by stripped-extension comparison (common for
            # `require('./foo')` -> foo.js).
            if spec.lstrip("./") in target_keys:
                rel = str(PurePosixPath(path.relative_to(repo)))
                if rel not in hits:
                    hits.append(rel)
                break
        if len(hits) >= limit:
            break
    return hits[:limit]


def _resolve_js_spec(base: Path, spec: str) -> Path | None:
    candidate = (base / spec).resolve()
    if candidate.is_file():
        return candidate
    for ext in (".js", ".mjs", ".cjs", ".json"):
        p = Path(str(candidate) + ext)
        if p.is_file():
            return p
    if candidate.is_dir():
        for name in ("index.js", "index.mjs", "index.cjs"):
            p = candidate / name
            if p.is_file():
                return p
    return None


def _iter_js_files(repo: Path):
    skip = {"node_modules", ".git", "dist", "build", ".next"}
    for p in repo.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix not in (".js", ".mjs", ".cjs"):
            continue
        parts = set(p.relative_to(repo).parts)
        if parts & skip:
            continue
        yield p


def _find_py_callers(repo: Path, target_rel: str, limit: int) -> List[str]:
    module_posix = PurePosixPath(target_rel).with_suffix("")
    dotted = str(module_posix).replace("/", ".")
    if not dotted:
        return []
    # Accept `from <dotted> ...` or `import <dotted>`; also accept
    # trailing-segment relative usage (`from <leaf> import ...` only
    # in the same directory).
    pat = re.compile(
        r"""^(?:from\s+(?P<from>[\w\.]+)\s+import|import\s+(?P<imp>[\w\.,\s]+))""",
        re.MULTILINE,
    )
    hits: List[str] = []
    target_abs = (repo / target_rel).resolve()
    for path in _iter_py_files(repo):
        if path.resolve() == target_abs:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        matched = False
        for m in pat.finditer(text):
            spec = (m.group("from") or m.group("imp") or "").strip()
            for part in re.split(r"[,\s]+", spec):
                part = part.strip()
                if not part:
                    continue
                if part == dotted or part.startswith(dotted + "."):
                    matched = True
                    break
                if dotted.endswith("." + part) or dotted == part:
                    matched = True
                    break
            if matched:
                break
        if matched:
            rel = str(PurePosixPath(path.relative_to(repo)))
            if rel not in hits:
                hits.append(rel)
            if len(hits) >= limit:
                break
    return hits[:limit]


def _iter_py_files(repo: Path):
    skip = {".venv", "venv", "__pycache__", ".git", "build", "dist"}
    for p in repo.rglob("*.py"):
        if not p.is_file():
            continue
        parts = set(p.relative_to(repo).parts)
        if parts & skip:
            continue
        yield p


# ---------------------------------------------------------------------------
# Bundle construction


def normalize_relative_import(rel: str) -> str:
    """Turn 'app/foo.js' or './app/foo.js' into a canonical './app/foo.js'.

    Guards against the double-'./' bug seen in the real run.
    """
    posix = PurePosixPath(rel.lstrip("/"))
    # strip any leading './' segments
    while posix.parts and posix.parts[0] == ".":
        posix = PurePosixPath(*posix.parts[1:]) if len(posix.parts) > 1 else PurePosixPath("")
    if not str(posix):
        return "./"
    return "./" + str(posix)


def extract_hunk_windows(parent_source: str, diff_text: str, context_lines: int = HUNK_CONTEXT_LINES) -> str:
    """Return only parent-source regions touched by the diff, widened by N lines.

    Uses the unified-diff hunk headers to locate the parent lines of
    interest. If parsing fails, returns the full source.
    """
    if not parent_source:
        return ""
    lines = parent_source.splitlines()
    hunk_re = re.compile(r"^@@ -(?P<start>\d+)(?:,(?P<count>\d+))? \+\d+(?:,\d+)? @@", re.MULTILINE)
    ranges: List[Tuple[int, int]] = []
    for m in hunk_re.finditer(diff_text):
        start = int(m.group("start"))
        count = int(m.group("count") or "1")
        lo = max(1, start - context_lines)
        hi = min(len(lines), start + count + context_lines - 1)
        ranges.append((lo, hi))
    if not ranges:
        return parent_source
    ranges.sort()
    merged: List[Tuple[int, int]] = []
    for lo, hi in ranges:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    parts: List[str] = []
    for lo, hi in merged:
        parts.append(f"# ... lines {lo}-{hi} ...")
        parts.extend(lines[lo - 1 : hi])
    return "\n".join(parts)


def select_files(
    changed: Sequence[str],
    churn_by_file: Dict[str, int] | None = None,
    max_files: int = MAX_FILES_DEFAULT,
) -> List[str]:
    """Cap at max_files, keeping the top-N by churn if provided."""
    files = list(changed)
    if churn_by_file:
        files.sort(key=lambda f: churn_by_file.get(f, 0), reverse=True)
    return files[:max_files]


def build_bundle(
    files: Sequence[Tuple[str, str, str]],
    callers: Sequence[Tuple[str, str]] = (),
    max_bytes: int = MAX_BYTES_DEFAULT,
) -> str:
    """Format a single prompt string for a bundle LLM call.

    `files` = [(rel_path, parent_source, diff), ...] — the changed files
    under test. If a parent source exceeds LARGE_FILE_BYTES we only keep
    hunk windows.

    `callers` = [(rel_path, parent_source), ...] — usage context only,
    not to be tested directly. Always hunk-less full source (they're
    bounded by --max-callers + per-file cap upstream).

    The total string is capped at `max_bytes` by truncating the tail of
    caller sources first, then trimming parent sources.
    """
    out: List[str] = []
    out.append("=== CHANGED FILES (generate tests that assert the parent behavior) ===\n")
    for rel, parent_source, diff in files:
        shown = parent_source
        if len(parent_source) > LARGE_FILE_BYTES:
            shown = extract_hunk_windows(parent_source, diff)
        out.append(f"\n--- FILE: {rel} ---\n")
        out.append("PARENT SOURCE:\n")
        out.append(shown)
        out.append("\nDIFF:\n")
        out.append(diff)
    if callers:
        out.append("\n\n=== USAGE CONTEXT (do not test these directly; they illustrate how the changed code is called) ===\n")
        for rel, source in callers:
            shown = source if len(source) <= LARGE_FILE_BYTES else source[:LARGE_FILE_BYTES] + "\n# ... (truncated)\n"
            out.append(f"\n--- CALLER: {rel} ---\n")
            out.append(shown)
    text = "".join(out)
    if len(text) > max_bytes:
        text = text[:max_bytes] + "\n# ... (bundle truncated to max_bytes)\n"
    return text


def churn_by_file(numstat_output: str) -> Dict[str, int]:
    """Parse `git diff --numstat` output into {path: added+deleted}."""
    out: Dict[str, int] = {}
    for line in numstat_output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            added = int(parts[0]) if parts[0] != "-" else 0
            deleted = int(parts[1]) if parts[1] != "-" else 0
        except ValueError:
            continue
        out[parts[2].strip()] = added + deleted
    return out


__all__ = [
    "MAX_BYTES_DEFAULT",
    "MAX_FILES_DEFAULT",
    "build_bundle",
    "churn_by_file",
    "extract_hunk_windows",
    "find_callers",
    "normalize_relative_import",
    "select_files",
]
