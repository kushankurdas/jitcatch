# Use case 13 — Running JitCatch on every pull request in CI

**Subcommand:** `jitcatch pr . --base <ref>`
**Typical runtime:** 2 – 10 min depending on provider and diff size.

---

## When to reach for this

You want automated, repeatable regression signal on every PR — attached to the PR itself as a downloadable artifact, a status check, or a comment. The PR subcommand is designed for this: bounded by `--max-files` / `--max-bytes`, deterministic inputs, and a single Markdown file you can upload.

Reach for this use case when:

- Your team reviews PRs and wants "here is what a generator-plus-runner thinks this diff broke" as a first-class input.
- You want to enforce that PR authors triage JitCatch findings before merge.
- You are experimenting with LLM-based reviewers and want data on real PRs.

Do **not** reach for this use case when:

- You run JitCatch only locally and don't want the CI coupling.
- Your repo can't tolerate cloud LLM calls in CI for compliance reasons — use a self-hosted runner + [07-local-ollama.md](./07-local-ollama.md) instead.

---

## GitHub Actions example

The shape below mirrors JitCatch's own `.github/workflows/ci.yml` conventions. Drop it into `.github/workflows/jitcatch.yml` in your consuming repo.

```yaml
name: JitCatch

on:
  pull_request:
    branches: [main]

jobs:
  jitcatch:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0   # JitCatch needs history to compute merge-base

      - name: Fetch base branch
        run: git fetch --no-tags origin ${{ github.base_ref }}:refs/remotes/origin/${{ github.base_ref }}

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Set up Node (for JS adapter)
        uses: actions/setup-node@v4
        with:
          node-version: '20'

      - name: Install JitCatch
        run: |
          pip install 'git+https://github.com/kushankurdas/jitcatch#egg=jitcatch[dev]'

      - name: Run JitCatch
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          jitcatch pr . \
            --base origin/${{ github.base_ref }} \
            --filename jitcatch-pr-${{ github.event.pull_request.number }} \
            --max-retries 1 \
            --max-retry-risks 6 \
            --verbose

      - name: Upload report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: jitcatch-report
          path: .jitcatch/output/*

      - name: Comment on PR
        if: always()
        uses: marocchino/sticky-pull-request-comment@v2
        with:
          path: .jitcatch/output/jitcatch-pr-${{ github.event.pull_request.number }}.md
```

---

## What to configure

| Concern | Lever |
|---|---|
| **Runtime budget** | `--max-retries`, `--max-retry-risks`, `--no-review` if you only want weak catches. |
| **LLM spend** | Per-stage models (see [10-per-stage-model-routing.md](./10-per-stage-model-routing.md)). For GitHub-hosted runners, Anthropic is simplest. |
| **Privacy** | Self-hosted runner + `--provider ollama` (see [07-local-ollama.md](./07-local-ollama.md)). |
| **Artifact size** | `.jitcatch/output/*.md` is a few KB; `.jitcatch/logs/` under `--verbose` can grow to several MB. Upload them separately if at all. |
| **Base ref** | Always pin `--base origin/<ref>` explicitly. `origin/HEAD` is unreliable in shallow CI checkouts. |

---

## What happens under the hood (specific to CI)

- `actions/checkout@v4` defaults to shallow depth 1, which breaks `merge-base`. Set `fetch-depth: 0` or fetch the base ref explicitly.
- `WorktreeSandbox` writes under `.git/worktrees/` inside the runner's workspace. No special FS permissions are required.
- Generated tests run with the invoking user's privileges. Treat the runner like any other test runner — a compromised PR could ship code that runs at `pytest` time.
- The `--filename` flag pins the output filename so you can upload and comment on it deterministically.

---

## Tips

- **Gate merges loosely, not strictly.** JitCatch is a *reviewer aid*, not a correctness oracle. Failing the CI job on "any weak catch" will frustrate authors whose intentional behavior changes trigger weak catches. A comment + artifact is almost always the right surface.
- **Cache the provider.** With Anthropic, prompt caching means re-running JitCatch on the same PR after a small push is a fraction of the cost of the first run. Don't clear artifacts between runs.
- **Segregate verbose logs.** If `--verbose` is on, route `.jitcatch/logs/` to a separate artifact upload so it does not get surfaced in PR comments.
- **Monitor `truncated (max_tokens)` in stderr.** A truncated judge or reviewer call silently degrades the report. Treat any non-zero truncation count as a warning worth fixing.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `could not find merge-base between origin/main and HEAD` | Shallow checkout. | `fetch-depth: 0` on `actions/checkout`, or explicit `git fetch`. |
| Silent empty report | The PR touches only files no adapter supports (e.g. YAML only). | Expected; report will say `no changed files match a known adapter`. |
| CI wall-clock >> local | Retry loop + reviewer + large bundle + cold cache. | Lower `--max-retries`, raise `--max-bytes` only if needed, route test-gen to a cheaper/faster model. |
| Secret leaked in artifact | `--verbose` log contains a snippet from a prompt that included diff content. | Treat JitCatch artifacts as sensitive; restrict artifact retention; disable `--verbose` in CI if diffs contain secrets. |
