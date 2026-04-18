from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple  # noqa: F401

from . import adapters, context, diff as gitdiff, report, revs
from .adapters import Adapter
from .assessor import apply_rules, judge_candidate, score_candidate
from .config import CatchCandidate, GeneratedTest
from .llm import AnthropicClient, LLMClient, StubClient
from .runner import WorktreeSandbox, evaluate_test
from .workflows import (
    run_dodgy_diff,
    run_dodgy_diff_bundle,
    run_intent_aware,
    run_intent_aware_bundle,
)


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.command
    if cmd == "run":
        return cmd_run(args)
    if cmd in ("last", "pr", "staged", "working"):
        return cmd_bundle(args)
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jitcatch", description="Just-in-Time Catching test gen (MVP)")
    sub = p.add_subparsers(dest="command")

    # run (per-file, backward compat)
    r = sub.add_parser("run", help="generate catching tests for a single file + explicit revs")
    r.add_argument("repo", help="path to git repo")
    r.add_argument("--file", required=True, help="source file (repo-relative) under test")
    r.add_argument("--parent", default="HEAD~1", help="parent git rev (default: HEAD~1)")
    r.add_argument("--child", default="HEAD", help="child git rev (default: HEAD)")
    r.add_argument("--with-callers", action="store_true", help="include caller context")
    r.add_argument("--max-callers", type=int, default=5)
    _add_shared_args(r)

    # auto-rev bundle subcommands — same pipeline, different rev selection
    for name, help_text in (
        ("last", "smoke-test HEAD~1..HEAD"),
        ("pr", "PR vs base branch (autodetects origin/HEAD; pass --base to override)"),
        ("staged", "pre-commit check of staged changes"),
        ("working", "check uncommitted working-tree changes"),
    ):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("repo", help="path to git repo")
        if name == "pr":
            s.add_argument("--base", default=None, help="base ref (e.g. origin/main, origin/develop)")
        s.add_argument("--with-callers", action="store_true", help="include caller context")
        s.add_argument("--max-callers", type=int, default=5)
        s.add_argument("--max-files", type=int, default=context.MAX_FILES_DEFAULT)
        s.add_argument("--max-bytes", type=int, default=context.MAX_BYTES_DEFAULT)
        _add_shared_args(s)

    return p


def _add_shared_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--workflow",
        choices=["intent", "dodgy", "both"],
        default="both",
    )
    p.add_argument("--stub", action="store_true", help="use StubClient (no API calls)")
    p.add_argument("--model", default="claude-sonnet-4-6")
    p.add_argument("--no-judge", action="store_true", help="skip LLM-as-judge")
    p.add_argument("--timeout", type=int, default=60, help="per-test timeout (s)")
    p.add_argument("--out", default="jitcatch_report.json", help="JSON report path")
    p.add_argument("--verbose", action="store_true", help="print debug info (empty LLM responses, etc.)")
    p.add_argument(
        "--max-tokens",
        type=int,
        default=8192,
        help="per-call output token cap for the LLM (default 8192). "
             "Bump higher if you see stop_reason=max_tokens in verbose logs.",
    )
    p.add_argument(
        "--log-dir",
        default=None,
        help="directory for per-call LLM transcripts (default: .jitcatch_logs "
             "under the repo when --verbose is on). Logs are untruncated.",
    )


def _make_llm(args: argparse.Namespace, repo: Path) -> LLMClient:
    if args.stub:
        return StubClient(repo)
    log_dir: Optional[Path] = None
    if args.log_dir:
        log_dir = Path(args.log_dir)
    elif args.verbose:
        log_dir = repo / ".jitcatch_logs"
    return AnthropicClient(
        model=args.model,
        max_tokens=args.max_tokens,
        verbose=args.verbose,
        log_dir=log_dir,
    )


def cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"error: {repo} is not a git repo", file=sys.stderr)
        return 2

    try:
        parent_rev = gitdiff.resolve_rev(repo, args.parent)
        child_rev = gitdiff.resolve_rev(repo, args.child)
    except gitdiff.GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        adapter = adapters.for_file(args.file)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    parent_source = gitdiff.read_file_at_rev(repo, parent_rev, args.file)
    if not parent_source:
        print(f"error: {args.file} is empty or missing at parent {parent_rev}", file=sys.stderr)
        return 2

    diff_text = gitdiff.get_diff(repo, parent_rev, child_rev, args.file)
    if not diff_text.strip():
        print(f"warning: no diff for {args.file} between {parent_rev[:8]}..{child_rev[:8]}", file=sys.stderr)

    llm = _make_llm(args, repo)
    hints = adapter.prompt_hints(args.file, repo_root=repo)

    # Optional caller context (single-file mode just prepends caller sources
    # to the parent_source so the existing single-file prompt benefits).
    if getattr(args, "with_callers", False):
        callers = context.find_callers(repo, args.file, adapter.lang, max_results=args.max_callers)
        if callers:
            caller_block = _render_callers(repo, parent_rev, callers)
            parent_source = parent_source + "\n\n" + caller_block

    all_tests: list[tuple[str, list[str], GeneratedTest, list[str]]] = []
    if args.workflow in ("intent", "both"):
        risks, intent_tests = run_intent_aware(llm, parent_source, diff_text, adapter.lang, hints)
        for t in intent_tests:
            all_tests.append(("intent_aware", risks, t, [args.file]))
    if args.workflow in ("dodgy", "both"):
        dodgy_tests = run_dodgy_diff(llm, parent_source, diff_text, adapter.lang, hints)
        for t in dodgy_tests:
            all_tests.append(("dodgy_diff", [], t, [args.file]))

    meta = _build_meta(args, repo, parent_rev, child_rev)
    file_diffs = {args.file: diff_text}
    return _evaluate_and_report(
        args=args,
        repo=repo,
        parent_rev=parent_rev,
        child_rev=child_rev,
        adapter_by_group={0: adapter},
        groups=[(0, all_tests, parent_source, diff_text)],
        llm=llm,
        meta=meta,
        file_diffs=file_diffs,
    )


def _build_meta(args: argparse.Namespace, repo: Path, parent_rev: str, child_rev: str) -> Dict[str, str]:
    meta = {
        "command": args.command,
        "repo": str(repo),
        "parent": parent_rev,
        "child": child_rev,
    }
    base = getattr(args, "base", None)
    if base:
        meta["base"] = base
    return meta


def cmd_bundle(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"error: {repo} is not a git repo", file=sys.stderr)
        return 2

    try:
        pair = revs.resolve(
            repo,
            args.command,
            base=getattr(args, "base", None),
        )
    except revs.RevError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        return _cmd_bundle_inner(args, repo, pair)
    finally:
        pair.close()


def _cmd_bundle_inner(args: argparse.Namespace, repo: Path, pair: revs.RevPair) -> int:
    parent_rev = pair.parent
    child_rev = pair.child
    print(f"[jitcatch] {args.command}: {pair.description}  (parent={parent_rev[:8]} child={child_rev[:8]})", file=sys.stderr)

    try:
        all_changed = gitdiff.changed_files(repo, parent_rev, child_rev)
    except gitdiff.GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    meta = _build_meta(args, repo, parent_rev, child_rev)
    if not all_changed:
        print(f"warning: no changed files between {parent_rev[:8]}..{child_rev[:8]}", file=sys.stderr)
        _emit_empty_report(args, meta)
        return 0

    # Group changed files by adapter (skip files with no adapter).
    adapter_files: Dict[Adapter, List[str]] = {}
    for f in all_changed:
        try:
            a = adapters.for_file(f)
        except ValueError:
            continue
        adapter_files.setdefault(a, []).append(f)

    if not adapter_files:
        print("warning: no changed files match a known adapter", file=sys.stderr)
        _emit_empty_report(args, meta)
        return 0

    # Compute churn to pick top-N per group.
    numstat = _numstat(repo, parent_rev, child_rev)
    churn = context.churn_by_file(numstat)

    llm = _make_llm(args, repo)

    # Generate tests per adapter group from a single bundled prompt.
    # `groups` = list of (group_key, [(workflow, risks, test, target_files), ...], combined_parent_for_judge, combined_diff_for_judge).
    adapter_by_group: Dict[int, Adapter] = {}
    groups: List[Tuple[int, list, str, str]] = []
    file_diffs: Dict[str, str] = {}
    for gkey, (adapter, files) in enumerate(adapter_files.items()):
        selected = context.select_files(files, churn, max_files=args.max_files)
        bundle_files: List[Tuple[str, str, str]] = []
        combined_parent_parts: List[str] = []
        combined_diff_parts: List[str] = []
        for rel in selected:
            parent_source = gitdiff.read_file_at_rev(repo, parent_rev, rel)
            diff_text = gitdiff.get_diff(repo, parent_rev, child_rev, rel)
            bundle_files.append((rel, parent_source, diff_text))
            combined_parent_parts.append(f"--- {rel} ---\n{parent_source}")
            combined_diff_parts.append(f"--- {rel} ---\n{diff_text}")
            file_diffs[rel] = diff_text

        caller_entries: List[Tuple[str, str]] = []
        if args.with_callers:
            seen = set(selected)
            for rel in selected:
                callers = context.find_callers(repo, rel, adapter.lang, max_results=args.max_callers)
                for c in callers:
                    if c in seen:
                        continue
                    seen.add(c)
                    caller_source = gitdiff.read_file_at_rev(repo, parent_rev, c)
                    if caller_source:
                        caller_entries.append((c, caller_source))

        bundle = context.build_bundle(bundle_files, caller_entries, max_bytes=args.max_bytes)
        hints = adapter.prompt_hints(selected[0] if selected else "", repo_root=repo)

        group_tests: list = []
        if args.workflow in ("intent", "both"):
            risks, intent_tests = run_intent_aware_bundle(llm, bundle, adapter.lang, hints)
            for t in intent_tests:
                group_tests.append(("intent_aware", risks, t, list(selected)))
        if args.workflow in ("dodgy", "both"):
            dodgy_tests = run_dodgy_diff_bundle(llm, bundle, adapter.lang, hints)
            for t in dodgy_tests:
                group_tests.append(("dodgy_diff", [], t, list(selected)))

        adapter_by_group[gkey] = adapter
        groups.append((
            gkey,
            group_tests,
            "\n\n".join(combined_parent_parts),
            "\n\n".join(combined_diff_parts),
        ))

    return _evaluate_and_report(
        args=args,
        repo=repo,
        parent_rev=parent_rev,
        child_rev=child_rev,
        adapter_by_group=adapter_by_group,
        groups=groups,
        llm=llm,
        meta=meta,
        file_diffs=file_diffs,
    )


def _emit_empty_report(args: argparse.Namespace, meta: Dict[str, str]) -> None:
    out_path = Path(args.out)
    report.write_json([], out_path)
    report.write_markdown([], _md_path_for(out_path), meta=meta, file_diffs={})
    print(report.render_text([]))


def _md_path_for(json_path: Path) -> Path:
    if json_path.suffix.lower() == ".json":
        return json_path.with_suffix(".md")
    return json_path.with_name(json_path.name + ".md")


def _numstat(repo: Path, parent: str, child: str) -> str:
    import subprocess
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff", "--numstat", f"{parent}..{child}"],
        capture_output=True,
        text=True,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _render_callers(repo: Path, rev: str, callers: Sequence[str]) -> str:
    parts = ["# USAGE CONTEXT (do not test directly):"]
    for c in callers:
        src = gitdiff.read_file_at_rev(repo, rev, c)
        if not src:
            continue
        parts.append(f"# --- {c} ---\n{src}")
    return "\n\n".join(parts)


def _evaluate_and_report(
    args: argparse.Namespace,
    repo: Path,
    parent_rev: str,
    child_rev: str,
    adapter_by_group: Dict[int, Adapter],
    groups: List[Tuple[int, list, str, str]],
    llm: LLMClient,
    meta: Optional[Dict[str, str]] = None,
    file_diffs: Optional[Dict[str, str]] = None,
) -> int:
    meta = meta or {}
    file_diffs = file_diffs or {}
    total_tests = sum(len(g[1]) for g in groups)
    if total_tests == 0:
        print("no tests generated", file=sys.stderr)
        _emit_empty_report(args, meta)
        return 0

    candidates: List[CatchCandidate] = []
    with WorktreeSandbox(repo, parent_rev, child_rev) as sb:
        for gkey, group_tests, parent_source_for_judge, diff_for_judge in groups:
            adapter = adapter_by_group[gkey]
            for workflow, risks, test, target_files in group_tests:
                try:
                    parent_res, child_res, _ = evaluate_test(adapter, sb, test, timeout=args.timeout)
                except Exception as e:  # noqa: BLE001
                    print(f"eval error for {test.name}: {e}", file=sys.stderr)
                    continue
                cand = CatchCandidate(
                    workflow=workflow,
                    test=test,
                    risks=list(risks),
                    parent_result=parent_res,
                    child_result=child_res,
                    target_files=list(target_files),
                )
                cand.rule_flags = apply_rules(cand)
                if cand.is_weak_catch and not args.no_judge:
                    try:
                        judge_candidate(llm, cand, parent_source_for_judge, diff_for_judge, adapter.lang)
                    except Exception as e:  # noqa: BLE001
                        print(f"judge error for {test.name}: {e}", file=sys.stderr)
                cand.final_score = score_candidate(cand)
                candidates.append(cand)

    out_path = Path(args.out)
    report.write_json(candidates, out_path)
    md_path = _md_path_for(out_path)
    report.write_markdown(candidates, md_path, meta=meta, file_diffs=file_diffs)
    print(report.render_text(candidates))
    print(f"\nJSON report: {out_path}")
    print(f"Markdown:    {md_path}")
    # LLM call stats — surfaces truncation fast.
    total = getattr(llm, "total_calls", None)
    trunc = getattr(llm, "truncated_calls", None)
    if total is not None:
        log_dir = getattr(llm, "_log_dir", None)
        print(
            f"LLM calls: {total} | truncated (max_tokens): {trunc or 0}"
            + (f" | logs: {log_dir}" if log_dir else ""),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
