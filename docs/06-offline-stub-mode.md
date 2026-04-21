# Use case 06 — Offline dry run with the stub client

**Flag:** `--stub`
**Provider:** `StubClient` — deterministic, no network, no API keys.

---

## When to reach for this

You want to exercise the full JitCatch pipeline **without** making a single LLM call. The stub client returns baked-in responses shaped like real LLM output, so every downstream stage (risk list → test code → judge → reviewer → retry) runs end-to-end. You get the wiring signal without the money or the latency.

Reach for this use case when:

- You are running JitCatch in an air-gapped environment (compliance, sensitive repos, offline CI).
- You want to verify your JitCatch install works before paying for a cloud API or downloading a model.
- You are hacking on JitCatch itself and want the tests and CLI to run fast and deterministically.
- You are demonstrating JitCatch's output shape to a team without spinning up a provider.

Do **not** reach for this use case when:

- You want real regression signal. Stub responses are *shape-correct* but *content-free* — they don't reason about your diff.

---

## Prerequisites

- None beyond a working `jitcatch` install.

---

## Command

`--stub` is compatible with every subcommand:

```bash
jitcatch last    . --stub
jitcatch pr      . --stub
jitcatch staged  . --stub
jitcatch working . --stub
jitcatch run     . --file src/foo.py --stub
```

---

## What happens under the hood

- `_make_llm` short-circuits to `StubClient(repo)` as soon as `--stub` is set, regardless of `--provider`.
- `StubClient` implements the same interface (`chat`, `chat_stage`, `total_calls`, `truncated_calls`) as the real provider clients.
- Every stage (`risks`, `tests`, `judge`, `review`) receives a canned response sufficient to continue the pipeline. Generated tests are valid for the target adapter but do not exercise the diff meaningfully.

This is **not** a mock with mutable behavior. It is a fixed stub. Its value is that the **pipeline itself** runs — worktrees are created, tests are executed, rule assessor runs, reports are written — with no external dependency.

---

## Reading the output

Identical file layout to any other run. Because the stub does not reason about your diff, **do not act on its weak catches**. Treat the output as proof that:

- `git` is happy with your revs.
- The worktree sandbox created both copies and ran tests in them.
- The adapter picked up your files.
- The report writer produced both JSON and Markdown.

If all of that succeeded, the next step is to rerun without `--stub` using a real provider.

---

## Tips

- **Great first command after installing.** `jitcatch last . --stub` tells you whether the tool is wired up, without any configuration.
- **Use it in unit tests of tools that wrap JitCatch.** The stub is deterministic, so tests built on top of its output don't flake.
- **Use it to benchmark wall-clock overhead.** Subtract stub runtime from real-provider runtime to isolate LLM latency from pipeline cost.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `no tests generated` | Stub returns empty for files an adapter did not claim. | Verify there is at least one Python or JavaScript file in the diff. |
| Run hits `git` errors, not LLM errors | Problem is with rev resolution, not with the stub. | Debug against `jitcatch last .` with a real commit history. |
| Stub run takes many seconds | Worktree creation + test execution dominate. | That is the floor; real providers add LLM latency on top. |
