# Use case 02 — Working-tree check (staged + unstaged)

**Subcommand:** `jitcatch working`
**Rev pair:** `HEAD` → synthetic commit combining `git diff --cached` **and** `git diff` (unstaged)
**Typical runtime:** comparable to `staged`.

---

## When to reach for this

You are mid-edit. You have not yet run `git add`. You want a regression signal on **everything you have touched**, whether or not it has been staged. This is the broadest local check JitCatch offers and is the right choice during exploratory development when you want feedback without interrupting your flow to stage files.

Reach for this use case when:

- You are iterating quickly and want a check against all pending edits, staged or not.
- You want to decide *how to split* your work into commits — JitCatch's ranking highlights which edits changed observable behavior and which were inert.
- You have a hunch that an "innocuous" refactor touched something it should not have.

Use [`jitcatch staged`](./01-pre-commit-check.md) instead when you have already curated the index with `git add -p` and only want to check the commit you are about to create.

---

## Prerequisites

- You are inside a git repository.
- At least one of `git diff --cached` / `git diff` is non-empty. If both are empty, JitCatch exits with `RevError: no working-tree changes (staged or unstaged)`.
- A provider is configured (see [07-local-ollama.md](./07-local-ollama.md), [08-anthropic-claude.md](./08-anthropic-claude.md), [09-openai-compatible-providers.md](./09-openai-compatible-providers.md)).

---

## Command

```bash
jitcatch working .
```

Useful variants:

```bash
# Include callers of changed symbols in the bundle — helps when a function
# changed signature and its callers have subtle call-site expectations.
jitcatch working . --with-callers

# Trim to the churn-heaviest files only. Good for large, messy branches.
jitcatch working . --max-files 10

# Offline rehearsal — confirms the pipeline runs end to end without any LLM.
jitcatch working . --stub
```

---

## What happens under the hood

1. JitCatch reads both staged (`git diff --cached --binary`) and unstaged (`git diff --binary`) patches.
2. A detached scratch worktree is created at `HEAD`. Both patches are applied (staged first, then unstaged), with `--index` so they are committed atomically.
3. The scratch commit's SHA becomes the *child rev*. Your index and working tree are **never touched**.
4. From that point on, the pipeline is identical to `staged`: bundle, workflows, worktree eval, rule assessor, LLM judge, retry.

The ordering — staged then unstaged — matters only when the two overlap. For the common case (disjoint edits) ordering is irrelevant.

---

## Reading the output

Identical shape to [`jitcatch staged`](./01-pre-commit-check.md). Two artifacts per run under `.jitcatch/output/`:

- A **test-backed findings** section ranked by `final_score`.
- A **reviewer-only findings** section (validator-filtered, opinion-based — see [11-agentic-reviewer.md](./11-agentic-reviewer.md)).
- A **likely false positives** collapsed at the bottom.

A weak catch here is especially actionable: the test exists, it currently fails against your uncommitted state, and it will keep failing until you either fix the regression or decide the behavior change was intentional — in which case keep the generated test as a characterization test for the new behavior.

---

## Tips

- **Use `--with-callers` for signature-changing diffs.** JitCatch picks the top-N callers of changed symbols and includes their source in the prompt, which helps the LLM reason about contract breaks that are only visible at the call site.
- **Treat this mode as a branch-review helper.** If a weak catch appears against an edit you had considered "pure cleanup", that is the strongest possible signal that the cleanup was not pure.
- **Low-signal runs are informative too.** No weak catches across a messy working tree, combined with a non-empty reviewer section, usually means "the behavior did not change, but style/structure changed in ways a reviewer might question".

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `no working-tree changes (staged or unstaged)` | Nothing to check. | `git status`; make sure you are in the right repo. |
| `git apply failed in scratch worktree` | Staged and unstaged patches conflict when applied in sequence. | Commit or stash one half, re-run, and check the other half separately. |
| Run is very slow | Default `--workflow both` plus retry loop plus reviewer on a large diff. | Start with `--no-retry --no-review` for a first pass; re-run fully once you've triaged. |
| `truncated (max_tokens): N > 0` | Prompt + response hit the model's output ceiling. | Lower `--max-bytes`, raise `--max-tokens`, or split the change into smaller working sets. |
