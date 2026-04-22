# Use case 01. Pre-commit check

**Subcommand:** `jitcatch staged`
**Rev pair:** `HEAD` → synthetic commit of `git diff --cached`
**Typical runtime:** 30 s – 3 min, depending on diff size and provider.

---

## When to reach for this

You have finished editing, you have run `git add`, and you want a regression check **before** `git commit` seals the history. This is the lowest-friction entry point to JitCatch: no remote branch is required, no commit has been written yet, and nothing in your repo is mutated.

Reach for this use case when:

- You are about to commit a behavior change and want executable evidence that the change actually does what you think it does.
- You want to catch a subtle regression in a refactor *before* it ever leaves your machine.
- You are practicing trunk-based development and want a fast local gate that does not require pushing to a branch first.

Do **not** reach for this use case when:

- You have unstaged edits you also want checked. Use [`jitcatch working`](./02-working-tree-check.md) instead.
- You want to re-check a commit that already exists. Use [`jitcatch last`](./03-last-commit-smoke-test.md).

---

## Prerequisites

- You are inside a git repository.
- `git diff --cached` is non-empty. If the staging area is empty, JitCatch exits with `RevError: no staged changes`.
- A provider is configured (Ollama running locally, `ANTHROPIC_API_KEY` exported, or `--stub` for offline dry runs).

---

## Command

```bash
jitcatch staged .
```

That is the whole command. Sensible defaults take over from there: `--workflow both`, `--provider auto`, judge + reviewer + retry loop enabled, max 2 retry rounds.

Common refinements:

```bash
# Fast check. Skip the reviewer and retries, keep just the weak-catch signal.
jitcatch staged . --no-review --no-retry

# Deeper check. Force both workflows and do not skip anything (this is the default).
jitcatch staged .

# Offline smoke test. No network, no API keys.
jitcatch staged . --stub
```

---

## What happens under the hood

1. JitCatch reads your staged patch with `git diff --cached --binary`.
2. It creates a **detached scratch worktree** at `HEAD`, applies the patch there, and commits it. The commit SHA of that throwaway commit becomes the *child rev*. Your real index and working tree are untouched.
3. Changed files are grouped by language adapter. Files without an adapter are skipped.
4. For each group, a bundled prompt is built and the test-gen workflows (`intent`, `dodgy`) run.
5. Each generated test is written into a **parent worktree** (at `HEAD`) and a **child worktree** (at the scratch commit), then executed in both.
6. Tests that **pass on parent and fail on child** become weak catches. They are rule-flagged, judged by the LLM, and ranked.
7. The scratch worktree is torn down via `RevPair.close()` on exit.

---

## Reading the output

JitCatch writes two files to `.jitcatch/output/` and prints a summary to stdout.

- `jitcatch-<timestamp>.json`. Machine-readable candidates, weak catches first.
- `jitcatch-<timestamp>.md`. Human-readable ranking with test code, parent/child output, judge scores, and reviewer findings in a separate section.

The **top section of the Markdown file** is what you care about before committing. Each weak catch tells you:

- The test that fired.
- Which files it targets.
- The diff between parent and child behavior (stdout/stderr).
- Judge confidence (`tp_prob`, `bucket`, rationale).
- A `final_score` combining rule flags and judge output.

If that section is empty, JitCatch found no executable regression. That is not a guarantee of correctness. It is a strong signal for the diffs it could reason about and test.

---

## Tips

- **Your index is safe.** The scratch worktree is additive. Even if JitCatch crashes mid-run, the `_ScratchWorktree` finalizer removes it on the next run.
- **Empty staging area?** Run `git add -p` first. JitCatch refuses to guess what you meant.
- **Binary or generated files.** These are passed through `git apply --binary` but are typically skipped at the adapter step. No test is generated against a file whose extension no adapter claims.
- **Pre-commit hook.** Wiring this as a git hook is possible but not recommended as a mandatory gate: runtimes vary with diff size and can exceed a developer's patience for a hook. Prefer opt-in via a shell alias.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `RevError: no staged changes` | Index is empty. | `git add <files>` then retry. |
| `git apply failed in scratch worktree` | Staged patch conflicts with what's on HEAD (rare after successful `git add`). | `git status`; re-stage a clean patch. |
| No weak catches, lots of tests generated | Model produced plausible-looking tests that do not target the actual change. | Re-run with `--verbose` and inspect `.jitcatch/logs/` to see the risk list. Consider a stronger model for `--model-risks`. |
| `truncated (max_tokens): N > 0` in stderr | A response was cut off. | Raise `--max-tokens`, lower `--max-bytes`, or switch the test stage to a higher-ceiling model. |
