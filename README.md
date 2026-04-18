# jitcatch

**Just-in-Time Catching test generation — an MVP.**

Given a git diff, `jitcatch` asks an LLM to generate unit tests that **pass on
the parent revision but fail on the child revision**. A test like that is a
*weak catch*: it reveals that the diff changed behavior. After post-processing
(LLM-as-judge + rule-based pattern matching), high-confidence weak catches are
surfaced as *strong catches* — candidate regressions to review before the diff
lands.

This is an MVP implementation of the approach described in:

> Becker et al., **Just-in-Time Catching Test Generation at Meta**,
> *FSE Companion ’26*. arXiv:[2601.22832](https://arxiv.org/abs/2601.22832)

The paper distinguishes *hardening* tests (pass at generation time, guard
against future regressions) from *catching* tests (fail at generation time,
catch bugs **before** the diff lands). This tool implements the catching
workflow for Python and JavaScript source files.

---

## Table of contents

1. [How it works](#how-it-works)
2. [Install](#install)
3. [Quick start](#quick-start)
4. [CLI reference](#cli-reference)
5. [Auto-rev modes](#auto-rev-modes)
6. [PR mode: multi-file context](#pr-mode-multi-file-context)
7. [Caller context](#caller-context)
8. [Stub mode](#stub-mode)
9. [Reports: JSON and Markdown](#reports-json-and-markdown)
10. [Debugging: per-call logs](#debugging-per-call-logs)
11. [Scoring](#scoring)
12. [Language support](#language-support)
13. [Architecture](#architecture)
14. [Running the tests](#running-the-tests)
15. [Limitations and non-goals](#limitations-and-non-goals)
16. [Troubleshooting](#troubleshooting)
17. [Project layout](#project-layout)

---

## How it works

```
┌──────────────────┐    ┌────────────┐    ┌──────────────────┐
│ rev pair +       │───▶│ LLM        │───▶│ generated tests  │
│ changed files    │    │ workflows  │    │ (pytest / node)  │
│ + caller context │    └────────────┘    └──────────────────┘
└──────────────────┘                                │
                                                    ▼
                        ┌──────────────────────────────────┐
                        │ WorktreeSandbox                  │
                        │   - parent worktree: run tests   │
                        │   - child  worktree: run tests   │
                        │   - node_modules symlinked in    │
                        └──────────────────────────────────┘
                                                    │
                            parent=pass & child=fail ⇒ weak catch
                                                    │
                                                    ▼
                        ┌──────────────────────────────────┐
                        │ Assessors                        │
                        │   - rule-based FP/TP patterns    │
                        │   - LLM-as-judge (tp_prob,bucket)│
                        └──────────────────────────────────┘
                                                    │
                                                    ▼
                          ranked JSON + text report
```

Two catch-generation workflows run per invocation:

- **Intent-aware** — the LLM first infers a list of *risks* the diff could
  introduce. Those risks are fed back in as context for test generation.
  Paper §3.1.
- **Dodgy-diff** — the diff is treated as if it were a mutation of the parent;
  the LLM generates tests for the parent that the mutated version should fail.
  Paper §3.3.

Both workflows run by default (`--workflow both`).

The parent/child pair can be selected **automatically** (PR vs base branch,
HEAD~1..HEAD, staged, or working-tree), or passed explicitly via the `run`
escape hatch. See [Auto-rev modes](#auto-rev-modes).

---

## Install

### Requirements

- Python 3.9+
- `git` on `$PATH`
- `pytest` (to execute generated Python tests)
- Node.js 18+ (to execute generated JavaScript tests; uses the built-in
  `node --test` runner)
- `anthropic` SDK (only for real LLM calls; stub mode has no dependencies)

### Install from source

```bash
git clone <this-repo> jitcatch
cd jitcatch
pip install -e .
```

Or run in place without installing:

```bash
cd jitcatch
PYTHONPATH=. python3 -m jitcatch.cli ...
```

---

## Quick start

The common case is a PR vs its base branch. `jitcatch pr` autodetects the
remote default branch, bundles every changed file the adapters recognize,
and ranks candidate regressions.

### Against a PR, with the Claude API

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pip install anthropic

cd /path/to/your/repo
jitcatch pr . --with-callers --out jc_pr.json
```

### Against an explicit base

```bash
jitcatch pr . --base origin/develop --out jc_vs_develop.json
```

### Pre-commit / uncommitted changes

```bash
jitcatch staged  .          # your currently-staged changes
jitcatch working .          # everything dirty in the worktree
```

### Just the last commit (fastest smoke)

```bash
jitcatch last .
```

### Explicit revs (escape hatch, single file)

```bash
jitcatch run /path/to/repo \
  --file src/calculator.py \
  --parent HEAD~1 --child HEAD
```

### Offline (stub mode, for demos / CI smoke tests)

Put a `.jitcatch_stub.json` at the repo root (see [Stub mode](#stub-mode)),
then pass `--stub` to any subcommand.

### Output

```
[jitcatch] pr: merge-base(origin/main, HEAD)..HEAD  (parent=b6987b6b child=988873fa)
Total generated: 4
Weak catches:    2

======================================================================
RANKED WEAK CATCHES (higher score = likelier true regression)
======================================================================

#1  score=+0.95  workflow=intent_aware
    test:    sentinelone_key_still_works
    files:   app/common/constants.js, app/services/manager.js
    judge:   tp_prob=+0.95  bucket=High
    why:     rename sentinelone→sentinelOne breaks callers that use the old key
    flags:   tp:null_value
    risks:   VMApps.sentinelone key was renamed, callers not updated
    child failure:
      | TypeError: Cannot read properties of undefined (reading 'appId')

JSON report: jc_pr.json
Markdown:    jc_pr.md
LLM calls: 4 | truncated (max_tokens): 0 | logs: /path/to/repo/.jitcatch_logs
```

Open `jc_pr.md` in any Markdown viewer for the human-readable, actionable
view: TL;DR, per-file diffs with line-number hunks, each weak catch with
its test code, judge rationale, and child-failure excerpt. The JSON is
the machine-readable twin.

---

## CLI reference

```
jitcatch <subcommand> <repo> [options]
```

### Subcommands

| Subcommand                   | parent                                   | child                            | Use case |
|------------------------------|------------------------------------------|----------------------------------|----------|
| `last <repo>`                | `HEAD~1`                                 | `HEAD`                           | Smoke-test the last commit. |
| `pr <repo> [--base <ref>]`   | `merge-base(<base or default>, HEAD)`    | `HEAD`                           | PR vs base branch. Default autodetected via `origin/HEAD`, falling back to `origin/main` / `origin/master` / `origin/develop`. |
| `staged <repo>`              | `HEAD`                                   | synthetic commit of staged diff  | Pre-commit check. User's index and worktree are **not** touched — the synthetic commit lives in a temporary worktree. |
| `working <repo>`             | `HEAD`                                   | synthetic commit of working tree | Uncommitted local changes. Same safety guarantee. |
| `run <repo> --file <path>`   | user-supplied `--parent`                 | user-supplied `--child`          | Explicit revs, single file. Escape hatch for weird histories. |

### Flags (shared by all subcommands)

| Flag                  | Default                 | Description |
|-----------------------|-------------------------|-------------|
| `--workflow`          | `both`                  | `intent`, `dodgy`, or `both`. |
| `--stub`              | off                     | Use `StubClient` instead of the Anthropic API. |
| `--model`             | `claude-sonnet-4-6`     | Claude model ID. |
| `--no-judge`          | off                     | Skip LLM-as-judge scoring. |
| `--timeout`           | `60`                    | Per-test seconds. |
| `--out`               | `jitcatch_report.json`  | JSON report path. A Markdown sibling is always written at `<out>.md`. |
| `--verbose`           | off                     | Print per-call LLM metadata (stop_reason, token counts, log path). |
| `--max-tokens`        | `8192`                  | Per-call LLM output cap. Raise (e.g. `16384`) if the end-of-run banner shows `truncated (max_tokens) > 0`. |
| `--log-dir`           | `<repo>/.jitcatch_logs` when `--verbose` is on | Directory for untruncated per-call transcripts (system prompt, user prompt, raw response, stop_reason, token counts). |

### Flags specific to `pr` / `staged` / `working` / `last`

| Flag              | Default | Description |
|-------------------|---------|-------------|
| `--with-callers`  | off     | Include grep-discovered caller files as *usage context* in the LLM prompt. |
| `--max-callers`   | `5`     | Per-file cap on callers added to the bundle. |
| `--max-files`     | `20`    | Cap on changed files per adapter group (top-N by churn). |
| `--max-bytes`     | `200000`| Hard cap on the assembled bundle string. |

### Flags specific to `pr`

| Flag       | Default | Description |
|------------|---------|-------------|
| `--base`   | autodetect | Base ref to diff against. Pass this when autodetection fails (e.g. no `origin` remote). |

### Flags specific to `run`

| Flag              | Default | Description |
|-------------------|---------|-------------|
| `--file`          | required | Source file under test, *repo-relative*. |
| `--parent`        | `HEAD~1` | Parent git rev. |
| `--child`         | `HEAD`   | Child git rev. |
| `--with-callers`  | off      | Include caller context in the single-file prompt. |
| `--max-callers`   | `5`      | Caller cap. |

Exit code `0` on success (including "no weak catches found"), `2` on argument
or repo errors.

---

## Auto-rev modes

All auto-rev subcommands resolve a `(parent, child)` pair and then feed the
**same** PR-bundle pipeline: group changed files by adapter, assemble one
prompt per group, generate tests, evaluate in both worktrees, judge, score.

### Default-branch detection (`pr` without `--base`)

1. `git symbolic-ref refs/remotes/origin/HEAD` — canonical answer if the
   remote was cloned normally.
2. Fallback to the first of `origin/main`, `origin/master`, `origin/develop`
   that resolves.
3. If none resolve, exit with an error telling you to pass `--base`.

### Safety for `staged` / `working`

The synthetic child commit is built in a **detached scratch worktree** at the
user's current `HEAD`. `git apply --index` replays the staged (or
working-tree) patch into that worktree's index, which is then committed. The
user's repo index and working tree are never mutated, and the scratch
worktree is removed on exit. This is verified by the test suite.

---

## PR mode: multi-file context

`pr` / `last` / `staged` / `working` bundle all changed files (that have a
registered adapter) into **one prompt per adapter group**. The LLM sees:

- every changed file's parent source (hunk-narrowed to ±50 lines when the
  file is larger than ~40 KB);
- every changed file's diff;
- adapter-specific framework hints;
- optional usage-context files from `--with-callers`.

The generated test may `import` / `require` any listed *changed* file by its
repo-relative path, so it can assert cross-file invariants (e.g. a rename in
`a.js` breaking a caller in `b.js`).

### Controls

- `--max-files` — caps the number of files per adapter group. When the cap
  is exceeded, files are ranked by diff churn (`git diff --numstat`).
- `--max-bytes` — hard cap on the final bundle string.
- Files > 40 KB are shown as hunk windows (`# ... lines X-Y ...`) instead of
  full source.

Python and JavaScript are **not** mixed in the same prompt; each adapter
group gets its own LLM call per workflow.

---

## Caller context

`--with-callers` runs a best-effort text grep to find files that
`require`/`import` each changed file, and adds them to the prompt as
*usage context* (labeled "do not test these directly"):

- **JavaScript**: matches `require('./x')`, `require("./x")`, `from './x'`,
  resolves `.js` / `.mjs` / `.cjs` / `index.*` extensions. Skips
  `node_modules`, `dist`, `build`, `.next`, `.git`.
- **Python**: matches `import <dotted>` / `from <dotted> import ...` against
  the target file's dotted module path. Skips `.venv`, `venv`,
  `__pycache__`, `build`, `dist`.

Callers are deduped against the changed set and capped by `--max-callers`
per file. Explicit non-goals: no TS path aliases, no webpack aliases, no
monorepo workspace resolution, no transitive (depth > 1) caller tracing.

---

## Stub mode

`--stub` swaps the Anthropic client for a deterministic `StubClient` that
reads canned responses from `<repo>/.jitcatch_stub.json`:

```json
{
  "risks":        ["operator flipped from + to -"],
  "bundle_risks": ["cross-file: key renamed without updating callers"],

  "intent_tests": [
    {
      "name": "add_basic",
      "code": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n",
      "rationale": "add returns sum"
    }
  ],
  "dodgy_tests": [],

  "bundle_intent_tests": [
    {
      "name": "cross_file_catch",
      "code": "const { test } = require('node:test');\nconst assert = require('node:assert/strict');\nconst b = require('./b');\ntest('x', () => { assert.strictEqual(b.run(), 5); });\n",
      "rationale": "import both files"
    }
  ],
  "bundle_dodgy_tests": [],

  "judge": {
    "tp_prob": 0.9,
    "bucket": "High",
    "rationale": "clear sign flip"
  }
}
```

Key selection:

- `run` uses `intent_tests` / `dodgy_tests` / `risks`.
- `pr` / `last` / `staged` / `working` use `bundle_intent_tests` /
  `bundle_dodgy_tests` / `bundle_risks`, falling back to the non-bundle keys
  when a bundle key is absent (so old stubs keep working).

Use it for:

- Hermetic demos and CI smoke tests (no API key, no spend).
- Validating the sandbox + scoring pipeline end-to-end.
- Regression tests over the tool itself.

If the stub file is missing, `StubClient` returns empty lists (the CLI will
report "no tests generated").

---

## Reports: JSON and Markdown

Every run writes two files side by side:

- `<out>.json` — machine-readable, full payload (all candidates, both weak
  catches and non-catches, parent/child stdout+stderr, judge raw output).
- `<out>.md` — human-readable, ranked. Drops into any Markdown viewer or
  GitHub comment. Contains: header metadata (command, repo, parent/child
  revs), TL;DR, per-file diffs *with line-number hunks* and `+/-`
  highlights, ranked weak catches each showing score/workflow/target
  files/judge/rationale/triggering diff/test code/child-failure excerpt,
  and a noise-check table of tests that did not catch.

The Markdown path is derived by swapping `.json` for `.md` on `--out` (or
appending `.md` if no extension). It is not opt-out — the MVP philosophy
is "always produce both."

### JSON report schema

```json
{
  "summary": {
    "total":        4,
    "weak_catches": 2
  },
  "candidates": [
    {
      "workflow":       "intent_aware",
      "test":           { "name": "...", "code": "...", "rationale": "..." },
      "risks":          ["..."],
      "parent_result":  { "status": "pass", "exit_code": 0, "stdout": "...", "stderr": "" },
      "child_result":   { "status": "fail", "exit_code": 1, "stdout": "...", "stderr": "" },
      "judge_tp_prob":  0.9,
      "judge_bucket":   "High",
      "judge_rationale":"...",
      "judge_raw":      "{\"tp_prob\":0.9,...}",
      "rule_flags":     ["tp:value_mismatch"],
      "final_score":    1.0,
      "target_files":   ["app/common/constants.js", "app/services/manager.js"],
      "is_weak_catch":  true
    }
  ]
}
```

- `status` is one of `pass | fail | error`.
- `is_weak_catch` is `true` iff the test passed on the parent and failed on
  the child.
- `target_files` lists the changed files the test was generated for. In
  `run` mode this is just the single file you passed; in bundle mode it is
  the adapter group's selection.
- `judge_raw` is the raw LLM response for the judge call, preserved for
  debugging parse failures. Empty when `--no-judge` is set or the candidate
  isn't a weak catch.

---

## Debugging: per-call logs

When `--verbose` is on (or `--log-dir` is passed explicitly), every LLM
call is written to its own file under `<log-dir>/` with **no truncation**.

Filename pattern: `<timestamp>_<seq>_<label>.log`, e.g.
`20260418-143052_003_tests.bundle.dodgy.log`.

Each file contains:

```
# label: tests.bundle.dodgy
# seq: 3
# model: claude-sonnet-4-6
# max_tokens_cap: 8192
# stop_reason: end_turn
# input_tokens: 4127
# output_tokens: 3208

===== SYSTEM =====
...
===== USER =====
...
===== RESPONSE =====
...
```

The end-of-run banner prints `LLM calls: N | truncated (max_tokens): M`
so you can spot response truncation at a glance. If `M > 0`, raise
`--max-tokens` (e.g. `--max-tokens 16384`) and re-run.

### Parser hardening

All JSON parsers in `llm.py` tolerate three real-world failure modes:

1. **Prose preamble.** `_strip_code_fence` is non-anchored, so responses
   like `"Looking at the diff... ```json {...} ```"` still parse.
2. **Truncation mid-body.** `_recover_truncated_tests_json` walks the
   `"tests": [...]` array forward and salvages every completed entry
   when the response cut off before the closing `]}`.
3. **Unparseable first attempt.** Risks, tests, and judge calls all
   retry once with `Return ONLY the raw JSON object...` appended before
   giving up. Retry responses are logged separately.

---

## Scoring

`final_score` is in `[-1.0, +1.0]`. Higher = more likely to be a true
regression.

```
final_score = clamp(
    judge_tp_prob
    - 0.3 * count(fp_flags)
    + 0.1 * count(tp_flags)
    , -1.0, +1.0
)
```

### Rule flags

**False-positive signals** (inspired by Table 2 of the paper):

| Flag                      | Trigger |
|---------------------------|---------|
| `fp:reflection`           | Test code uses `getattr` / `hasattr` / `inspect` / `Reflect.` / prototype inspection. |
| `fp:mock_usage`           | Test code uses `unittest.mock` / `MagicMock` / `jest.fn` / `sinon`. |
| `fp:flakiness`            | Test code references `time.sleep`, `random.*`, `Math.random`, network I/O. |
| `fp:undefined_variable`   | `NameError` / `ReferenceError` in the child's failure output. |
| `fp:broken_test_runner`   | Import / module-not-found / collection error in failure output. |
| `fp:parent_unstable`      | The test did not pass on the parent — signal is unreliable. |

**True-positive signals** (inspired by Table 3):

| Flag                 | Trigger |
|----------------------|---------|
| `tp:null_value`      | `NoneType` / "is not a function" / "cannot read property" in failure. |
| `tp:value_mismatch`  | `AssertionError` / `assert.strictEqual` / `Expected:` / `Received:` in failure. |

### LLM-as-judge

If `--no-judge` is not set and the candidate is a weak catch, the same LLM is
asked to classify the failure:

```json
{ "tp_prob": 0.9, "bucket": "High", "rationale": "..." }
```

`tp_prob` is in `[-1, 1]` (`-1` = surely a false positive, `+1` = surely a
real bug), matching the paper's Normalized Token Probability score. `bucket`
is `High | Medium | Low`, matching the Ensemble Categorical Likelihood
score. This is a single-model judge; the paper uses an ensemble of three.

The judge parser shares the same hardening as the tests parser — see
[Debugging: per-call logs](#debugging-per-call-logs). The raw LLM response
is preserved on `candidate.judge_raw` either way.

---

## Language support

| Language   | Source extensions       | Test runner    | Notes |
|------------|-------------------------|----------------|-------|
| Python     | `.py`                   | `pytest`       | Tests are emitted as `_jc_test_<name>.py` at the repo root. Plain `assert` statements. |
| JavaScript | `.js`, `.mjs`, `.cjs`   | `node --test`  | Tests are emitted as `_jc_test_<name>.test.mjs` for ESM projects (`"type": "module"` in `package.json`) or `.test.cjs` for CommonJS projects. ESM vs CJS is picked per-repo from `package.json`; prompt hints switch between `import` and `require()` syntax accordingly. Uses `node:test` + `node:assert/strict`. |

When `<repo>/node_modules` exists, the runner symlinks it into each worktree
so generated tests can resolve installed packages without a reinstall.

Jest / Vitest runners are **not** wired up in the MVP — `node --test` was
chosen because it has zero install cost.

---

## Architecture

```
jitcatch/
├── cli.py             # argparse entry, orchestrator for all subcommands
├── config.py          # dataclasses: TestResult, GeneratedTest, CatchCandidate
├── diff.py            # thin `git` wrappers (rev-parse, diff, show, changed_files)
├── revs.py            # auto-rev resolvers: last / pr / staged / working
├── context.py         # caller discovery + bundle builder + hunk narrowing
├── runner.py          # WorktreeSandbox (two detached worktrees, auto-cleanup, node_modules symlink)
├── llm.py             # LLMClient (Anthropic + Stub); prompts, per-call logging, hardened parsers with retry + truncation recovery
├── report.py          # JSON writer + Markdown writer + text renderer
├── adapters/
│   ├── base.py        # Adapter ABC + subprocess helper
│   ├── python.py      # PythonAdapter (pytest)
│   └── javascript.py  # JavaScriptAdapter (node --test, ESM + CJS)
├── workflows/
│   ├── intent_aware.py  # risks → tests (single + bundle)
│   └── dodgy_diff.py    # diff-as-mutation → tests (single + bundle)
└── assessor/
    ├── rules.py       # regex-based FP/TP pattern flags + final_score
    └── judge.py       # LLM-as-judge invocation
```

### Key abstractions

- **`Adapter`** — language plug-in. Implements `prompt_hints`, `write_test`,
  `run_test`. Add a new language by writing another `Adapter` and registering
  it in `adapters/__init__.py`.
- **`LLMClient`** — test generator / judge. Two implementations:
  `AnthropicClient` (real API) and `StubClient` (reads JSON fixture). Bundle
  methods (`infer_risks_bundle`, `generate_tests_bundle`) let subclasses
  specialize for multi-file prompts.
- **`RevPair`** — context-manager returned by `revs.resolve(...)`. Cleans up
  any scratch worktree on exit.
- **`WorktreeSandbox`** — context manager that creates two detached
  `git worktree` directories (parent + child), symlinks `node_modules`,
  guarantees cleanup.

### Orchestration flow

#### `run` (single file, explicit revs)

1. Resolve `parent_rev` / `child_rev`, choose adapter from file extension.
2. Read parent source and diff via `git show` / `git diff`.
3. Optionally prepend caller context.
4. Ask the LLM for risks + tests (intent-aware) and/or tests alone (dodgy).
5. Open `WorktreeSandbox`, run each test in both worktrees.
6. Apply rule flags, run judge on weak catches, compute `final_score`.
7. Write JSON + Markdown + text report.

#### `pr` / `last` / `staged` / `working` (bundle)

1. `revs.resolve(...)` → `(parent, child)` pair. For `staged` / `working`,
   a scratch worktree is created and cleaned up on exit.
2. `git diff --name-only parent..child` → changed files; group by adapter.
3. Per group: read each file at parent, compute diffs, optionally discover
   callers, assemble a single bundle string (respecting `--max-files` /
   `--max-bytes`, hunk-narrowing large files).
4. Call `generate_tests_bundle` once per (group, workflow).
5. Same evaluate → flag → judge → score → report pipeline as above.

---

## Running the tests

```bash
cd jitcatch
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

32 tests ship:

- `test_smoke.py` — planted-bug fixtures for Python + JavaScript, end-to-end
  through the `run` subcommand.
- `test_revs.py` — verifies each rev resolver, including that `staged` and
  `working` leave the user's index and worktree untouched.
- `test_context.py` — caller discovery for JS + Python, bundle builder,
  hunk-window extraction, churn parsing, path normalization.
- `test_pr_mode.py` — end-to-end cross-file catch via `last` subcommand: a
  rename in `a.js` breaks a caller in `b.js`; bundle mode generates a test
  that imports both and catches it.
- `test_llm_parse.py` — parser hardening: strict JSON, fenced JSON with
  prose preamble, prose-without-fence, truncated responses (salvage of
  completed `tests[]` entries), code-fence without a closing fence, and
  judge parser with prose preamble.

JavaScript-dependent tests are skipped automatically if `node` is not on
`$PATH`.

---

## Limitations and non-goals

The MVP intentionally omits a number of things that the Meta deployment has:

- **Single LLM judge**, not an ensemble of three (`Llama3.3-70B`, `Gemini 3`,
  `Claude Sonnet 4`).
- **No DRS / risk-weighted diff targeting** — every input diff is processed.
- **No coincidental hardening-test harvest.**
- **No full import-graph resolution.** Caller discovery is a best-effort
  text grep: no TS path aliases, no webpack aliases, no monorepo workspace
  resolution, no transitive (depth > 1) caller tracing.
- **Python and JavaScript are not mixed** in a single PR-mode prompt; each
  adapter group is its own call.
- **No Jest / Vitest / Mocha support** — JavaScript runs via `node --test`
  only.
- **No Kotlin / Java / TypeScript / Go** — Python + JavaScript only.
- **No sandboxing beyond `git worktree`** — generated tests execute with the
  permissions of the current user. Do not point this at untrusted diffs.
- **No self-repair for generated test *code* syntax errors.** Risks,
  tests, and judge calls each retry once when the LLM's *JSON envelope*
  is unparseable, but if the test body itself has a syntax error we
  report the failure and move on.
- **No streaming** — generation is a single blocking call per workflow
  (plus at most one retry on parse failure).

---

## Troubleshooting

**`error: <repo> is not a git repo`** — the path must point at the directory
containing `.git/`, not a subdirectory.

**`could not detect default branch; pass --base explicitly`** — `pr` mode
couldn't resolve `origin/HEAD`, `origin/main`, `origin/master`, or
`origin/develop`. Pass the base ref yourself: `jitcatch pr . --base origin/trunk`.

**`git worktree add ... failed: fatal: '<path>' already exists`** — a previous
run was interrupted. Run `git worktree prune` in the target repo, or delete
the stale path.

**`ANTHROPIC_API_KEY not set`** — export the key, or pass `--stub` to run
without a key.

**`no tests generated`** — the LLM returned no parseable tests. Re-run
with `--verbose` to write full per-call transcripts to
`<repo>/.jitcatch_logs/`. Check the end-of-run banner: if
`truncated (max_tokens) > 0`, raise `--max-tokens` (try `16384`). If
`stop_reason=end_turn` in the logs but output is still empty, the model
likely hit a safety refusal. In stub mode it means your
`.jitcatch_stub.json` is missing or has empty `*_tests` arrays.

**`no staged changes` / `no working-tree changes`** — the `staged` /
`working` subcommands need something to diff. Check `git diff --cached` /
`git diff`.

**`no changed files between <rev>..<rev>`** — the rev range is empty or
every changed file lacks a registered adapter (`.py`, `.js`, `.mjs`,
`.cjs`).

**Generated Python test fails with `ModuleNotFoundError`** — imports in the
generated test must resolve from the repo root. The MVP writes tests at the
repo root and runs `pytest` from there; if your package lives under `src/`
without an `__init__.py`, the LLM may need a hint. Either move the file, add
`__init__.py`, or adjust the prompt hints.

**Generated JS test fails with `ERR_MODULE_NOT_FOUND` or "Cannot use import
statement outside a module"** — the adapter picks ESM vs CJS from the
repo's `package.json`. If detection is wrong, check that `"type": "module"`
is set for ESM projects. Mixed repos (some `.mjs` + some `.cjs`) work
because the per-file suffix overrides.

---

## Project layout

```
jitcatch/
├── pyproject.toml
├── README.md
├── .gitignore
├── jitcatch/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── context.py
│   ├── diff.py
│   ├── llm.py
│   ├── report.py
│   ├── revs.py
│   ├── runner.py
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── javascript.py
│   │   └── python.py
│   ├── assessor/
│   │   ├── __init__.py
│   │   ├── judge.py
│   │   └── rules.py
│   └── workflows/
│       ├── __init__.py
│       ├── dodgy_diff.py
│       └── intent_aware.py
└── tests/
    ├── __init__.py
    ├── fixtures/
    ├── test_context.py
    ├── test_llm_parse.py
    ├── test_pr_mode.py
    ├── test_revs.py
    └── test_smoke.py
```

---

## License

No license declared. Treat as source-available research code.

## Citation

If you reference the underlying approach, please cite the paper:

```
@inproceedings{becker2026jitcatch,
  title     = {Just-in-Time Catching Test Generation at Meta},
  author    = {Becker, Matthew and Chen, Yifei and Cochran, Nicholas
               and Ghasemi, Pouyan and Gulati, Abhishek and Harman, Mark
               and Haluza, Zachary and Honarkhah, Mehrdad and Robert, Herve
               and Liu, Jiacheng and Liu, Weini and Thummala, Sreeja
               and Yang, Xiaoning and Xin, Rui and Zeng, Sophie},
  booktitle = {Companion Proceedings of the 34th ACM International
               Conference on the Foundations of Software Engineering
               (FSE Companion '26)},
  year      = {2026}
}
```
