from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple  # noqa: F401

from . import adapters, context, diff as gitdiff, report, revs
from .adapters import Adapter
from .assessor import apply_rules, judge_candidate, score_candidate
from .config import CatchCandidate, GeneratedTest, ReviewFinding
from .llm import AnthropicClient, LLMClient, StubClient
from .runner import WorktreeSandbox, evaluate_test
from .workflows import (
    find_gaps,
    run_agentic_reviewer,
    run_dodgy_diff,
    run_dodgy_diff_bundle,
    run_intent_aware,
    run_intent_aware_bundle,
    run_retry_round,
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
    p.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="default model for any stage without a stage-specific override.",
    )
    p.add_argument(
        "--model-risks",
        default=None,
        help="model for risk inference (reasoning-heavy). Defaults to --model.",
    )
    p.add_argument(
        "--model-tests",
        default=None,
        help="model for test generation (bulk output). Defaults to --model.",
    )
    p.add_argument(
        "--model-judge",
        default=None,
        help="model for judging weak catches (reasoning-heavy). Defaults to --model.",
    )
    p.add_argument(
        "--model-review",
        default=None,
        help="model for agentic diff review (reasoning-heavy). Defaults to --model.",
    )
    p.add_argument("--no-judge", action="store_true", help="skip LLM-as-judge")
    p.add_argument(
        "--no-review",
        action="store_true",
        help="skip agentic reviewer pass (BugBot-style diff reasoning). "
             "By default the reviewer runs alongside test-gen and surfaces "
             "bugs that produce no failing test (mocks, env-coupled paths).",
    )
    p.add_argument(
        "--no-retry",
        action="store_true",
        help="skip feedback-driven retry loop for risks that no weak catch covered.",
    )
    p.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="max retry rounds for uncaught risks (default: 2).",
    )
    p.add_argument(
        "--max-retry-risks",
        type=int,
        default=8,
        help="cap on risks targeted per retry round (default: 8). Bounds LLM cost.",
    )
    p.add_argument(
        "--skip-validator",
        action="store_true",
        help="skip validator pass on reviewer findings (keep every flag).",
    )
    p.add_argument("--timeout", type=int, default=60, help="per-test timeout (s)")
    p.add_argument(
        "--filename",
        default=None,
        help="report base name (no extension). Writes "
             "<repo>/.jitcatch/output/<name>.json and .md. "
             "Auto-generated from timestamp when omitted.",
    )
    p.add_argument("--verbose", action="store_true", help="print debug info (empty LLM responses, etc.)")
    p.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="per-call output token cap. Defaults to the current call's model "
             "ceiling (32k opus, 64k sonnet/haiku). Pass a lower number to save "
             "spend at the risk of truncation (stop_reason=max_tokens).",
    )
    p.add_argument(
        "--log-dir",
        default=None,
        help="override directory for per-call LLM transcripts (default: "
             "<repo>/.jitcatch/logs/ when --verbose is on). Logs are untruncated.",
    )


def _make_llm(args: argparse.Namespace, repo: Path) -> LLMClient:
    if args.stub:
        return StubClient(repo)
    log_dir: Optional[Path] = None
    if args.log_dir:
        log_dir = Path(args.log_dir)
    elif args.verbose:
        log_dir = repo / ".jitcatch" / "logs"
    stage_models = {
        "risks": args.model_risks or args.model,
        "tests": args.model_tests or args.model,
        "judge": args.model_judge or args.model,
        "review": getattr(args, "model_review", None) or args.model,
    }
    return AnthropicClient(
        model=args.model,
        max_tokens=args.max_tokens,
        verbose=args.verbose,
        log_dir=log_dir,
        stage_models=stage_models,
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
    risks: list[str] = []
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
    # Single-file "bundle" for reviewer + retry — same shape as bundle
    # workflow, just with one file.
    review_bundle = context.build_bundle(
        [(args.file, parent_source, diff_text)], [], max_bytes=context.MAX_BYTES_DEFAULT
    )
    group_contexts = {
        0: {
            "bundle": review_bundle,
            "hints": hints,
            "lang": adapter.lang,
            "risks": risks,
        }
    }
    return _evaluate_and_report(
        args=args,
        repo=repo,
        parent_rev=parent_rev,
        child_rev=child_rev,
        adapter_by_group={0: adapter},
        groups=[(0, all_tests, parent_source, diff_text)],
        group_contexts=group_contexts,
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
    group_contexts: Dict[int, dict] = {}
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
        group_risks: list = []
        if args.workflow in ("intent", "both"):
            group_risks, intent_tests = run_intent_aware_bundle(llm, bundle, adapter.lang, hints)
            for t in intent_tests:
                group_tests.append(("intent_aware", group_risks, t, list(selected)))
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
        group_contexts[gkey] = {
            "bundle": bundle,
            "hints": hints,
            "lang": adapter.lang,
            "risks": list(group_risks),
            "selected": list(selected),
        }

    return _evaluate_and_report(
        args=args,
        repo=repo,
        parent_rev=parent_rev,
        child_rev=child_rev,
        adapter_by_group=adapter_by_group,
        groups=groups,
        group_contexts=group_contexts,
        llm=llm,
        meta=meta,
        file_diffs=file_diffs,
    )


def _emit_empty_report(
    args: argparse.Namespace,
    meta: Dict[str, str],
    findings: Optional[List[ReviewFinding]] = None,
) -> None:
    repo = Path(meta.get("repo") or args.repo).resolve()
    json_path, md_path = _resolve_output_paths(args, repo)
    report.write_json([], json_path, findings=findings or [])
    report.write_markdown([], md_path, meta=meta, file_diffs={}, findings=findings or [])
    print(report.render_text([], findings=findings or []))


def _resolve_output_paths(args: argparse.Namespace, repo: Path) -> Tuple[Path, Path]:
    """Return (json_path, md_path) under <repo>/.jitcatch/output/.
    Filename comes from --filename (stem only); falls back to a
    timestamped default. The output directory is created on demand."""
    import time as _time
    name = (args.filename or "").strip()
    if not name:
        name = f"jitcatch-{_time.strftime('%Y%m%d-%H%M%S')}"
    # Strip accidental extensions / path separators.
    name = Path(name).name
    if name.lower().endswith((".json", ".md")):
        name = Path(name).stem
    out_dir = repo / ".jitcatch" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{name}.json", out_dir / f"{name}.md"


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


def _eval_one_test(
    adapter: Adapter,
    sb: WorktreeSandbox,
    test: GeneratedTest,
    workflow: str,
    risks: list,
    target_files: list,
    parent_source_for_judge: str,
    diff_for_judge: str,
    llm: LLMClient,
    args: argparse.Namespace,
) -> Optional[CatchCandidate]:
    try:
        parent_res, child_res, _ = evaluate_test(adapter, sb, test, timeout=args.timeout)
    except Exception as e:  # noqa: BLE001
        print(f"eval error for {test.name}: {e}", file=sys.stderr)
        return None
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
    return cand


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
    group_contexts: Optional[Dict[int, dict]] = None,
) -> int:
    meta = meta or {}
    file_diffs = file_diffs or {}
    group_contexts = group_contexts or {}
    total_tests = sum(len(g[1]) for g in groups)

    candidates: List[CatchCandidate] = []
    # Per-group lists to drive retry-loop gap detection independently per
    # language group — a gap in the JS group shouldn't trigger a retry
    # against Python context.
    group_cands: Dict[int, List[CatchCandidate]] = {g[0]: [] for g in groups}

    run_review = not getattr(args, "no_review", False)
    run_retry = not getattr(args, "no_retry", False)
    max_retries = int(getattr(args, "max_retries", 2) or 0)
    skip_validator = bool(getattr(args, "skip_validator", False))
    max_retry_risks = int(getattr(args, "max_retry_risks", 8) or 8)

    findings: List[ReviewFinding] = []

    # Short-circuit: if no tests AND reviewer disabled, nothing to do.
    if total_tests == 0 and not run_review:
        print("no tests generated", file=sys.stderr)
        _emit_empty_report(args, meta)
        return 0

    # Agentic reviewer — runs independently of test-gen. Happens before the
    # sandbox is created so we can short-circuit when test-gen was empty.
    if run_review:
        for gkey, _tests, _parent, _diff in groups:
            ctx = group_contexts.get(gkey) or {}
            bundle = ctx.get("bundle", "")
            lang = ctx.get("lang") or adapter_by_group[gkey].lang
            if not bundle:
                continue
            try:
                group_findings = run_agentic_reviewer(
                    llm, bundle=bundle, lang=lang, skip_validator=skip_validator
                )
            except Exception as e:  # noqa: BLE001
                print(f"reviewer error (group {gkey}): {e}", file=sys.stderr)
                group_findings = []
            findings.extend(group_findings)

    if total_tests == 0 and not findings:
        print("no tests generated", file=sys.stderr)
        _emit_empty_report(args, meta)
        return 0

    if total_tests > 0:
        with WorktreeSandbox(repo, parent_rev, child_rev) as sb:
            for gkey, group_tests, parent_source_for_judge, diff_for_judge in groups:
                adapter = adapter_by_group[gkey]
                for workflow, risks, test, target_files in group_tests:
                    cand = _eval_one_test(
                        adapter, sb, test, workflow, risks, target_files,
                        parent_source_for_judge, diff_for_judge, llm, args,
                    )
                    if cand is not None:
                        candidates.append(cand)
                        group_cands[gkey].append(cand)

            # Feedback-driven retry rounds. For each group, find risks with
            # no weak catch and ask the LLM for another test with failure
            # feedback. Cap by max_retries and max_retry_risks to bound cost.
            if run_retry and max_retries > 0:
                for round_idx in range(max_retries):
                    added_any = False
                    for gkey, group_tests, parent_source_for_judge, diff_for_judge in groups:
                        ctx = group_contexts.get(gkey) or {}
                        risks_all = ctx.get("risks") or []
                        if not risks_all:
                            continue
                        gaps = find_gaps(risks_all, group_cands[gkey])
                        if not gaps:
                            continue
                        bundle = ctx.get("bundle", "")
                        hints = ctx.get("hints", "")
                        adapter = adapter_by_group[gkey]
                        selected = ctx.get("selected") or []
                        try:
                            new_tests = run_retry_round(
                                llm,
                                bundle=bundle,
                                lang=adapter.lang,
                                hints=hints,
                                gaps=gaps,
                                max_gaps=max_retry_risks,
                            )
                        except Exception as e:  # noqa: BLE001
                            print(f"retry error (group {gkey}, round {round_idx}): {e}", file=sys.stderr)
                            new_tests = []
                        for risk_text, t in new_tests:
                            cand = _eval_one_test(
                                adapter, sb, t,
                                f"retry_r{round_idx+1}",
                                [risk_text],
                                selected or [],
                                parent_source_for_judge, diff_for_judge,
                                llm, args,
                            )
                            if cand is not None:
                                candidates.append(cand)
                                group_cands[gkey].append(cand)
                                added_any = True
                    if not added_any:
                        break

    json_path, md_path = _resolve_output_paths(args, repo)
    report.write_json(candidates, json_path, findings=findings)
    report.write_markdown(
        candidates, md_path, meta=meta, file_diffs=file_diffs, findings=findings
    )
    print(report.render_text(candidates, findings=findings))
    print(f"\nJSON report: {json_path}")
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
