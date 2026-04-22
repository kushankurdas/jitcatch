# JitCatch — Use Case Documentation

This directory contains one document per use case. Each file is self-contained: **when to use it**, **the exact command**, **what happens under the hood**, **what the output means**, and **common pitfalls**.

Start with the use case that matches your workflow — you do not need to read them in order.

## Index

### Developer loops (local)

| # | File | Use case | Subcommand |
|---|---|---|---|
| 01 | [pre-commit-check.md](./01-pre-commit-check.md) | Check staged changes before `git commit` | `jitcatch staged` |
| 02 | [working-tree-check.md](./02-working-tree-check.md) | Check uncommitted changes (staged + unstaged) | `jitcatch working` |
| 03 | [last-commit-smoke-test.md](./03-last-commit-smoke-test.md) | Smoke-test the commit you just made | `jitcatch last` |
| 04 | [pr-review.md](./04-pr-review.md) | Review a whole PR against its base branch | `jitcatch pr` |
| 05 | [single-file-review.md](./05-single-file-review.md) | Target one file between explicit revs | `jitcatch run` |
| 15 | [explain-candidate.md](./15-explain-candidate.md) | Inspect a single finding and chat with an LLM about it | `jitcatch explain` |

### LLM backends

| # | File | Use case |
|---|---|---|
| 06 | [offline-stub-mode.md](./06-offline-stub-mode.md) | Run the full pipeline with zero network and no API keys |
| 07 | [local-ollama.md](./07-local-ollama.md) | Use a local Ollama model (zero config, free, private) |
| 08 | [anthropic-claude.md](./08-anthropic-claude.md) | Use Anthropic's Claude via `ANTHROPIC_API_KEY` |
| 09 | [openai-compatible-providers.md](./09-openai-compatible-providers.md) | Groq, Together, Fireworks, OpenRouter, LM Studio, vLLM, LocalAI |

### Signal quality & cost tuning

| # | File | Use case |
|---|---|---|
| 10 | [per-stage-model-routing.md](./10-per-stage-model-routing.md) | Use cheap models for bulk work, premium models for reasoning |
| 11 | [agentic-reviewer.md](./11-agentic-reviewer.md) | Surface bugs that cannot be exercised by a generated test |
| 12 | [feedback-retry-loop.md](./12-feedback-retry-loop.md) | Re-ask the LLM for tests covering risks the first round missed |

### Integrations

| # | File | Use case |
|---|---|---|
| 13 | [ci-integration.md](./13-ci-integration.md) | Run JitCatch on every pull request in CI |
| 14 | [javascript-projects.md](./14-javascript-projects.md) | Use JitCatch on JavaScript (ESM and CommonJS) repos |

## Concepts used throughout

These terms appear in most files; they are defined once here so individual use cases can stay short.

- **Parent rev / Child rev.** The two git SHAs JitCatch compares. A test that **passes on parent** and **fails on child** is a *weak catch* — executable evidence that the diff changed behavior.
- **Weak catch.** The core invariant of the tool. Every ranking signal (rule flags, LLM-as-judge, `final_score`) is built on top of this pass/fail pair.
- **WorktreeSandbox.** JitCatch runs each generated test inside a detached `git worktree` for the parent and another for the child. Your working tree and index are never mutated.
- **Bundle.** A single prompt assembled from all changed files that share a language adapter (Python, JavaScript). Capped by `--max-bytes` and `--max-files`.
- **Workflows.** Two test-gen strategies run by default: `intent` (risks-first) and `dodgy` (mutation-mindset). Pick one with `--workflow`.
- **Output.** Two files per run under `<repo>/.jitcatch/output/` — one JSON, one Markdown. Weak catches are ranked first; reviewer-only findings appear in their own section.

See the [top-level README](../README.md) for the full CLI reference, architecture diagram, and threat model.
