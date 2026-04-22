from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple  # noqa: F401

from . import adapters, cache as risk_cache, context, diff as gitdiff, report, revs
from .adapters import Adapter
from .assessor import apply_rules, judge_candidate, score_candidate
from .config import CatchCandidate, GeneratedTest, ReviewFinding
from .llm import (
    AnthropicClient,
    LLMClient,
    OLLAMA_DEFAULT_BASE_URL,
    OLLAMA_DEFAULT_MODEL,
    OllamaClient,
    OpenAICompatClient,
    StubClient,
)
from .runner import WorktreeSandbox, evaluate_test, rerun_child
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
    if cmd == "explain":
        return cmd_explain(args)
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jitcatch",
        description="JitCatch. Free, local-first regression-catcher. "
                    "Generates unit tests from a diff and runs them against "
                    "the parent and child revs in isolated git worktrees.",
    )
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

    # auto-rev bundle subcommands. Same pipeline, different rev selection
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

    # explain. Read the latest report, print the candidate detail, then drop
    # into an interactive LLM chat about that candidate. Non-TTY stdin (pipes,
    # redirected input, CI) skips the chat loop and falls back to the plain
    # detail dump so explain stays scriptable.
    e = sub.add_parser(
        "explain",
        help="show full detail for a candidate by its stable id (prefix "
             "ok), then open an interactive LLM chat about it. Reads "
             "the latest JSON report under .jitcatch/output/ unless "
             "--report is given.",
    )
    e.add_argument("repo", help="path to git repo")
    e.add_argument("id", help="candidate id (prefix match, min 4 chars)")
    e.add_argument(
        "--report",
        default=None,
        help="path to a specific JSON report; defaults to the most "
             "recently modified jitcatch-*.json under the repo's output dir.",
    )
    e.add_argument(
        "--no-chat",
        action="store_true",
        help="print the candidate detail and exit. Skip the chat REPL.",
    )
    _add_llm_args(e)

    return p


def _add_llm_args(p: argparse.ArgumentParser) -> None:
    """LLM provider args needed by any subcommand that calls into an LLM.
    Subset of `_add_shared_args`. No generation/retry/report knobs."""
    p.add_argument("--stub", action="store_true", help="use StubClient (no API calls)")
    p.add_argument(
        "--provider",
        choices=["auto", "anthropic", "ollama", "openai-compat"],
        default="auto",
    )
    p.add_argument("--base-url", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--max-tokens", type=int, default=None)
    p.add_argument("--llm-timeout", type=float, default=120.0)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--log-dir", default=None)
    # Stage-model overrides are unused for chat but `_make_llm` reads them,
    # so declare them as Nones to keep that helper happy.
    p.set_defaults(
        model_risks=None,
        model_tests=None,
        model_judge=None,
        model_review=None,
    )


def _add_shared_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--workflow",
        choices=["intent", "dodgy", "both"],
        default="both",
    )
    p.add_argument("--stub", action="store_true", help="use StubClient (no API calls)")
    p.add_argument(
        "--provider",
        choices=["auto", "anthropic", "ollama", "openai-compat"],
        default="auto",
        help="LLM provider. 'auto' picks 'anthropic' when ANTHROPIC_API_KEY "
             "is set, else 'ollama' (http://localhost:11434/v1). "
             "'openai-compat' accepts any chat-completions endpoint (LM Studio, "
             "vLLM, LocalAI, Groq, OpenRouter, Together, Fireworks, ...) - "
             "pair with --base-url.",
    )
    p.add_argument(
        "--base-url",
        default=None,
        help="base URL for openai-compat / ollama provider. "
             "Defaults: ollama=http://localhost:11434/v1 (overridable via "
             "$OLLAMA_BASE_URL). Required when --provider=openai-compat.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="default model for any stage without a stage-specific override. "
             "Provider-aware defaults: anthropic=claude-sonnet-4-6, "
             "ollama/openai-compat=qwen2.5-coder:7b.",
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
        "--flake-check",
        type=int,
        default=3,
        help="number of extra child re-runs to confirm a failure is "
             "deterministic. If any re-run passes, the candidate is "
             "flagged fp:flake_runtime. Default 3; set to 0 to disable.",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass the risk-inference cache for this run. Default cache "
             "lives at <repo>/.jitcatch/cache/ with 7-day TTL.",
    )
    p.add_argument(
        "--clear-cache",
        action="store_true",
        help="purge the risk-inference cache before running.",
    )
    p.add_argument(
        "--llm-timeout",
        type=float,
        default=120.0,
        help="HTTP read timeout per LLM call for ollama/openai-compat (s). "
             "Raise for slow local models (14b+) or long prompts. Default 120.",
    )
    p.add_argument(
        "--filename",
        default=None,
        help="report base name (no extension). Writes "
             "<repo>/.jitcatch/output/<name>.<ext>. "
             "Auto-generated from timestamp when omitted.",
    )
    p.add_argument(
        "--format",
        dest="output_format",
        default="",
        help="comma-separated human-readable formats: html, md, all. "
             "JSON is always written (required by `jitcatch explain`). "
             "Example: --format html,md.",
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


def cmd_explain(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    report_path = _resolve_explain_report(args, repo)
    if report_path is None:
        print(
            f"error: no JSON report found under {repo / '.jitcatch' / 'output'}. "
            "Run jitcatch against the repo first, or pass --report <path>.",
            file=sys.stderr,
        )
        return 2
    try:
        payload = __import__("json").loads(report_path.read_text())
    except (OSError, ValueError) as e:
        print(f"error: cannot read {report_path}: {e}", file=sys.stderr)
        return 2

    wanted = (args.id or "").strip().lower()
    if len(wanted) < 4:
        print("error: id prefix must be at least 4 characters", file=sys.stderr)
        return 2
    candidates = payload.get("candidates") or []
    matches = [c for c in candidates if str(c.get("id", "")).startswith(wanted)]
    if not matches:
        print(f"error: no candidate matching id prefix '{wanted}' in {report_path}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: id prefix '{wanted}' is ambiguous - {len(matches)} matches:", file=sys.stderr)
        for c in matches:
            print(f"  {c.get('id')}  {c.get('test', {}).get('name')}", file=sys.stderr)
        return 1

    cand = matches[0]

    if getattr(args, "no_chat", False):
        print(_format_explain(cand, report_path))
        return 0
    # Skip the REPL when stdin isn't a tty (pipes, redirected input, CI) -
    # keeps explain scriptable and avoids spinning up an LLM client for
    # no reason. Fall back to the plain detail dump in that case.
    if not sys.stdin.isatty():
        print(_format_explain(cand, report_path))
        return 0
    return _run_explain_chat(args, repo, cand, payload)


class _Style:
    """ANSI styling helper. Colors only when stdout is a TTY and NO_COLOR
    isn't set. Keeps piped/CI output clean."""

    def __init__(self, enabled: bool) -> None:
        self.on = enabled

    def _w(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.on else s

    def dim(self, s: str) -> str: return self._w("2", s)
    def bold(self, s: str) -> str: return self._w("1", s)
    def cyan(self, s: str) -> str: return self._w("36", s)
    def green(self, s: str) -> str: return self._w("32", s)
    def yellow(self, s: str) -> str: return self._w("33", s)
    def magenta(self, s: str) -> str: return self._w("35", s)
    def red(self, s: str) -> str: return self._w("31", s)


def _run_explain_chat(
    args: argparse.Namespace,
    repo: Path,
    cand: dict,
    payload: dict,
) -> int:
    st = _Style(sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
    try:
        llm = _make_llm(args, repo)
    except Exception as e:  # noqa: BLE001
        print(st.red(f"\n✗ cannot start chat: {e}"), file=sys.stderr)
        return 0
    system = _explain_system_prompt(cand, payload.get("meta") or {})
    messages: List[dict] = []
    _print_chat_banner(cand, st)
    you = st.bold(st.cyan("you")) + st.dim(" ❯ ")
    llm_label = st.bold(st.green("llm")) + st.dim(" ❯ ")
    while True:
        try:
            line = input(f"\n{you}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line or line.lower() in ("exit", "quit", ":q"):
            print(st.dim("bye."))
            break
        messages.append({"role": "user", "content": line})
        if st.on:
            sys.stdout.write(st.dim("\n  thinking…"))
            sys.stdout.flush()
        try:
            reply = llm.chat(system, messages, label="explain.chat")
        except Exception as e:  # noqa: BLE001
            if st.on:
                sys.stdout.write("\r\033[K")
            print(st.red(f"✗ {e}"), file=sys.stderr)
            messages.pop()
            continue
        if st.on:
            sys.stdout.write("\r\033[K")
        messages.append({"role": "assistant", "content": reply})
        print(f"\n{llm_label}{reply.rstrip()}")
    return 0


def _print_chat_banner(cand: dict, st: "_Style") -> None:
    test = cand.get("test") or {}
    cid = str(cand.get("id") or "")[:12]
    name = test.get("name") or "?"
    workflow = cand.get("workflow") or "?"
    bucket = cand.get("judge_bucket") or ""
    score = cand.get("final_score")
    weak = cand.get("is_weak_catch")

    bar = st.dim("─" * 60)
    title = st.bold(st.magenta("jitcatch explain"))
    meta_bits = [st.cyan(cid), st.bold(name), st.dim(workflow)]
    if bucket:
        meta_bits.append(st.yellow(f"bucket={bucket}"))
    if isinstance(score, (int, float)):
        meta_bits.append(st.dim(f"score={score:+.2f}"))
    if weak:
        meta_bits.append(st.green("weak-catch"))
    print()
    print(bar)
    print(f"  {title}  {'  '.join(meta_bits)}")
    print(bar)
    print(st.dim("  ask about this candidate. empty line, 'exit', or Ctrl-D to quit."))


def _explain_system_prompt(cand: dict, meta: dict) -> str:
    """Seed the chat with the full candidate record so the LLM can reason
    about the finding without a second tool call. The detail dump the user
    already saw is included verbatim so follow-ups like "why did the child
    fail?" or "is this a real regression?" can be answered grounded in the
    same text."""
    import json as _json
    test = cand.get("test") or {}
    parent = cand.get("parent_result") or {}
    child = cand.get("child_result") or {}
    context_lines = [
        "You are a senior engineer helping the user understand a jitcatch "
        "regression-test candidate. Ground every answer in the JSON context "
        "below. If the data doesn't contain the answer, say so. Don't "
        "speculate about code you haven't been shown. Be concise.",
        "",
        "--- RUN META ---",
        _json.dumps(meta, indent=2, default=str),
        "",
        "--- CANDIDATE ---",
        f"id: {cand.get('id')}",
        f"workflow: {cand.get('workflow')}",
        f"weak_catch: {cand.get('is_weak_catch')}",
        f"final_score: {cand.get('final_score')}",
        f"judge: tp_prob={cand.get('judge_tp_prob')} "
        f"bucket={cand.get('judge_bucket')}",
        f"target_files: {cand.get('target_files')}",
        f"flags: {cand.get('rule_flags')}",
        f"risks: {cand.get('risks')}",
        "",
        f"-- rationale --\n{cand.get('judge_rationale') or test.get('rationale') or ''}",
        "",
        f"-- test name --\n{test.get('name')}",
        "",
        f"-- test code --\n{test.get('code') or ''}",
        "",
        f"-- parent result (status={parent.get('status')} "
        f"exit_code={parent.get('exit_code')}) --",
        f"stdout:\n{parent.get('stdout') or ''}",
        f"stderr:\n{parent.get('stderr') or ''}",
        "",
        f"-- child result (status={child.get('status')} "
        f"exit_code={child.get('exit_code')}) --",
        f"stdout:\n{child.get('stdout') or ''}",
        f"stderr:\n{child.get('stderr') or ''}",
    ]
    return "\n".join(context_lines)


def _resolve_explain_report(args: argparse.Namespace, repo: Path) -> Optional[Path]:
    if args.report:
        p = Path(args.report).expanduser().resolve()
        return p if p.exists() else None
    out_dir = repo / ".jitcatch" / "output"
    if not out_dir.exists():
        return None
    # Pick the newest jitcatch-*.json by mtime. Matches the default
    # naming scheme from `_resolve_output_paths`.
    candidates = sorted(
        out_dir.glob("jitcatch-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _format_explain(cand: dict, report_path: Path) -> str:
    """Render a single candidate as a human-readable block. Pulls every
    field the report persists. Test code, parent/child output, judge
    rationale, rule flags, risks. So the reader has everything needed
    to decide on the finding without another tool."""
    test = cand.get("test") or {}
    parent = cand.get("parent_result") or {}
    child = cand.get("child_result") or {}
    flags = cand.get("rule_flags") or []
    risks = cand.get("risks") or []
    target_files = cand.get("target_files") or []

    lines: List[str] = []
    lines.append(f"id:          {cand.get('id')}")
    lines.append(f"name:        {test.get('name')}")
    lines.append(f"workflow:    {cand.get('workflow')}")
    lines.append(f"weak_catch:  {cand.get('is_weak_catch')}")
    lines.append(f"final_score: {cand.get('final_score', 0):+.3f}")
    lines.append(
        f"judge:       tp_prob={cand.get('judge_tp_prob', 0):+.2f} "
        f"bucket={cand.get('judge_bucket', '')}"
    )
    if target_files:
        lines.append(f"files:       {', '.join(target_files)}")
    if flags:
        lines.append(f"flags:       {', '.join(flags)}")
    if risks:
        lines.append("risks:")
        for r in risks:
            lines.append(f"  - {r}")
    rationale = cand.get("judge_rationale") or test.get("rationale") or ""
    if rationale:
        lines.append("")
        lines.append("-- rationale --")
        lines.append(rationale.rstrip())
    code = test.get("code") or ""
    if code:
        lines.append("")
        lines.append("-- test code --")
        lines.append(code.rstrip())
    for label, res in (("parent", parent), ("child", child)):
        status = res.get("status", "")
        ec = res.get("exit_code", "")
        stdout = (res.get("stdout") or "").rstrip()
        stderr = (res.get("stderr") or "").rstrip()
        lines.append("")
        lines.append(f"-- {label} result (status={status}, exit_code={ec}) --")
        if stdout:
            lines.append("stdout:")
            lines.append(stdout)
        if stderr:
            lines.append("stderr:")
            lines.append(stderr)
    lines.append("")
    lines.append(f"source: {report_path}")
    return "\n".join(lines)


_PRES_FORMATS = frozenset({"html", "md"})
_VALID_FORMATS = _PRES_FORMATS | {"json", "all"}


def _formats(args: argparse.Namespace) -> set:
    """Parse --format into a set of human-readable formats to emit in
    addition to the always-on JSON report. 'all' expands to every
    presentation format. 'json' is accepted but a no-op since JSON is
    always written. Unknown values raise SystemExit so a typo doesn't
    silently skip the format the user wanted."""
    raw = getattr(args, "output_format", "") or ""
    values = {v.strip().lower() for v in raw.split(",") if v.strip()}
    if "all" in values:
        return set(_PRES_FORMATS)
    bad = values - _VALID_FORMATS
    if bad:
        print(
            f"error: --format got unknown value(s) {sorted(bad)}; "
            f"valid: html, md, all (json is always written)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return values & _PRES_FORMATS


def _cache_repo(args: argparse.Namespace, repo: Path) -> Optional[Path]:
    """Return the repo path used for risk-cache lookup, or None when
    caching is disabled. --no-cache bypasses both read and write."""
    if getattr(args, "no_cache", False):
        return None
    return repo


def _cache_model(llm: LLMClient) -> str:
    """Identify the risks-stage model for cache keying. Falls back to
    the client's default model when no per-stage override is set, and
    to empty string for clients (Stub) without a model attr."""
    stage = getattr(llm, "_stage_models", None) or {}
    return stage.get("risks") or getattr(llm, "_model", "") or ""


def _resolve_provider(requested: str) -> str:
    """Resolve --provider=auto to a concrete provider based on env. Keeps
    the historical Claude path for users with ANTHROPIC_API_KEY set, and
    falls back to local Ollama so the tool works with zero config."""
    if requested != "auto":
        return requested
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "ollama"


def _make_llm(args: argparse.Namespace, repo: Path) -> LLMClient:
    if args.stub:
        return StubClient(repo)
    log_dir: Optional[Path] = None
    if args.log_dir:
        log_dir = Path(args.log_dir)
    elif args.verbose:
        log_dir = repo / ".jitcatch" / "logs"

    provider = _resolve_provider(getattr(args, "provider", "auto"))
    # Provider-aware default model. Explicit --model always wins.
    default_model = (
        "claude-sonnet-4-6" if provider == "anthropic" else OLLAMA_DEFAULT_MODEL
    )
    model = args.model or default_model
    stage_models = {
        "risks": args.model_risks or model,
        "tests": args.model_tests or model,
        "judge": args.model_judge or model,
        "review": getattr(args, "model_review", None) or model,
    }

    if provider == "anthropic":
        return AnthropicClient(
            model=model,
            max_tokens=args.max_tokens,
            verbose=args.verbose,
            log_dir=log_dir,
            stage_models=stage_models,
        )

    if provider in ("ollama", "openai-compat"):
        base_url = args.base_url
        if not base_url:
            if provider == "ollama":
                base_url = os.environ.get("OLLAMA_BASE_URL", OLLAMA_DEFAULT_BASE_URL)
            else:
                raise RuntimeError(
                    "--base-url required for --provider=openai-compat "
                    "(or use --provider=ollama for localhost:11434)"
                )
        api_key = os.environ.get("OPENAI_API_KEY")
        # Route ollama through native /api/chat so `format: "json"` and
        # num_ctx are honored. The OpenAI-compat /v1 shim silently drops
        # both, which is what caused deepseek-coder-v2:16b to produce
        # prose summaries instead of the required JSON schemas. The
        # generic openai-compat path (Groq / Together / vLLM) keeps the
        # /v1 chat-completions transport. Those providers often reject
        # Ollama-specific fields.
        client_cls = OllamaClient if provider == "ollama" else OpenAICompatClient
        return client_cls(
            model=model,
            base_url=base_url,
            api_key=api_key,
            max_tokens=args.max_tokens,
            verbose=args.verbose,
            log_dir=log_dir,
            stage_models=stage_models,
            timeout=args.llm_timeout,
        )

    raise RuntimeError(f"unknown provider: {provider!r}")


def cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"error: {repo} is not a git repo", file=sys.stderr)
        return 2

    if getattr(args, "clear_cache", False):
        n = risk_cache.clear_cache(repo)
        print(f"[JitCatch] cache cleared: {n} entries removed", file=sys.stderr)

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
        risks, intent_tests = run_intent_aware(
            llm, parent_source, diff_text, adapter.lang, hints,
            cache_repo=_cache_repo(args, repo),
            cache_model=_cache_model(llm),
        )
        for t in intent_tests:
            all_tests.append(("intent_aware", risks, t, [args.file]))
    if args.workflow in ("dodgy", "both"):
        dodgy_tests = run_dodgy_diff(llm, parent_source, diff_text, adapter.lang, hints)
        for t in dodgy_tests:
            all_tests.append(("dodgy_diff", [], t, [args.file]))

    meta = _build_meta(args, repo, parent_rev, child_rev)
    file_diffs = {args.file: diff_text}
    # Single-file "bundle" for reviewer + retry. Same shape as bundle
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

    if getattr(args, "clear_cache", False):
        n = risk_cache.clear_cache(repo)
        print(f"[JitCatch] cache cleared: {n} entries removed", file=sys.stderr)

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
    print(f"[JitCatch] {args.command}: {pair.description}  (parent={parent_rev[:8]} child={child_rev[:8]})", file=sys.stderr)

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
            group_risks, intent_tests = run_intent_aware_bundle(
                llm, bundle, adapter.lang, hints,
                cache_repo=_cache_repo(args, repo),
                cache_model=_cache_model(llm),
            )
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
    json_path, md_path, html_path = _resolve_output_paths(args, repo)
    fmts = _formats(args)
    report.write_json([], json_path, findings=findings or [])
    if "md" in fmts:
        report.write_markdown([], md_path, meta=meta, file_diffs={}, findings=findings or [])
    if "html" in fmts:
        report.write_html([], html_path, meta=meta, file_diffs={}, findings=findings or [])
    print(report.render_text([], findings=findings or []))


def _resolve_output_paths(args: argparse.Namespace, repo: Path) -> Tuple[Path, Path, Path]:
    """Return (json_path, md_path, html_path) under <repo>/.jitcatch/output/.
    Filename comes from --filename (stem only); falls back to a
    timestamped default. The output directory is created on demand.
    All three formats are always written."""
    import time as _time
    name = (args.filename or "").strip()
    if not name:
        name = f"jitcatch-{_time.strftime('%Y%m%d-%H%M%S')}"
    name = Path(name).name
    if name.lower().endswith((".json", ".md", ".html")):
        name = Path(name).stem
    out_dir = repo / ".jitcatch" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{name}.json", out_dir / f"{name}.md", out_dir / f"{name}.html"


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
        parent_res, child_res, child_art = evaluate_test(adapter, sb, test, timeout=args.timeout)
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
    # Runtime flake detector. When the initial child run failed (weak-catch
    # shape), re-run N extra times against the same artifact. If any re-run
    # passes, the failure is non-deterministic (timing / ordering / network
    # race leaked past the static FP_FLAKY_PAT regex) and we demote the
    # candidate with fp:flake_runtime. Skips when disabled (--flake-check 0)
    # or when the initial run didn't fail (nothing to confirm).
    flake_check = int(getattr(args, "flake_check", 0) or 0)
    if flake_check > 0 and child_res is not None and not child_res.passed:
        try:
            reruns = rerun_child(adapter, sb, child_art, timeout=args.timeout, n=flake_check)
        except Exception as e:  # noqa: BLE001
            print(f"flake check error for {test.name}: {e}", file=sys.stderr)
            reruns = []
        if any(r.passed for r in reruns):
            if "fp:flake_runtime" not in cand.rule_flags:
                cand.rule_flags.append("fp:flake_runtime")
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
    # language group. A gap in the JS group shouldn't trigger a retry
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

    # Agentic reviewer. Runs independently of test-gen. Happens before the
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

    usage = getattr(llm, "usage", None)
    json_path, md_path, html_path = _resolve_output_paths(args, repo)
    fmts = _formats(args)
    report.write_json(candidates, json_path, findings=findings, usage=usage)
    if "md" in fmts:
        report.write_markdown(
            candidates, md_path, meta=meta, file_diffs=file_diffs,
            findings=findings, usage=usage,
        )
    if "html" in fmts:
        report.write_html(
            candidates, html_path, meta=meta, file_diffs=file_diffs,
            findings=findings, usage=usage,
        )
    print(report.render_text(candidates, findings=findings))
    print()
    print(f"JSON report: {json_path}")
    if "md" in fmts:
        print(f"Markdown:    {md_path}")
    if "html" in fmts:
        print(f"HTML:        {html_path}")
    # LLM call stats. Surfaces truncation fast.
    total = getattr(llm, "total_calls", None)
    trunc = getattr(llm, "truncated_calls", None)
    if total is not None:
        log_dir = getattr(llm, "_log_dir", None)
        print(
            f"LLM calls: {total} | truncated (max_tokens): {trunc or 0}"
            + (f" | logs: {log_dir}" if log_dir else ""),
            file=sys.stderr,
        )
    _print_usage_footer(usage)
    return 0


def _print_usage_footer(usage) -> None:
    """Token + cost summary. Skip when no calls were made (StubClient)."""
    if usage is None or usage.calls == 0:
        return
    print(
        f"Tokens: in={usage.input_tokens:,} out={usage.output_tokens:,} "
        f"| cost: ${usage.cost_usd:.4f}",
        file=sys.stderr,
    )
    if usage.by_stage:
        parts = []
        for stage, s in sorted(usage.by_stage.items()):
            parts.append(
                f"{stage}={s['input_tokens']:,}/{s['output_tokens']:,}"
                + (f" (${s['cost_usd']:.4f})" if s["cost_usd"] > 0 else "")
            )
        print(f"  by stage (in/out): {' | '.join(parts)}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
