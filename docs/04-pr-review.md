# Use case 04. Pull-request review against a base branch

**Subcommand:** `jitcatch pr [--base <ref>]`
**Rev pair:** `merge-base(base, HEAD)` → `HEAD`
**Typical runtime:** 1 – 10 min. Scales with diff size and file count.

---

## When to reach for this

You want JitCatch's verdict on an **entire feature branch**, the way a reviewer would see it in a pull request. This is the flagship use case: it bundles every file the branch touched, reasons about the change as a whole, and produces the richest possible report.

Reach for this use case when:

- You are preparing to open or update a pull request and want a pre-review pass.
- You are reviewing someone else's branch and want executable evidence alongside your own reading.
- You are running JitCatch in CI on every PR (see [13-ci-integration.md](./13-ci-integration.md)).

Do **not** reach for this use case when:

- You only want the last commit's signal - [`jitcatch last`](./03-last-commit-smoke-test.md) is cheaper.
- You are still iterating locally and have not pushed - [`jitcatch working`](./02-working-tree-check.md) covers uncommitted state.

---

## Prerequisites

- You are inside a git repository whose base branch is reachable (locally or via `origin`).
- The base ref you want to diff against is fetched. JitCatch autodetects `origin/HEAD` first and falls back to `origin/main`, `origin/master`, `origin/develop`.
- A provider is configured.

---

## Command

```bash
# Autodetect the base branch (origin/HEAD -> origin/main etc.)
jitcatch pr .

# Explicit base (the safer form in CI and on repos with non-standard defaults)
jitcatch pr . --base origin/main
jitcatch pr . --base origin/develop
jitcatch pr . --base upstream/trunk
```

Refinements:

```bash
# Broader reasoning: include the top-5 callers of each changed symbol.
jitcatch pr . --with-callers --max-callers 5

# Bigger branches: bump the prompt budget. Default is 200 KB.
jitcatch pr . --max-bytes 400000

# Premium pass: keep every signal, spend more on reasoning-heavy stages.
jitcatch pr . \
  --model-risks claude-sonnet-4-6 \
  --model-judge claude-sonnet-4-6 \
  --model-tests claude-haiku-4-5-20251001

# Cheap pass: skip the reviewer and retry loop, just get the weak catches.
jitcatch pr . --no-review --no-retry
```

---

## What happens under the hood

1. `resolve_pr` resolves `base` (or autodetects it via `detect_default_branch`) and computes `merge-base(base, HEAD)`. That merge base is the *parent rev*. `HEAD` is the *child rev*.
2. `git diff --numstat parent..child` gives per-file churn. Files are grouped by adapter; each group keeps the top `--max-files` by churn.
3. For each group, `context.build_bundle` assembles a single prompt with:
   - the parent source of each selected file,
   - the per-file diff,
   - optional caller sources if `--with-callers`,
   - trimmed to `--max-bytes`; files beyond that budget are hunk-windowed (50 lines around each hunk).
4. Workflows (`intent`, `dodgy`) run per group. Tests are executed in parent + child worktrees.
5. Agentic reviewer runs on the same bundle in parallel to test-gen.
6. Retry loop diffs the risk list against weak catches and generates follow-up tests for uncaught risks (see [12-feedback-retry-loop.md](./12-feedback-retry-loop.md)).

---

## Reading the output

The Markdown report is organized so the first thing you read is the most actionable:

1. **Test-backed findings (weak catches)**. Ranked by `final_score`. Each entry is a self-contained regression claim: test code, parent/child output, rule flags, judge score and rationale, target files.
2. **Reviewer-only findings**. Bugs the reviewer surfaced without a failing test. Opinion-based, validator-filtered. They never outrank test-backed findings.
3. **Likely false positives**. Collapsed at the bottom so the top of the report stays clean.

For a PR review, scan section 1 and 2 back-to-back: weak catches tell you *what the branch changed*, reviewer findings tell you *what the LLM is worried about anyway*.

---

## Tips

- **Always pin `--base` in CI.** `origin/HEAD` can be ambiguous in freshly-cloned shallow checkouts. Pass `--base origin/main` (or whatever your default is) explicitly.
- **Fetch the base ref first** in CI jobs: `git fetch --no-tags origin main:refs/remotes/origin/main`. Merge-base against an unfetched ref fails with `RevError: could not find merge-base`.
- **For long branches, `--max-bytes` is the lever that matters most.** Raising `--max-files` without raising `--max-bytes` just causes more files to be truncated.
- **Per-stage models pay off here.** A PR bundle is where the model bill happens. See [10-per-stage-model-routing.md](./10-per-stage-model-routing.md).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `could not detect default branch; pass --base explicitly` | No `origin/HEAD`, no `origin/main` / `origin/master` / `origin/develop`. | Pass `--base <ref>` explicitly. |
| `could not find merge-base between <ref> and HEAD` | The base ref is not fetched, or the branch was force-pushed against an unrelated history. | `git fetch origin <ref>`; verify `git merge-base <ref> HEAD` manually. |
| Report has only reviewer findings, no weak catches | Test-gen was starved of context. | Increase `--max-bytes`; consider `--with-callers`; pick a stronger `--model-risks`. |
| `truncated (max_tokens): N > 0` across many calls | Bundle too large for the model's output ceiling. | Shrink `--max-bytes`, raise `--max-tokens`, or switch to a higher-ceiling model for `--model-tests`. |
