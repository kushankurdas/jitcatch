# Use case 05 — Single-file review with explicit revs

**Subcommand:** `jitcatch run --file <path> [--parent <rev>] [--child <rev>]`
**Rev pair:** whatever you pass.
**Typical runtime:** 10 s – 90 s.

---

## When to reach for this

You want to ask JitCatch a very specific question: *did `<file>` behave differently between `<revA>` and `<revB>`?* This is the escape hatch below the bundle-based subcommands — it targets one file, does not autodetect revs, and skips the adapter-group aggregation. Use it for forensics, bisects, and API-stability checks.

Reach for this use case when:

- You are bisecting a flaky or slow regression and want per-rev signal on one file.
- You want to verify behavior stability across a release boundary (`v1.2.0..v1.3.0`).
- You are writing a characterization-test harness and want JitCatch to seed it with candidates.
- You are integrating JitCatch into another tool that wants tight control over which file and which revs get processed.

Do **not** reach for this use case when:

- You want the multi-file, multi-language bundle pipeline — use one of `last`, `pr`, `staged`, `working`.
- You want JitCatch to pick files for you by churn.

---

## Prerequisites

- `<file>` must exist at `<parent>` and have a language adapter (Python, JavaScript today).
- Both revs must resolve in the repo.

---

## Command

```bash
# Compare HEAD~1 and HEAD for one file
jitcatch run . --file src/payments/charge.py

# Compare two arbitrary revs
jitcatch run . --file src/api/users.py \
  --parent release-1.0 --child release-1.1

# With caller context
jitcatch run . --file src/core/auth.py \
  --parent origin/main --child HEAD \
  --with-callers --max-callers 5
```

All the shared flags (`--workflow`, `--provider`, `--model`, `--max-tokens`, `--no-judge`, `--no-review`, `--no-retry`, …) work the same as in bundle subcommands.

---

## What happens under the hood

1. The CLI calls `cmd_run` directly (no rev resolver, no bundle aggregation).
2. `diff.read_file_at_rev` reads the file at the parent rev; `diff.get_diff` emits the per-file diff.
3. If `--with-callers` is set, the top-N callers of the target file are appended to `parent_source` as a `# USAGE CONTEXT` block. They are not tested; they exist only to inform the prompt.
4. Workflows run on the single file. A **single-file bundle** of shape `[(file, parent_source, diff)]` is built so the reviewer and retry loop see the same shape as in bundle mode.
5. Tests are executed in parent + child worktrees, ranked, written to `.jitcatch/output/`.

---

## Reading the output

Same artifacts, smaller scope. Every weak catch here is about one file, so `target_files` is always `[--file]`. The reviewer section is typically terse because the bundle is tiny.

This mode is where **judge rationale** is most valuable — with a tightly scoped diff, the LLM-as-judge has enough surface area to write a specific rationale you can cite in a bug report or a commit message.

---

## Tips

- **Great for bisecting.** Wrap `jitcatch run` in a `git bisect run` script: exit 0 when no weak catch appears, exit 1 when one does, and `git bisect` will find the introducing commit.
- **Use `--workflow intent` for signature or contract changes.** The risk list is the valuable output; `dodgy` is less informative for intentional API shifts.
- **Skip the reviewer for one-file runs.** `--no-review` often saves 30–60 s without losing signal in this mode.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `error: <file> is empty or missing at parent <rev>` | The file did not exist at `<parent>`. | Pick a `<parent>` where the file existed, or test against a later baseline. |
| `warning: no diff for <file> between ...` | The file is unchanged between the two revs. | Nothing to do — pipeline still runs but no test-gen signal is expected. |
| `ValueError: no adapter for <file>` | Extension is not Python or JavaScript. | Not supported yet — see the README for how to add a language adapter. |
