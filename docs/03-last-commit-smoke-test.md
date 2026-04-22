# Use case 03. Last-commit smoke test

**Subcommand:** `jitcatch last`
**Rev pair:** `HEAD~1` → `HEAD`
**Typical runtime:** 30 s – 2 min.

---

## When to reach for this

You have just committed. You want to convince yourself the commit you wrote does what you think it does, without pushing anywhere or opening a PR. This is the "post-commit rehearsal" use case. Cheap, focused, and perfect as the last step of your local loop before `git push`.

Reach for this use case when:

- You finished a commit and want a one-command sanity check before pushing.
- You are replaying a commit that went in without CI coverage and want retroactive signal.
- You are building muscle memory on how JitCatch scores *your* diffs so you can calibrate against the rest of the workflow.

Do **not** reach for this use case when:

- You want to check more than just the previous commit. Use [`jitcatch pr`](./04-pr-review.md) to check the whole branch against `origin/main`.
- You want to check *uncommitted* state. Use [`jitcatch staged`](./01-pre-commit-check.md) or [`jitcatch working`](./02-working-tree-check.md).

---

## Prerequisites

- You are inside a git repository.
- The repo has at least two commits on the current branch (`HEAD~1` must resolve).
- A provider is configured.

---

## Command

```bash
jitcatch last .
```

Useful variants:

```bash
# Only run the risk-driven workflow, skip the mutation-mindset one. Faster,
# less coverage, good for structured diffs where "what could go wrong" is
# easy to enumerate.
jitcatch last . --workflow intent

# Only run the mutation-mindset workflow. Better for refactors where the
# intent is "preserve behavior". See docs/10 for workflow selection logic.
jitcatch last . --workflow dodgy

# Spend less. Cap retries and skip the agentic reviewer.
jitcatch last . --max-retries 0 --no-review
```

---

## What happens under the hood

1. `resolve_last` returns `RevPair(parent=HEAD~1, child=HEAD)`. No scratch worktree is needed because both revs exist in history.
2. `git diff --numstat HEAD~1..HEAD` gives churn per file; used to select the top-N files when `--max-files` is exceeded.
3. Changed files are grouped by adapter. Each group gets its own bundle, workflow pass, and eval pair.
4. Two detached worktrees are added via `git worktree add --detach`. One at `HEAD~1`, one at `HEAD`. Each generated test is written and executed in both, then removed.
5. Rule assessor runs deterministic fp:*/tp:* flags, LLM-as-judge assigns `tp_prob`, retry loop fills in gaps, reviewer runs on the bundle.
6. Results are written to `.jitcatch/output/`.

---

## Reading the output

Same artifacts as the other subcommands. Because `last` covers a single commit, the top of the report is usually short. Most weak catches come from either the behavior *intentionally* changed or a regression you did not notice.

A useful mental model:

- **0 weak catches, 0 reviewer findings** → the commit is probably signal-free (a doc tweak, a comment, a rename handled cleanly).
- **Weak catches, no reviewer findings** → generated tests reproduce the behavior change. Decide: intentional (keep the tests as characterization tests) or regression (fix).
- **0 weak catches, reviewer findings** → the change may be visible only via paths test-gen cannot reach (mocked interfaces, env-guarded branches). Read the reviewer findings carefully.
- **Both** → inspect both sections, starting from the test-backed one.

---

## Tips

- **Pair this with a shell alias.** `alias jc='jitcatch last .'` makes post-commit checks one keystroke.
- **Use `--model-tests` with a cheap model.** Most of the token cost is test generation, which benefits less from a reasoning-heavy model than the risk-inference step. See [10-per-stage-model-routing.md](./10-per-stage-model-routing.md).
- **Do not chain this over many commits in a loop.** Each commit's context is separate; `jitcatch pr` bundles the whole series once and is almost always cheaper than N invocations of `last`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ambiguous argument 'HEAD~1'` | Repo has only one commit. | Commit at least once more, or use `jitcatch run` with explicit revs. |
| "warning: no changed files between ..." | `HEAD~1..HEAD` is a merge commit with empty tree delta, or a gitignored path. | Use `jitcatch pr --base <ref>` to get a non-trivial rev pair. |
| Run feels slow for a small commit | Retry loop is fetching 2 rounds even when the first round was comprehensive. | `--max-retries 0` for the smallest possible run. |
