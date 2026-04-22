# Use case 15 — Explain a candidate (interactive LLM chat)

**Subcommand:** `jitcatch explain <repo> <id-prefix>`
**Rev pair:** — (reads an existing report, no rev resolution)
**Typical runtime:** instant for `--no-chat`; otherwise as long as your chat session.

---

## When to reach for this

You have a JitCatch report and one candidate in it that you want to understand more deeply. Instead of hand-opening the JSON, copy-pasting the test code, stdout, stderr, and judge rationale into another window, and then asking an LLM "what does this mean?" — you point `jitcatch explain` at the candidate's id and it drops you into a chat already seeded with everything about that candidate.

Reach for this use case when:

- A weak catch is ranked high but you are not sure whether it is a real regression or a subtle test artifact.
- A judge rationale says "bucket=High" with a one-line reason and you want to interrogate it.
- A reviewer-only finding looks plausible but you want to stress-test it before filing a bug.
- You want a copy-pasteable fix suggestion grounded in the parent/child stdout, not a generic "here's how you might fix it".
- You are scripting around JitCatch and want the full candidate detail block as stdout — use `--no-chat`, or just pipe the command through `less` / `cat`.

Do **not** reach for this use case when:

- You have not generated a report yet — run one of `last`, `pr`, `staged`, `working`, or `run` first.
- You want to regenerate tests — `explain` is read-only against the report; it never calls the test-gen pipeline.

---

## Prerequisites

- You are inside a git repository.
- At least one JSON report exists under `<repo>/.jitcatch/output/` — or you pass `--report <path>` explicitly. JSON is always written by every other subcommand, so any prior run works.
- For the chat REPL: an LLM provider is configured (same rules as `pr`/`last`). `--no-chat` and non-tty stdin bypass this.

---

## Command

```bash
# 1. generate a report — any subcommand works, JSON is always written
jitcatch last .

# 2. copy a candidate id from the stdout summary (or from the JSON "id" field)
#    any prefix of 4+ hex chars is enough as long as it is unambiguous
jitcatch explain . a7f3b2
```

Useful variants:

```bash
# Skip the REPL, just print the full detail block (test code, parent/child
# stdout/stderr, judge rationale, rule flags, risks) and exit. No LLM call.
jitcatch explain . a7f3b2 --no-chat

# Explain a candidate from a specific report instead of the latest one.
jitcatch explain . a7f3b2 --report .jitcatch/output/jitcatch-20260422-091500.json

# Pipe-friendly: stdin is non-tty so the REPL is auto-skipped and the plain
# detail block is emitted.
jitcatch explain . a7f3b2 | less

# Force a specific provider/model for the chat (same flags as pr/last).
jitcatch explain . a7f3b2 --provider anthropic --model claude-sonnet-4-6
jitcatch explain . a7f3b2 --provider ollama --model qwen2.5-coder:7b
jitcatch explain . a7f3b2 --stub              # offline, canned replies
```

All LLM provider flags from the generation subcommands are accepted: `--stub`, `--provider`, `--base-url`, `--model`, `--max-tokens`, `--llm-timeout`, `--verbose`, `--log-dir`. Stage-model overrides (`--model-risks`, `--model-tests`, ...) are not used by chat and can be omitted.

---

## What happens under the hood

1. `_resolve_explain_report` picks the report — either `--report <path>` or the most recently modified `jitcatch-*.json` under `<repo>/.jitcatch/output/` by mtime.
2. The JSON is loaded and searched for candidates whose `id` starts with the supplied prefix. The prefix must be at least 4 characters; an ambiguous prefix (multiple matches) prints all the matching ids and exits non-zero so you can disambiguate.
3. The single matched candidate is rendered into a plain detail block covering: `id`, test name, workflow, `weak_catch`, `final_score`, `judge_tp_prob`, `judge_bucket`, target files, rule flags, risks, judge rationale, test code, and the parent + child `status` / `exit_code` / `stdout` / `stderr`.
4. If `--no-chat` is set, or stdin is not a tty (pipe, redirect, CI), that detail block is printed and the command exits. No LLM client is constructed — so `--no-chat` is safe on machines without API keys.
5. Otherwise, `_make_llm` constructs a provider client using the same rules as the generation subcommands. A system prompt is assembled from the candidate JSON (run meta, candidate fields, rationale, test code, parent/child results) and a colored banner is printed.
6. A line-based REPL runs: you type a question, the client's `chat(system, messages, label="explain.chat")` is called, the reply is streamed back. The transcript is kept in-memory for follow-up turns. Exit with an empty line, `exit`/`quit`/`:q`, or Ctrl-D.

The REPL is grounded in the candidate data already in the system prompt — it does not re-read the report, hit git, or run any tests. Answers about code paths not present in the JSON will be acknowledged as such rather than guessed.

---

## Reading the output

### Detail block (always printed, except when you enter the chat REPL)

Human-readable dump of every field the JSON persists. A minimal example:

```
id:          a7f3b2c1d0e9
name:        test_parses_empty_body
workflow:    intent_aware
weak_catch:  True
final_score: +0.723
judge:       tp_prob=+0.82 bucket=High
files:       src/http/parser.py
flags:       tp:null_value
risks:
  - [src/http/parser.py:42] empty body produces None, not ""

-- rationale --
Child swallows empty-body case and returns None, but parent always
returned the empty string. Callers downstream index [0] on it.

-- test code --
def test_parses_empty_body():
    ...

-- parent result (status=passed, exit_code=0) --
stdout: ...

-- child result (status=failed, exit_code=1) --
stdout: ...
stderr: IndexError: string index out of range

source: /repo/.jitcatch/output/jitcatch-20260422-091500.json
```

Use this mode whenever you want the raw data — it is stable enough to grep.

### Chat REPL (tty, no `--no-chat`)

```
────────────────────────────────────────────────────────────
  jitcatch explain  a7f3b2c1d0e9  test_parses_empty_body  intent_aware  bucket=High  score=+0.72  weak-catch
────────────────────────────────────────────────────────────
  ask about this candidate. empty line, 'exit', or Ctrl-D to quit.

you ❯ is this a real regression or a flake?
  thinking…
llm ❯ The `fp:flake_runtime` flag wasn't set and the child failed on …

you ❯ what would a minimal fix look like?
llm ❯ …
```

- `you ❯` prompt is cyan; `llm ❯` prompt is green. Colors render only when stdout is a tty and `NO_COLOR` is not set.
- Exit cleanly with an empty line, `exit` / `quit` / `:q`, or Ctrl-D.
- A transient `  thinking…` line is printed while the client is awaiting a response and erased before the reply renders.
- If the LLM call fails (timeout, API error, truncation), the error is printed to stderr and the last user message is dropped from history so you can retry without duplicating it.

---

## Tips

- **Always works after any generation run.** JSON is written by every subcommand regardless of `--format`. You do not need to re-run with `--format md` just to have something for `explain` to read.
- **Prefix collisions are cheap to resolve.** If a 4-char prefix matches two candidates, the error message lists the full ids — copy the longer one back in.
- **Use `--no-chat` in CI.** It is deterministic, needs no API key, and prints everything you would get out of parsing the JSON by hand.
- **Pipe the plain block into other tools.** `jitcatch explain . a7f3b2 | pbcopy`, `| less`, `| tee ~/triage.txt` all work — stdin auto-detection drops the REPL.
- **Switch models per session.** The cheapest provider that answers the question well is good enough — `explain` does not benefit from a reasoning-heavy model the way judging does. `--provider ollama --model qwen2.5-coder:7b` is usually plenty.
- **`--stub` is useful for demos.** Produces canned replies so you can screenshot the REPL without spending tokens.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `error: no JSON report found under <repo>/.jitcatch/output` | No prior run, or `.jitcatch/output/` was wiped. | Run `jitcatch last .` (or any other subcommand) first, or pass `--report <path>` to a known JSON. |
| `error: id prefix must be at least 4 characters` | You passed `abc` or a shorter token. | Copy a longer prefix from the JSON report's `"id"` field or the stdout summary. |
| `error: no candidate matching id prefix '<x>'` | Prefix typo, or the report you're reading is older than the one where that id exists. | Check the `"id"` list in the loaded JSON, or pass `--report` to the right file. |
| `error: id prefix '<x>' is ambiguous — N matches` | Your prefix matches multiple candidates. | Copy one of the full ids the error listed and re-run. |
| The REPL never opens — only the detail block is printed | Stdin is not a tty (piped input, running under some CI runners) or `--no-chat` was passed. | Run in an interactive terminal without piping, and drop `--no-chat`. |
| `✗ cannot start chat: <error>` | LLM client failed to construct — missing `ANTHROPIC_API_KEY`, Ollama not running, bad `--base-url`. | Same fix as for generation subcommands: see the LLM-backend docs (06–09). The detail block is still printed so the session is not useless. |
| Colored prompts look like `\033[36m` garbage | Terminal strips ANSI, or you are looking at captured output. | Already handled automatically when stdout is not a tty. To force plain output explicitly, set `NO_COLOR=1`. |
| Chat answers feel generic / hallucinated | Provider picked a model without enough context, or the candidate's stdout/stderr is empty (nothing to ground on). | Swap to a stronger model for that session (`--model ...`), or fall back to `--no-chat` and reason from the detail block directly. |
