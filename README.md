# JitCatch

> Free, local-first regression-catcher. Generates unit tests from a diff, then runs them against the parent and child revs in isolated git worktrees — a test that passes on the parent and fails on the child is executable evidence of a regression.

JitCatch implements ideas from Meta's [*Just-in-Time Catching Test Generation at Meta*](https://arxiv.org/abs/2601.22832) (Becker et al., FSE Companion '26), adapted for local developer loops and open-source LLM backends.

[![CI](https://github.com/kushankurdas/jitcatch/actions/workflows/ci.yml/badge.svg)](https://github.com/kushankurdas/jitcatch/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)

---

## Table of contents

- [Why JitCatch](#why-jitcatch)
- [How it works](#how-it-works)
- [Installation](#installation)
- [Quick start](#quick-start)
- [CLI reference](#cli-reference)
- [LLM providers](#llm-providers)
- [Workflows](#workflows)
- [Output](#output)
- [Project layout](#project-layout)
- [Supported languages](#supported-languages)
- [Configuration tips](#configuration-tips)
- [Development](#development)
- [Security](#security)
- [License](#license)

---

## Why JitCatch

Most LLM-generated-test tools stop once a test compiles. That's cheap theater — an untested test is just a plausible-looking string. JitCatch enforces a stronger bar:

1. **Generate** a test targeting the diff.
2. **Write** it into two detached git worktrees — one at the parent rev, one at the child.
3. **Run** it in both worktrees.
4. **Rank** candidates: a test that **passes on parent** and **fails on child** is a weak catch — real, reproducible evidence that the diff changed behavior.

The "weak catch" is the core invariant. Everything else in the pipeline exists to improve the signal-to-noise ratio on top of it:

- Rule-based assessors flag common false-positive patterns (`fp:reflection`, `fp:flakiness`, `fp:broken_test_runner`).
- An LLM-as-judge pass scores each weak catch (`tp_prob`, `bucket`, rationale).
- A feedback-driven retry loop targets risks the first round missed, with the prior test's failure output in the prompt.
- An agentic reviewer channel surfaces bugs that test-gen can't reach (mocks, env-coupled paths, untested symbols) — opinion-only findings, kept in a separate section.

**Design goals:** local-first, zero-config for Ollama, no API keys required for the full offline path (`--stub`), and deterministic wherever the signal can be expressed as a pattern instead of a prompt.

---

## How it works

```
┌──────────────────────────────────────────────────────────────────────┐
│  jitcatch pr <repo>                                                  │
└──────────────────────────────────────────────────────────────────────┘
            │
            ▼
   ┌──────────────────┐     ┌──────────────────────┐
   │  revs.resolve    │────▶│  (parent, child)     │   (HEAD~1..HEAD,
   │  pr | last |     │     │   RevPair            │    merge-base,
   │  staged | working│     │                      │    scratch commit)
   └──────────────────┘     └──────────────────────┘
            │
            ▼
   ┌──────────────────┐
   │  context.bundle  │     Group changed files by language adapter.
   │  (top-N by churn)│     Build a single prompt per group.
   └──────────────────┘
            │
            ├────────────────────────────────┐
            ▼                                ▼
   ┌──────────────────┐              ┌────────────────────┐
   │ workflows/intent │              │  workflows/dodgy   │
   │ risks → tests    │              │  mutation-mindset  │
   └──────────────────┘              └────────────────────┘
            │                                │
            └────────────────┬───────────────┘
                             ▼
                   ┌────────────────────┐
                   │ WorktreeSandbox    │  git worktree add --detach
                   │   parent / child   │  run each test in both
                   └────────────────────┘
                             │
                             ▼
                   ┌────────────────────┐      ┌──────────────────────┐
                   │ assessor/rules     │      │ workflows/reviewer   │
                   │ fp:* / tp:* flags  │      │ BugBot-style diff    │
                   └────────────────────┘      │ review (+ validator) │
                             │                 └──────────────────────┘
                             ▼                           │
                   ┌────────────────────┐                │
                   │ assessor/judge     │                │
                   │ tp_prob, bucket    │                │
                   └────────────────────┘                │
                             │                           │
                             ▼                           │
                   ┌────────────────────┐                │
                   │ workflows/retry    │                │
                   │ feedback loop for  │                │
                   │ uncaught risks     │                │
                   └────────────────────┘                │
                             │                           │
                             └─────────────┬─────────────┘
                                           ▼
                                  ┌─────────────────┐
                                  │  report (json + │
                                  │  markdown)      │
                                  └─────────────────┘
```

---

## Installation

Requires **Python ≥ 3.9**, **git**, and (for the JavaScript adapter) **Node ≥ 18**.

```bash
git clone https://github.com/kushankurdas/jitcatch
cd jitcatch
pip install -e '.[dev]'
```

This installs the `jitcatch` console script. Verify:

```bash
jitcatch --help
```

---

## Quick start

Run against a pull request, using the repo's origin default branch as the base:

```bash
cd /path/to/your/repo
jitcatch pr .
```

Output lands in `.jitcatch/output/` (JSON + Markdown) and a summary is printed to stdout.

### Try it offline first

No API key, no network — uses the built-in `StubClient`:

```bash
jitcatch pr . --stub
```

### Point at a local model via Ollama

Zero config when Ollama is running on the default port:

```bash
ollama pull qwen2.5-coder:7b
jitcatch pr . --provider ollama
```

### Use Claude

```bash
export ANTHROPIC_API_KEY=sk-ant-...
jitcatch pr . --provider anthropic
# or just:
jitcatch pr .   # --provider=auto picks anthropic when ANTHROPIC_API_KEY is set
```

---

## CLI reference

```
jitcatch <subcommand> <repo> [options]
```

### Subcommands

| Subcommand | Parent rev | Child rev | Use case |
|---|---|---|---|
| `last` | `HEAD~1` | `HEAD` | Smoke-test the commit you just made |
| `pr [--base <ref>]` | `merge-base(base, HEAD)` | `HEAD` | Review a whole PR against its base |
| `staged` | `HEAD` | synthetic commit of `git diff --cached` | Pre-commit check |
| `working` | `HEAD` | synthetic commit of working tree | Check uncommitted changes |
| `run --file <f> --parent <r> --child <r>` | explicit | explicit | Single-file, explicit revs |

`staged` and `working` create a detached **scratch worktree** at `HEAD`, apply your patch there, and commit it — your index and working tree are never mutated.

### Common options

| Flag | Default | Description |
|---|---|---|
| `--workflow {intent,dodgy,both}` | `both` | Which test-gen strategies to run |
| `--provider {auto,anthropic,ollama,openai-compat}` | `auto` | LLM backend. `auto` → anthropic if `ANTHROPIC_API_KEY`, else ollama |
| `--base-url <url>` | — | Required for `openai-compat`. Defaults for ollama: `http://localhost:11434/v1` |
| `--model <name>` | provider-aware | Default model. `claude-sonnet-4-6` for anthropic, `qwen2.5-coder:7b` otherwise |
| `--model-risks / --model-tests / --model-judge / --model-review` | — | Per-stage model overrides |
| `--stub` | off | Offline mode, no LLM calls |
| `--no-judge` | off | Skip LLM-as-judge scoring pass |
| `--no-review` | off | Skip the agentic reviewer (BugBot-style) |
| `--no-retry` | off | Skip feedback-driven retry rounds |
| `--max-retries <n>` | `2` | Retry rounds for uncaught risks |
| `--max-retry-risks <n>` | `8` | Per-round risk cap — bounds LLM spend |
| `--skip-validator` | off | Keep every reviewer finding (don't drop FPs) |
| `--with-callers` | off | Include caller source as usage context |
| `--max-callers <n>` | `5` | Cap on callers added per file |
| `--max-files <n>` | `20` | Cap on files per adapter group (by churn) |
| `--max-bytes <n>` | `200_000` | Cap on bundle prompt size |
| `--timeout <sec>` | `60` | Per-test execution timeout |
| `--llm-timeout <sec>` | `120` | HTTP read timeout per LLM call |
| `--max-tokens <n>` | model ceiling | Per-call output token cap |
| `--filename <name>` | timestamped | Base name for JSON/MD reports |
| `--verbose` | off | Write per-call LLM transcripts to `.jitcatch/logs/` |
| `--log-dir <path>` | — | Override LLM transcript directory |

---

## LLM providers

| Provider | Flag | Auth | Default model |
|---|---|---|---|
| Anthropic | `--provider anthropic` | `ANTHROPIC_API_KEY` env var | `claude-sonnet-4-6` |
| Ollama | `--provider ollama` | none (local) | `qwen2.5-coder:7b` |
| OpenAI-compatible | `--provider openai-compat --base-url <url>` | `OPENAI_API_KEY` if required by endpoint | `qwen2.5-coder:7b` |
| Stub (offline) | `--stub` | — | — |

The `openai-compat` provider works with any chat-completions endpoint:

- LM Studio, vLLM, LocalAI
- Groq, Together, Fireworks, OpenRouter
- Self-hosted gateways

**Why Ollama gets its own client:** JitCatch routes Ollama through the native `/api/chat` endpoint so `format: "json"` and `num_ctx` are honored. The generic `/v1` OpenAI-compat shim silently drops both, which breaks strict JSON-schema prompts on many local models.

### Per-stage models

Different stages have different cost/quality profiles. Use cheaper models for bulk output and reasoning-heavy models where it matters:

```bash
jitcatch pr . \
  --provider openai-compat --base-url https://api.together.xyz/v1 \
  --model-risks  meta-llama/Meta-Llama-3.1-70B-Instruct \
  --model-tests  meta-llama/Meta-Llama-3.1-8B-Instruct \
  --model-judge  meta-llama/Meta-Llama-3.1-70B-Instruct \
  --model-review meta-llama/Meta-Llama-3.1-70B-Instruct
```

---

## Workflows

### `intent` — risks-first

1. Ask the LLM to enumerate *risks* the diff introduces (null deref, off-by-one, contract change, etc.), each tagged with `[file:line]`.
2. Generate one test per risk.

**Best for:** structured diffs where intent can be reasoned about from the code.

### `dodgy` — mutation-mindset

1. Skip risk inference.
2. Directly ask for tests that would **detect the diff as if it were a bug** — the test should pin the parent's behavior and fail on any change.

**Best for:** refactors, small tweaks, cases where the intent is "preserve behavior".

`--workflow both` (the default) runs both and merges candidates.

### Agentic reviewer

Runs independently of test-gen. The reviewer reads the bundle and flags suspected bugs with a rationale. A second LLM validator pass filters obvious false positives (drops) or reduces confidence (downgrades). Findings with `validator_verdict ∈ {keep, downgrade}` are kept.

**Why a separate channel:** some bugs can't be exercised by a generated test. A mock swallows the error, an env var stubs out the broken path, or the buggy function is never called in any test. The reviewer surfaces those without pretending they come with executable evidence — findings appear in their own Markdown section and **never outrank test-backed weak catches** in the report.

### Retry loop

After the first round of tests runs, JitCatch diffs the risk list against the weak catches. For each **uncaught risk** it generates a follow-up test, including the prior test's failure output as feedback. Capped by `--max-retries` and `--max-retry-risks` to bound cost.

---

## Output

Two files per run, under `<repo>/.jitcatch/output/`:

- `jitcatch-<timestamp>.json` — machine-readable, sorted so weak catches come first (by `final_score` descending, non-weak appended).
- `jitcatch-<timestamp>.md` — human-readable summary with:
  - **Test-backed findings** (weak catches) — ranked by severity × confidence.
  - **Reviewer-only findings** — opinion-based, never outrank test-backed.
  - **Likely false positives** — low-signal entries collapsed to keep the top of the report clean.

Each candidate carries:

- `parent_result` / `child_result` — pass/fail status, stdout, stderr.
- `rule_flags` — deterministic assessor signals (`fp:reflection`, `tp:null_value`, …).
- `judge_tp_prob`, `judge_bucket`, `judge_rationale` — LLM-as-judge scores.
- `final_score` ∈ [-1, 1] — combined ranking score.
- `target_files` — files the test targets.

See [`docs/VALUE.md`](docs/VALUE.md) (when present) for the three-signal model and a false-positive playbook.

---

## Project layout

```
jitcatch/
├── cli.py            Argument parsing, subcommand dispatch, end-to-end orchestration
├── llm.py            Provider clients (Anthropic, Ollama, OpenAI-compat, Stub)
├── revs.py           Parent/child rev resolution + scratch worktrees
├── diff.py           Low-level git helpers
├── context.py        Bundle assembly, caller discovery, file selection
├── runner.py         WorktreeSandbox, evaluate_test
├── config.py         Dataclasses: CatchCandidate, GeneratedTest, ReviewFinding, TestResult
├── report.py         JSON + Markdown output
├── workflows/
│   ├── intent_aware.py   Risks-first test gen
│   ├── dodgy_diff.py     Mutation-mindset test gen
│   ├── reviewer.py       BugBot-style diff review + validator
│   └── retry.py          Feedback-driven retry rounds
├── assessor/
│   ├── rules.py          Deterministic fp:*/tp:* flagging + final_score
│   └── judge.py          LLM-as-judge wrapper
└── adapters/
    ├── base.py           Adapter ABC + subprocess helper
    ├── python.py         pytest
    └── javascript.py     node:test (ESM + CommonJS)
tests/
├── test_context.py        Bundle + caller discovery
├── test_revs.py           Rev resolvers (last/pr/staged/working)
├── test_smoke.py          End-to-end with StubClient
├── test_rules.py          Deterministic assessor rules
├── test_reviewer_retry.py Reviewer pipeline, retry loop, report sorting
├── test_llm_parse.py      JSON extraction, truncation recovery
├── test_pr_mode.py        pr/base-detection logic
├── test_provider_dispatch.py  Provider routing via httpx MockTransport
└── fixtures/              Fixture repos for language-adapter tests
```

---

## Supported languages

| Language | Extensions | Test runner | Adapter |
|---|---|---|---|
| Python | `.py` | `pytest` | [`jitcatch/adapters/python.py`](jitcatch/adapters/python.py) |
| JavaScript | `.js`, `.mjs`, `.cjs` | `node --test` (node:test) | [`jitcatch/adapters/javascript.py`](jitcatch/adapters/javascript.py) |

The JS adapter auto-detects ESM vs CommonJS from the target file extension and the project's `package.json "type"` field. `detect_runner` also recognizes Jest and Vitest for future extension.

### Adding a new language

Subclass `jitcatch.adapters.base.Adapter`, register it in `jitcatch/adapters/__init__.py`, and add a fixture under `tests/fixtures/`. See the existing adapters as templates. Contract:

```python
class Adapter(ABC):
    lang: str
    exts: tuple[str, ...] = ()

    def detect(self, source_rel: str) -> bool: ...
    def prompt_hints(self, module_rel: str, repo_root: Path | None = None) -> str: ...
    def write_test(self, repo_root: Path, test_name: str, code: str) -> TestArtifact: ...
    def run_test(self, repo_root: Path, artifact: TestArtifact, timeout: int) -> TestResult: ...
```

---

## Configuration tips

- **Large diffs are bounded by design.** Bundle is capped at `--max-bytes` (default 200 KB). Files beyond that are hunk-windowed (50 lines around each hunk). Override with `--max-bytes` if you have a generous context window.
- **Top-N by churn.** `select_files` keeps the most-changed files when a group exceeds `--max-files`. Noise from incidental edits doesn't crowd out the signal.
- **Prompt injection from untrusted repos.** JitCatch assumes you trust the code it reads. See [`SECURITY.md`](SECURITY.md) for the threat model.
- **Verbose logs.** `--verbose` writes every LLM request/response to `.jitcatch/logs/` untruncated. Invaluable when a run produces no weak catches — start by reading the risk list.
- **Truncation.** If the stderr summary reports `truncated (max_tokens): N > 0`, a response was cut off. Raise `--max-tokens`, shrink `--max-bytes`, or switch stages to a higher-ceiling model via `--model-tests`.

---

## Development

```bash
git clone https://github.com/kushankurdas/jitcatch
cd jitcatch
pip install -e '.[dev]'
pytest tests/
```

The test suite is fully offline:

- `StubClient` for LLM calls
- `httpx.MockTransport` for provider-dispatch tests
- Temp-dir git repos for sandbox tests

No API keys, no network. CI runs on Python 3.9–3.12 against Ubuntu with Node 20 installed for the JS adapter.

Run a single test:

```bash
pytest tests/test_reviewer_retry.py::ReportSortingTest -v
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the PR checklist, architecture orientation, and the code style rules.

---

## Security

JitCatch executes generated tests with the invoking user's privileges. The worktree sandbox is for **rev isolation**, not security containment. Run it against trusted repositories only, or inside a disposable container for CI.

Report vulnerabilities privately — see [`SECURITY.md`](SECURITY.md). Do not open public issues for security bugs.

---

## Citation

JitCatch is an independent open-source implementation of ideas from:

> Becker, M. et al. **Just-in-Time Catching Test Generation at Meta.** In *Companion Proceedings of the 34th ACM International Conference on the Foundations of Software Engineering* (FSE Companion '26), June 2026, Montreal, Canada. [arXiv:2601.22832](https://arxiv.org/abs/2601.22832).

```bibtex
@inproceedings{becker2026jitcatch,
  author    = {Becker, Matthew and others},
  title     = {Just-in-Time Catching Test Generation at Meta},
  booktitle = {Companion Proceedings of the 34th ACM International Conference on the Foundations of Software Engineering (FSE Companion '26)},
  year      = {2026},
  address   = {Montreal, Canada},
  url       = {https://arxiv.org/abs/2601.22832},
  eprint    = {2601.22832},
  archivePrefix = {arXiv}
}
```

This repository is not affiliated with or endorsed by Meta.

---

## License

MIT — see [`LICENSE`](LICENSE).

Copyright © 2026 Kushankur Das.
