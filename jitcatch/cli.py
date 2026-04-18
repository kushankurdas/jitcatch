from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

from . import adapters, diff as gitdiff, report
from .assessor import apply_rules, judge_candidate, score_candidate
from .config import CatchCandidate, GeneratedTest
from .llm import AnthropicClient, LLMClient, StubClient
from .runner import WorktreeSandbox, evaluate_test
from .workflows import run_dodgy_diff, run_intent_aware


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jitcatch", description="Just-in-Time Catching test gen (MVP)")
    sub = p.add_subparsers(dest="command")

    r = sub.add_parser("run", help="generate catching tests for a diff")
    r.add_argument("repo", help="path to git repo")
    r.add_argument("--file", required=True, help="source file (repo-relative) under test")
    r.add_argument("--parent", default="HEAD~1", help="parent git rev (default: HEAD~1)")
    r.add_argument("--child", default="HEAD", help="child git rev (default: HEAD)")
    r.add_argument(
        "--workflow",
        choices=["intent", "dodgy", "both"],
        default="both",
    )
    r.add_argument("--stub", action="store_true", help="use StubClient (no API calls)")
    r.add_argument("--model", default="claude-sonnet-4-6")
    r.add_argument("--no-judge", action="store_true", help="skip LLM-as-judge")
    r.add_argument("--timeout", type=int, default=60, help="per-test timeout (s)")
    r.add_argument("--out", default="jitcatch_report.json", help="JSON report path")
    return p


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

    llm: LLMClient = StubClient(repo) if args.stub else AnthropicClient(model=args.model)
    hints = adapter.prompt_hints(args.file)

    all_tests: list[tuple[str, list[str], GeneratedTest]] = []
    if args.workflow in ("intent", "both"):
        risks, intent_tests = run_intent_aware(llm, parent_source, diff_text, adapter.lang, hints)
        for t in intent_tests:
            all_tests.append(("intent_aware", risks, t))
    if args.workflow in ("dodgy", "both"):
        dodgy_tests = run_dodgy_diff(llm, parent_source, diff_text, adapter.lang, hints)
        for t in dodgy_tests:
            all_tests.append(("dodgy_diff", [], t))

    if not all_tests:
        print("no tests generated", file=sys.stderr)
        report.write_json([], Path(args.out))
        print(report.render_text([]))
        return 0

    candidates: list[CatchCandidate] = []
    with WorktreeSandbox(repo, parent_rev, child_rev) as sb:
        for workflow, risks, test in all_tests:
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
            )
            cand.rule_flags = apply_rules(cand)
            if cand.is_weak_catch and not args.no_judge:
                try:
                    judge_candidate(llm, cand, parent_source, diff_text, adapter.lang)
                except Exception as e:  # noqa: BLE001
                    print(f"judge error for {test.name}: {e}", file=sys.stderr)
            cand.final_score = score_candidate(cand)
            candidates.append(cand)

    report.write_json(candidates, Path(args.out))
    print(report.render_text(candidates))
    print(f"\nJSON report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
