# Use case 14. JavaScript projects (ESM and CommonJS)

**Adapter:** `jitcatch.adapters.javascript`
**Extensions:** `.js`, `.mjs`, `.cjs`
**Test runner:** `node --test` (the built-in `node:test` module).

---

## When to reach for this

Your repo is JavaScript (or mixed Python + JavaScript) and you want JitCatch's regression check to cover it the same way it covers Python. The JavaScript adapter ships in-tree, auto-detects ESM vs CommonJS, and uses `node:test` so you do not need Jest or Vitest installed.

Reach for this use case when:

- Your repo contains `.js`, `.mjs`, or `.cjs` files that would otherwise be invisible to test-gen.
- You are shipping a mixed-language PR and want one JitCatch run to cover both Python and JavaScript.
- You prefer `node:test` over a heavier test framework for generated tests.

---

## Prerequisites

- **Node.js ≥ 18.** `node --test` landed in 18. Older versions will fail at test execution time.
- A clean `package.json` if your project uses ESM. JitCatch reads the `"type"` field to decide ESM vs CommonJS per file.

---

## Command

No special flags. The adapter is picked up automatically from file extensions. Every JitCatch subcommand supports JavaScript:

```bash
jitcatch pr .
jitcatch staged .
jitcatch working .
jitcatch last .
jitcatch run . --file src/server/auth.mjs
```

---

## What happens under the hood

1. `adapters.for_file(<path>)` returns the JavaScript adapter for `.js`, `.mjs`, or `.cjs`.
2. `prompt_hints` embeds language-specific guidance (ESM import syntax, `node:test` assertions) in the bundle.
3. `detect_runner` (used for future extensibility) recognizes Jest and Vitest configs but JitCatch itself currently writes only `node:test` harnesses.
4. `write_test` emits the generated test into the worktree under a sibling filename (`<target>.jitcatch.test.mjs` or `.cjs` to match ESM/CJS).
5. `run_test` invokes `node --test <path>`; exit code and stdout/stderr become the `TestResult`.

---

## ESM vs CommonJS selection

Selection is driven by three inputs, in order:

1. The target file's extension:
   - `.mjs` → ESM.
   - `.cjs` → CommonJS.
   - `.js` → ambiguous; see next rule.
2. The nearest `package.json`'s `"type"` field:
   - `"type": "module"` → `.js` files are ESM.
   - `"type": "commonjs"` (or absent) → `.js` files are CommonJS.
3. The generated test filename mirrors the target:
   - ESM target → `.test.mjs`.
   - CJS target → `.test.cjs`.

This makes generated tests consistent with the target file's module system, so imports and mocks behave predictably.

---

## Reading the output

Identical to Python runs:

- **Test-backed findings** - `node --test` passed on parent, failed on child. The rendered Markdown includes the test code and parent/child stdout from `node --test`'s TAP-ish output.
- **Reviewer-only findings**. Diff-level reasoning the reviewer surfaced.
- **Likely false positives**. Collapsed at the bottom.

`rule_flags` and `judge_tp_prob` behave identically; the `fp:flakiness` flag is particularly relevant on JavaScript tests with timer-based assertions.

---

## Tips

- **Keep generated tests close to the target.** The default layout places `.jitcatch.test.*` next to the source file so ESM resolvers and path aliases behave the same as in hand-written tests.
- **Node-only APIs only.** Generated tests use `node:test`, `node:assert`, and standard-library modules. They do not assume Jest, Vitest, Mocha, or Chai. Your project does not need those to be installed.
- **Compilers and transpilers are out of scope.** JitCatch does not run a TypeScript compiler. A `.ts` file will not be picked up by the JavaScript adapter. For TypeScript-heavy repos, wait on a dedicated adapter or compile to `.js` before running JitCatch.
- **Multi-language PRs.** A PR that touches Python and JavaScript runs both workflows in parallel per adapter group. You get one report covering both.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `node: unknown option --test` | Node version < 18. | Upgrade Node; JitCatch documents 18+. |
| ESM test fails with `require is not defined` | Target is ESM (`.mjs` or `package.json`'s `"type":"module"`) but the generated test uses `require`. | Usually a prompt issue - rerun; if persistent, raise `--model-tests`. |
| CJS test fails with `Cannot use import statement outside a module` | Target is CommonJS but the generated test uses `import`. | Same as above - swap to a stronger test-gen model. |
| `ValueError: no adapter for <file>` | File extension isn't `.js`, `.mjs`, `.cjs`. | Either add a custom adapter (see README) or exclude the file from the PR scope. |
| Tests flake on timers | `node --test` doesn't have deterministic fake timers by default. | Treat `fp:flakiness` rule flags as accurate; accept that time-coupled code is hard to test-gen. |
