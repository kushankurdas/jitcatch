# Contributing to JitCatch

Thanks for your interest in JitCatch. This doc covers how to set up a dev environment, how to run the tests, and what a good PR looks like.

If you're reporting a bug or suggesting a feature, use the [issue templates](.github/ISSUE_TEMPLATE) rather than sending a PR blind. It's easier to agree on the shape of a change before code is written.

---

## Development setup

Requirements: Python ≥ 3.9, `git`, and (for running the test suite end-to-end) `node` ≥ 18.

```bash
git clone https://github.com/kushankurdas/jitcatch
cd jitcatch
pip install -e '.[dev]'
```

That installs the `jitcatch` console script in editable mode plus `pytest`.

### Running the test suite

```bash
pytest tests/
```

The suite is fully offline. It uses `StubClient` for LLM calls, `httpx.MockTransport` for provider-dispatch tests, and temp-dir git repos for sandbox tests. No API keys, no network.

Run a single test:

```bash
pytest tests/test_reviewer_retry.py::ReportSortingTest -v
```

### Trying changes against a real repo

```bash
cd /path/to/any/repo/with/changes
jitcatch pr . --stub                 # offline demo
jitcatch pr . --provider ollama      # free, local model
```

See [`docs/`](docs/) for per-use-case guides and the [Output](README.md#output) section of the README for how findings are grouped and ranked.

---

## Pull request checklist

Before you open a PR:

- [ ] Tests pass (`pytest tests/`).
- [ ] New behavior has a test. Bug fixes include a regression test.
- [ ] No new runtime dependencies unless clearly justified in the PR description.
- [ ] No breaking CLI/flag changes without a migration note in `CHANGELOG.md`.
- [ ] Docs touched if user-visible behavior changed (`README.md`, `docs/`).

The PR description should answer: **what changed, and why**. The "what" can be short. Readers have the diff. Focus the description on motivation, tradeoffs, and anything that isn't obvious from the code.

---

## What makes a good contribution

**Small and focused** beats large and sweeping. If your change is big, split it.

**Doesn't expand scope.** A bug fix shouldn't reformat adjacent code. A feature shouldn't refactor unrelated modules.

**Preserves the core invariant.** JitCatch's value is executable evidence (test passes on parent, fails on child). Features should strengthen that signal, not dilute it. LLM-opinion-only features belong in the reviewer channel, not the test-gen path.

**Keeps the pipeline deterministic where possible.** Rule-based filters (`jitcatch/assessor/rules.py`) are preferred over LLM-dependent ones for signals that can be expressed as patterns.

---

## Architecture orientation

See the architecture diagram and module list in [`README.md`](README.md). The short version:

- `jitcatch/cli.py`. Argument parsing, subcommand dispatch.
- `jitcatch/llm.py`. Provider clients.
- `jitcatch/revs.py`. Parent/child ref resolution.
- `jitcatch/context.py`. Diff bundle assembly.
- `jitcatch/workflows/`. Test-gen strategies, reviewer, retry loop.
- `jitcatch/runner.py` - `WorktreeSandbox`, test evaluation.
- `jitcatch/assessor/`. Rules + LLM judge.
- `jitcatch/adapters/`. Per-language test write+run plug-ins.
- `jitcatch/report.py`. JSON and Markdown output.

### Adding a new language

Subclass `jitcatch.adapters.base.Adapter`, register it in `jitcatch/adapters/__init__.py`, and add a fixture in `tests/fixtures/`. See `jitcatch/adapters/python.py` and `jitcatch/adapters/javascript.py` as references.

### Adding a new provider

Subclass `LLMClient` in `jitcatch/llm.py` and wire it into the `_make_llm` dispatch in `jitcatch/cli.py`. The OpenAI-compatible client already covers most hosted endpoints. Prefer `--provider openai-compat --base-url ...` over a new client when the API is compatible.

---

## Code style

- Keep comments focused on **why**, not **what**. Delete comments that only restate the code.
- Prefer standard library over new dependencies.
- No feature flags or backwards-compat shims for pre-1.0 changes. We can break things cleanly.
- Keep functions short. If a function is doing two things, split it.

---

## Reporting security issues

Don't open a public issue for a security vulnerability. See [`SECURITY.md`](SECURITY.md).

---

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating, you agree to abide by its terms.
