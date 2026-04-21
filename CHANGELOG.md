# Changelog

All notable changes to JitCatch are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `docs/VALUE.md` — practical guide to interpreting JitCatch output (three-signal model, false-positive playbook, value workflows).
- Markdown report splits low-signal entries into a collapsed **Likely false positives** section so high-signal bugs surface first.
- `ReportSortingTest` covering JSON sort order, test-backed vs review-only ranking, and FP-section placement.

### Changed
- `write_json` sorts candidates so weak catches come first (by `final_score` descending), non-weak appended.
- `_group_sort_key` now uses `has_test` as the primary key — test-backed groups always outrank review-only findings regardless of severity. Executable evidence beats LLM opinion.

## [0.1.0] — initial release

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
