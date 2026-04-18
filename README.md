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
5. [Stub mode](#stub-mode)
6. [JSON report schema](#json-report-schema)
7. [Scoring](#scoring)
8. [Language support](#language-support)
9. [Architecture](#architecture)
10. [Running the smoke tests](#running-the-smoke-tests)
11. [Limitations and non-goals](#limitations-and-non-goals)
12. [Troubleshooting](#troubleshooting)
13. [Project layout](#project-layout)

---

## How it works

```
┌────────────┐    ┌────────────┐    ┌──────────────────┐
│ git diff   │───▶│ LLM        │───▶│ generated tests  │
│ + parent   │    │ workflows  │    │ (pytest / node)  │
│   source   │    └────────────┘    └──────────────────┘
└────────────┘                                │
                                              ▼
                      ┌──────────────────────────────────┐
                      │ WorktreeSandbox                  │
                      │   - parent worktree: run tests   │
                      │   - child  worktree: run tests   │
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

Two catch-generation workflows are implemented:

- **Intent-aware** — the LLM first infers a list of *risks* the diff could
  introduce. Those risks are fed back in as context for test generation. This
  maps to §3.1 of the paper.
- **Dodgy-diff** — the diff is treated as if it were a mutation of the parent;
  the LLM generates tests for the parent that the mutated version should fail.
  This maps to §3.3.

Both workflows run by default (`--workflow both`).

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

Point `jitcatch` at any git repository, pass the source file you want to
generate catching tests for, and (optionally) specify the parent and child
revisions to diff.

### With the Claude API

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pip install anthropic

jitcatch run /path/to/your/repo \
  --file src/calculator.py \
  --parent HEAD~1 \
  --child  HEAD
```

### Offline (stub mode, for demos / CI smoke tests)

Put a `.jitcatch_stub.json` at the repo root (see [Stub mode](#stub-mode)),
then:

```bash
jitcatch run /path/to/your/repo --file src/calculator.py --stub
```

### Output

```
Total generated: 2
Weak catches:    1

======================================================================
RANKED WEAK CATCHES (higher score = likelier true regression)
======================================================================

#1  score=+1.00  workflow=intent_aware
    test:    add_basic
    judge:   tp_prob=+0.90  bucket=High
    why:     sign flip in add() is a clear regression
    flags:   tp:value_mismatch
    risks:   operator flipped from + to -
    child failure:
      | >   assert add(2, 3) == 5
      | E   assert -1 == 5

JSON report: jitcatch_report.json
```

---

## CLI reference

```
jitcatch run <repo> --file <path> [options]
```

| Flag                  | Default                 | Description |
|-----------------------|-------------------------|-------------|
| `<repo>`              | required                | Path to a git repo. |
| `--file`              | required                | Source file under test, *repo-relative*. |
| `--parent`            | `HEAD~1`                | Parent git rev. |
| `--child`             | `HEAD`                  | Child git rev. |
| `--workflow`          | `both`                  | `intent`, `dodgy`, or `both`. |
| `--stub`              | off                     | Use `StubClient` instead of the Anthropic API. |
| `--model`             | `claude-sonnet-4-6`     | Claude model ID. |
| `--no-judge`          | off                     | Skip LLM-as-judge scoring. |
| `--timeout`           | `60`                    | Per-test seconds. |
| `--out`               | `jitcatch_report.json`  | JSON report path. |

Exit code `0` on success (including “no weak catches found”), `2` on argument
or repo errors.

---

## Stub mode

`--stub` swaps the Anthropic client for a deterministic `StubClient` that
reads canned responses from `<repo>/.jitcatch_stub.json`:

```json
{
  "risks": ["operator flipped from + to -"],
  "intent_tests": [
    {
      "name": "add_basic",
      "code": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n",
      "rationale": "add returns sum"
    }
  ],
  "dodgy_tests": [],
  "judge": {
    "tp_prob": 0.9,
    "bucket": "High",
    "rationale": "clear sign flip"
  }
}
```

Use it for:

- Hermetic demos and CI smoke tests (no API key, no spend).
- Validating the sandbox + scoring pipeline end-to-end.
- Regression tests over the tool itself.

If the stub file is missing, `StubClient` returns empty lists (the CLI will
report “no tests generated”).

---

## JSON report schema

`jitcatch_report.json`:

```json
{
  "summary": {
    "total":        2,
    "weak_catches": 1
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
      "rule_flags":     ["tp:value_mismatch"],
      "final_score":    1.0,
      "is_weak_catch":  true
    }
  ]
}
```

`status` is one of `pass | fail | error`. `is_weak_catch` is `true` iff the
test passed on the parent and failed on the child.

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
| `fp:undefined_variable`   | `NameError` / `ReferenceError` in the child’s failure output. |
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
real bug), matching the paper’s Normalized Token Probability score. `bucket`
is `High | Medium | Low`, matching the Ensemble Categorical Likelihood score.
This is a single-model judge; the paper uses an ensemble of three.

---

## Language support

| Language   | Source extensions       | Runner         | Notes |
|------------|-------------------------|----------------|-------|
| Python     | `.py`                   | `pytest`       | Tests are emitted as `_jc_test_<name>.py` at the repo root. Expects plain `assert` statements. |
| JavaScript | `.js`, `.mjs`, `.cjs`   | `node --test`  | Tests are emitted as `_jc_test_<name>.test.mjs` (ES modules) at the repo root, using `node:test` + `node:assert/strict`. |

Jest / Vitest runners are **not** wired up in the MVP — `node --test` was
chosen because it has zero install cost.

---

## Architecture

```
jitcatch/
├── cli.py            # argparse entry, orchestrator
├── config.py         # dataclasses: TestResult, GeneratedTest, CatchCandidate
├── diff.py           # thin `git` wrappers (rev-parse, diff, show)
├── runner.py         # WorktreeSandbox (two detached worktrees, auto-cleanup)
├── llm.py            # LLMClient (Anthropic + Stub); prompts + JSON parsers
├── report.py         # JSON writer + text renderer
├── adapters/
│   ├── base.py       # Adapter ABC + subprocess helper
│   ├── python.py     # PythonAdapter (pytest)
│   └── javascript.py # JavaScriptAdapter (node --test)
├── workflows/
│   ├── intent_aware.py  # risks → tests
│   └── dodgy_diff.py    # diff-as-mutation → tests
└── assessor/
    ├── rules.py      # regex-based FP/TP pattern flags + final_score
    └── judge.py      # LLM-as-judge invocation
```

### Key abstractions

- **`Adapter`** — language plug-in. Implements `prompt_hints`, `write_test`,
  `run_test`. Add a new language by writing another `Adapter` and registering
  it in `adapters/__init__.py`.
- **`LLMClient`** — test generator / judge. Two implementations:
  `AnthropicClient` (real API) and `StubClient` (reads JSON fixture). Swap for
  a new backend by implementing `infer_risks`, `generate_tests`, `judge`.
- **`WorktreeSandbox`** — context manager that creates two detached
  `git worktree` directories (parent + child), guarantees cleanup on exit.

### Orchestration flow (`cli.cmd_run`)

1. Resolve `parent_rev` / `child_rev`, choose adapter from file extension.
2. Read parent source and diff via `git show` / `git diff`.
3. Ask the LLM for risks + tests (intent-aware) and/or tests alone (dodgy).
4. Open `WorktreeSandbox`, write each generated test into both worktrees,
   run it in each, collect `parent_result` + `child_result`.
5. Apply rule flags. If weak catch and `--no-judge` is off, call the judge.
6. Compute `final_score`, write JSON, render text report.

---

## Running the smoke tests

Two fixture-based end-to-end tests ship in `tests/test_smoke.py`. They build
throwaway git repos (one Python, one JavaScript), plant a known bug in the
child commit, and assert `jitcatch` reports a weak catch:

```bash
cd jitcatch
PYTHONPATH=. python3 -m unittest tests.test_smoke -v
```

Expected:

```
test_javascript_fixture_detects_bug (tests.test_smoke.SmokeTest) ... ok
test_python_fixture_detects_bug   (tests.test_smoke.SmokeTest) ... ok
----------------------------------------------------------------------
Ran 2 tests in ~2s
OK
```

The JavaScript test is skipped automatically if `node` is not on `$PATH`.

### Manual end-to-end demo

```bash
# Build a toy Python repo with a planted bug
mkdir /tmp/demo && cd /tmp/demo
git init -q -b main
git config user.email t@e.com && git config user.name t
cat > calc.py <<'EOF'
def mul(a, b): return a * b
EOF
git add -A && git commit -qm parent
cat > calc.py <<'EOF'
def mul(a, b): return a + b
EOF
git add -A && git commit -qm "child (bug)"

cat > .jitcatch_stub.json <<'EOF'
{
  "risks": ["mul replaced with add"],
  "intent_tests": [
    {"name":"mul_basic","code":"from calc import mul\ndef test_m(): assert mul(3,4)==12\n"}
  ],
  "dodgy_tests": [],
  "judge": {"tp_prob":0.95,"bucket":"High","rationale":"op swap"}
}
EOF

PYTHONPATH=/path/to/jitcatch python3 -m jitcatch.cli \
  run /tmp/demo --file calc.py --stub
```

---

## Limitations and non-goals

The MVP intentionally omits a number of things that the Meta deployment has:

- **Single LLM judge**, not an ensemble of three (`Llama3.3-70B`, `Gemini 3`,
  `Claude Sonnet 4`).
- **No DRS / risk-weighted diff targeting** — every input diff is processed.
- **No coincidental hardening-test harvest.**
- **No cross-file context** — only the source file you pass is shown to the
  LLM. Imports and collaborators are invisible.
- **No Jest / Vitest / Mocha support** — JavaScript runs via `node --test`
  only.
- **No Kotlin / Java / TypeScript / Go** — Python + JavaScript only.
- **No sandboxing beyond `git worktree`** — generated tests execute with the
  permissions of the current user. Do not point this at untrusted diffs.
- **No retry or self-repair** — if the LLM emits invalid JSON or a test with a
  syntax error, that candidate is dropped.
- **No streaming** — generation is a single blocking call per workflow.

---

## Troubleshooting

**`error: <repo> is not a git repo`** — the path must point at the directory
containing `.git/`, not a subdirectory.

**`git worktree add ... failed: fatal: '<path>' already exists`** — a previous
run was interrupted. `git worktree prune` in the target repo, or delete the
stale path.

**`ANTHROPIC_API_KEY not set`** — export the key, or pass `--stub` to run
without a key.

**`no tests generated`** — in real mode, the LLM returned unparseable JSON.
Re-run; lower `--timeout` won’t help. In stub mode, your
`.jitcatch_stub.json` is missing or has empty `intent_tests` / `dodgy_tests`.

**`no diff for <file> between <rev>..<rev>`** — the file did not change
between the revisions you passed. Check `git log -- <file>`.

**Generated Python test fails with `ModuleNotFoundError`** — imports in the
generated test must resolve from the repo root. The MVP writes tests at the
repo root and runs `pytest` from there; if your package lives under `src/`
without an `__init__.py`, the LLM may need a hint. Either move the file, add
`__init__.py`, or adjust the prompt hints.

**Generated JS test fails with `ERR_MODULE_NOT_FOUND`** — imports in the
generated test use `./<relative>.mjs` paths. If your source is `.js` but the
repo’s `package.json` does not declare `"type": "module"`, Node will treat
`.js` as CommonJS and the import will fail. Workarounds: rename to `.mjs`,
add `"type": "module"` to `package.json`, or pass a `.cjs` file.

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
│   ├── diff.py
│   ├── llm.py
│   ├── report.py
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
