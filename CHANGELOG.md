# Changelog

All notable changes to JitCatch are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Open-source release scaffolding: `LICENSE` (MIT), `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue/PR templates, CI workflow.

## [0.1.0] — 2026-04-21

Initial public release.

### Added
- Dual-worktree regression runner: pass-parent / fail-child evidence for every candidate test.
- Subcommands: `pr`, `last`, `staged`, `working`, `run`.
- Providers: Anthropic, Ollama, any OpenAI-compatible endpoint, offline `--stub`.
- Workflows: intent-aware, dodgy-diff, agentic reviewer, feedback-driven retry.
- Assessors: deterministic rule flags + LLM-as-judge, combined `final_score`.
- Adapters: Python (`pytest`), JavaScript (`node:test`).
- Reports: JSON + Markdown under `.jitcatch/output/`.

[Unreleased]: https://github.com/kushankurdas/jitcatch/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kushankurdas/jitcatch/releases/tag/v0.1.0
