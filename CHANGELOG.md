# Changelog

All notable changes to JitCatch are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `jitcatch explain <repo> <id-prefix>` subcommand. Reads the latest JSON report under `.jitcatch/output/` (override with `--report`), prints the full detail block for a candidate, and drops into a colored interactive LLM chat REPL seeded with that candidate's context. `--no-chat` and non-tty stdin skip the REPL and emit the plain detail block. Documented in [`docs/15-explain-candidate.md`](docs/15-explain-candidate.md).
- HTML report writer (`report.write_html`). Self-contained single-file output with inlined CSS, color-coded diffs, severity badges, and a collapsed false-positive section.
- `--format {html,md,all}` flag selects human-readable formats to emit alongside the always-on JSON. Example: `--format html,md`.
- Runtime flake detection via `--flake-check N` (default 3). Failing child tests are re-run N times; any flip demotes the candidate with `fp:flake_runtime`. Set `0` to disable.
- Parallel parent/child worktree evaluation in `WorktreeSandbox`. Both revs execute their test concurrently per candidate.
- Risk-inference cache at `<repo>/.jitcatch/cache/risks/` keyed on `(bundle + lang + model)` with a 7-day TTL. `--no-cache` bypasses it for a run; `--clear-cache` purges before running.
- Per-run token + cost accounting (`UsageStats`). Rendered in every report format and on stderr, broken down by stage (risks / tests / judge / review). Stub runs omit it.
- Markdown report splits low-signal entries into a collapsed **Likely false positives** section so high-signal bugs surface first.
- `ReportSortingTest` covering JSON sort order, test-backed vs review-only ranking, and FP-section placement.

### Changed
- `write_json` sorts candidates so weak catches come first (by `final_score` descending), non-weak appended.
- `_group_sort_key` now uses `has_test` as the primary key. Test-backed groups always outrank review-only findings regardless of severity. Executable evidence beats LLM opinion.

## [0.1.0]. Initial release

### Added
- Five CLI subcommands: `pr`, `last`, `staged`, `working`, `run`.
- Dual-worktree runner (`jitcatch/runner.py`) that evaluates generated tests on parent and child revs in isolated git worktrees.
- Two test-generation workflows: `intent` (risks-first) and `dodgy` (mutation-mindset).
- Agentic reviewer channel (`jitcatch/workflows/reviewer.py`) for opinion-based findings when test-gen can't exercise the regression.
- Feedback-driven retry loop targeting gaps left by the first test round.
- Deterministic rule-based assessor (`jitcatch/assessor/rules.py`) with `fp:*` / `tp:*` flags.
- LLM-as-judge assessor producing `tp_prob`, `bucket`, and `rationale` per candidate.
- Provider support: Anthropic, Ollama, any OpenAI-compatible endpoint, and an offline `StubClient` for tests.
- Per-stage model overrides (`--model-risks`, `--model-tests`, `--model-judge`, `--model-review`).
- Language adapters for Python (`pytest`) and JavaScript (`node:test`).
- JSON and Markdown reports under `.jitcatch/output/`.

[Unreleased]: https://github.com/kushankurdas/jitcatch/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kushankurdas/jitcatch/releases/tag/v0.1.0
