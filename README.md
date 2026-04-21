# JitCatch

**The regression-catcher that runs locally, costs nothing, and proves every catch with an executable test.**

JitCatch takes a git diff, asks an LLM to generate unit tests targeting the change, then runs those tests in **two isolated worktrees** — one at the parent commit, one at the child. A test that **passes on parent and fails on child** is a catch: reproducible, reviewable evidence that the diff broke something.

It is not a PR reviewer. It runs the code.

---

## Why this exists

Cursor BugBot, GitHub Copilot Code Review, Qodo Merge, and CodeRabbit all ship the same shape: cloud service, LLM-only, text comments on PRs, hosted-only pricing. None of them execute the code under review.

JitCatch does. The dual-worktree runner is the moat — it produces pass-parent / fail-child evidence that a text-only reviewer cannot manufacture. On top of that runner, JitCatch is:

- **Free.** No API key required. [Ollama](https://ollama.com) is a first-class provider.
- **Local-first.** The whole pipeline runs on a laptop against a local model.
- **Provider-agnostic.** Any OpenAI-compatible endpoint works — LM Studio, vLLM, LocalAI, Groq, OpenRouter, Together, Fireworks. Claude is supported as an optional paid upgrade.

---

## Install

Requires Python ≥ 3.9, `git`, and — for the target repo — `pytest` (Python) or `node` ≥ 18 (JS).

```bash
git clone <this-repo>
cd jitcatch
pip install -e .
```

The `jitcatch` console script is installed.

---

## Configure

JitCatch reads credentials from shell environment variables. Export the one for the provider you want (or add it to your shell rc file):

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # Claude
export OLLAMA_BASE_URL=http://host:11434/v1   # optional Ollama override
export OPENAI_API_KEY=sk-...             # openai-compat (Groq/OpenRouter/...)
```

No `.env` file is loaded — use your shell like any other CLI tool.

## Quickstart — free, local (Ollama)

```bash
ollama pull qwen2.5-coder:7b
ollama serve &

cd /path/to/your/repo
jitcatch pr .
```

`--provider auto` picks `ollama` + `qwen2.5-coder:7b` when `ANTHROPIC_API_KEY` is unset.

## Quickstart — Claude

```bash
export ANTHROPIC_API_KEY=sk-ant-...
jitcatch pr .
```

Auto-picks `anthropic` + `claude-sonnet-4-6`.

## Quickstart — any OpenAI-compatible endpoint

```bash
export OPENAI_API_KEY=sk-...
jitcatch pr . \
  --provider openai-compat \
  --base-url https://api.groq.com/openai/v1 \
  --model llama-3.3-70b-versatile
```

Works against Groq, OpenRouter, Together, Fireworks, LM Studio, vLLM, LocalAI.

---

## What you get

After a run, look in `<repo>/.jitcatch/output/`:

```
.jitcatch/output/
  <name>.json     # machine-readable: summary, candidates[], review_findings[]
  <name>.md       # human-readable report
```

A **weak catch** is a generated test where `parent_result.passed and not child_result.passed`. That is the core signal. The JSON also contains:

- `judge_tp_prob` / `judge_bucket` — LLM-as-judge assessment of whether the catch is a true positive.
- `rule_flags` — deterministic pattern checks (`fp:flakiness`, `fp:reflection`, `tp:null_value`, …) that move the score.
- `final_score` — combined, clamped to `[-1, 1]`.
- `review_findings` — separate channel from the **agentic reviewer** (BugBot-parity): suspicious diffs the LLM flagged even when test-gen couldn't exercise the regression.

---

## Commands

Five subcommands. All share the same pipeline — they differ only in how they pick the parent/child revs.

### `jitcatch pr <repo>` — review a PR

Parent = merge-base(`--base`, HEAD). Autodetects `origin/HEAD` when `--base` is omitted.

```bash
# basic: autodetected base
jitcatch pr .

# explicit base branch
jitcatch pr . --base origin/develop

# include files that import what the diff touches
jitcatch pr . --with-callers --max-callers 10

# cap bundle size on huge PRs
jitcatch pr . --max-files 10 --max-bytes 100000

# named report
jitcatch pr . --filename pr-1234

# full-verbose run with transcripts
jitcatch pr . --verbose --log-dir ./jc-logs
```

### `jitcatch last <repo>` — check the last commit

Parent = `HEAD~1`, child = `HEAD`. Good for post-commit smoke-testing.

```bash
jitcatch last .
jitcatch last . --workflow intent     # skip dodgy-diff; fewer LLM calls
jitcatch last . --no-judge            # skip the judge phase
```

### `jitcatch staged <repo>` — pre-commit check

Parent = `HEAD`, child = a scratch commit containing only the staged index. Does not touch your working tree.

```bash
git add -p
jitcatch staged .

# fast pre-commit: no judge, no review, short timeout
jitcatch staged . --no-judge --no-review --timeout 30
```

### `jitcatch working <repo>` — check uncommitted changes

Parent = `HEAD`, child = a scratch commit containing staged + unstaged changes.

```bash
jitcatch working .
jitcatch working . --workflow dodgy   # mutation-mindset tests only
```

### `jitcatch run <repo> --file F --parent X --child Y` — single file, explicit revs

```bash
# one file, HEAD~1 → HEAD (the default --parent / --child)
jitcatch run . --file src/billing/charge.py

# compare any two refs
jitcatch run . --file src/billing/charge.py \
  --parent v1.4.0 --child feature/new-charge

# pull in caller context
jitcatch run . --file src/billing/charge.py --with-callers
```

### Provider examples (work with any subcommand)

```bash
# Claude (needs ANTHROPIC_API_KEY exported)
jitcatch pr . --provider anthropic --model claude-sonnet-4-6

# Faster/cheaper Claude judge + reviewer, default model for bulk test-gen
jitcatch pr . --model claude-sonnet-4-6 \
  --model-judge claude-haiku-4-5 \
  --model-review claude-haiku-4-5

# Local Ollama
jitcatch pr . --provider ollama --model qwen2.5-coder:14b

# Remote Ollama host
jitcatch pr . --provider ollama --base-url http://10.0.0.5:11434/v1

# Any OpenAI-compatible endpoint (needs OPENAI_API_KEY exported)
jitcatch pr . --provider openai-compat \
  --base-url https://api.groq.com/openai/v1 \
  --model llama-3.3-70b-versatile

# Offline / test mode — reads .jitcatch_stub.json
jitcatch pr . --stub
```

### See all flags

```bash
jitcatch pr --help
jitcatch staged --help
jitcatch run --help
```

## Flags (shared by all subcommands)

**Provider / model**
- `--provider {auto,anthropic,ollama,openai-compat}` — default `auto`. Auto picks `anthropic` if `ANTHROPIC_API_KEY` is set, else `ollama`.
- `--base-url URL` — for `ollama` / `openai-compat`. Defaults to `$OLLAMA_BASE_URL` or `http://localhost:11434/v1`.
- `--model NAME` — default per provider (`claude-sonnet-4-6` for anthropic, `qwen2.5-coder:7b` for ollama).
- `--model-risks`, `--model-tests`, `--model-judge`, `--model-review` — per-stage override; each falls back to `--model`.
- `--max-tokens N` — per-call output cap; defaults to the model's registered ceiling.
- `--stub` — use `StubClient`, reading canned responses from `.jitcatch_stub.json`. Offline. Used by tests.

**Pipeline**
- `--workflow {intent,dodgy,both}` — default `both`.
  - *intent* — infer risks from the diff, then generate tests targeting each risk.
  - *dodgy* — mutation-testing mindset: generate tests that should fail *if* the diff is applied.
- `--no-judge` — skip the LLM-as-judge pass.
- `--no-review` — skip the agentic diff reviewer.
- `--no-retry` / `--max-retries N` / `--max-retry-risks N` — control the feedback-driven retry loop.
- `--skip-validator` — keep every reviewer finding without the LLM validator pass.
- `--timeout SECS` — per-test timeout (default 60).

**Context shaping**
- `--with-callers` / `--max-callers N` — include files that import the target.
- `--max-files N` (default 20) / `--max-bytes N` (default 200000) — bundle size caps.

**Output**
- `--filename NAME` — base name for the report files (no extension). Timestamped default.
- `--log-dir PATH` — per-call LLM transcript directory.
- `--verbose` — debug logs + enable per-call transcripts.

---

## Supported languages

| Language | Extensions | Test framework |
|---|---|---|
| Python | `.py` | `pytest` (via `python -m pytest`) |
| JavaScript | `.js`, `.mjs`, `.cjs` | `node:test` (via `node --test`) |

A new language plugs in by subclassing `jitcatch.adapters.base.Adapter` and registering in `jitcatch/adapters/__init__.py`.

---

## Architecture

```
 diff            ┌──────────────────────┐
 ─────►  revs ──►│ context bundle       │
                 │ (parent sources,     │
                 │  hunks, diffs,       │
                 │  caller context)     │
                 └──────────┬───────────┘
                            │
         ┌──────────────────┼────────────────────┐
         ▼                  ▼                    ▼
   intent-aware         dodgy-diff          agentic
   workflow             workflow            reviewer
   (risks → tests)      (tests → mutants)   (findings)
         │                  │                    │
         └─────────┬────────┘                    │
                   ▼                             │
         WorktreeSandbox                         │
         ┌──────────────┐                        │
         │ parent wtree │◄──┐                    │
         │ child  wtree │◄──┤ run test on both   │
         └──────┬───────┘   │                    │
                ▼           │                    │
         TestResult x2 ─────┘                    │
                │                                │
                ▼                                │
         CatchCandidate                          │
         (is_weak_catch = pass@parent ∧ fail@child)
                │                                │
                ▼                                │
         assessor: rules + judge → final_score   │
                │                                │
                └──────────┬─────────────────────┘
                           ▼
                   report: .json + .md
```

Key modules:

- `jitcatch/cli.py` — argument parsing, provider dispatch.
- `jitcatch/llm.py` — `AnthropicClient`, `OpenAICompatClient`, `StubClient`. Model ceilings, JSON salvage for truncated output.
- `jitcatch/revs.py` — resolves parent/child refs for each subcommand; scratch-worktree handling for `staged`/`working`.
- `jitcatch/context.py` — hunks, caller discovery, bundle assembly with size caps.
- `jitcatch/workflows/` — intent-aware, dodgy-diff, reviewer, retry.
- `jitcatch/runner.py` — `WorktreeSandbox` + `evaluate_test`.
- `jitcatch/assessor/` — deterministic rules + LLM judge → `final_score`.
- `jitcatch/adapters/` — per-language test write + run.
- `jitcatch/report.py` — JSON and Markdown reports under `.jitcatch/output/`.

---

## Environment variables

| Var | Used by | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | anthropic provider | Required for `--provider anthropic`. Presence flips `--provider auto` to anthropic. |
| `OLLAMA_BASE_URL` | ollama provider | Default `http://localhost:11434/v1`. |
| `OPENAI_API_KEY` | openai-compat provider | Optional Bearer token. |

Export in your shell (or rely on CI secrets / your runtime env).

---

## Development

```bash
pip install -e '.[dev]'
pytest tests/
```

75 tests, all offline (stubbed httpx / `StubClient` / tempdir git repos). No network, no API keys needed.

Structure:

- `tests/test_smoke.py` — end-to-end via stub for Python + JS fixtures.
- `tests/test_provider_dispatch.py` — `--provider` resolution + `OpenAICompatClient._complete` over `httpx.MockTransport`.
- `tests/test_revs.py` — rev resolution for `last`/`pr`/`staged`/`working`.
- `tests/test_context.py` — caller discovery, bundle assembly.
- `tests/test_llm_parse.py` — JSON salvage on truncated LLM output.
- `tests/test_reviewer_retry.py` — reviewer + retry loop.
- `tests/test_rules.py` — deterministic assessor rules.
- `tests/test_pr_mode.py` — PR rev resolution integration.

---

## Credits

JitCatch is a free, local-first, open-source implementation of the ideas in Meta's **Just-in-Time Catching Test Generation** research.

- **Paper:** *Just-in-Time Catching Test Generation at Meta* — Matthew Becker et al., 14 authors.
- **Venue:** FSE Companion '26 — 34th ACM International Conference on the Foundations of Software Engineering, Montreal, June 2026.
- **arXiv:** [2601.22832](https://arxiv.org/abs/2601.22832) ([PDF](https://arxiv.org/pdf/2601.22832))
- **Engineering at Meta blog:** [The Death of Traditional Testing: Agentic Development Broke a 50-Year-Old Field, JiT Testing Can Revive It](https://engineering.fb.com/2026/02/11/developer-tools/the-death-of-traditional-testing-agentic-development-jit-testing-revival/)
- **Related:** [Harden and Catch for Just-in-Time Assured LLM-Based Software Testing (arXiv:2504.16472)](https://arxiv.org/html/2504.16472)

Key ideas from the paper that JitCatch implements:

- **Catching, not hardening.** Generated tests are expected to *fail* on the candidate change — they exist to surface regressions, not to pin behavior. A test that passes on both parent and child is noise; a test that passes on parent and fails on child is a catch.
- **Code-change awareness.** Prompts include the diff and hunk context so the LLM targets what actually changed.
- **Layered assessors.** Rule-based filters + LLM-as-judge reduce human review load. Meta reports ~70% reduction; JitCatch mirrors the two-tier design in `jitcatch/assessor/`.
- **Dual-commit evaluation.** Every candidate test runs against the parent revision *and* the child revision in isolated sandboxes. JitCatch uses git worktrees; Meta uses its internal build system.

JitCatch is an independent OSS project and is not affiliated with or endorsed by Meta. All production numbers cited above come from the paper; JitCatch has not reproduced them. If you use JitCatch in research, please cite the paper above alongside this repository.

### Citation

```bibtex
@inproceedings{becker2026jitcatch,
  author    = {Becker, Matthew and others},
  title     = {Just-in-Time Catching Test Generation at Meta},
  booktitle = {Companion Proceedings of the 34th ACM International Conference on the Foundations of Software Engineering (FSE Companion '26)},
  year      = {2026},
  month     = jun,
  address   = {Montreal, Canada},
  publisher = {ACM},
  eprint    = {2601.22832},
  archivePrefix = {arXiv},
  url       = {https://arxiv.org/abs/2601.22832}
}
```
