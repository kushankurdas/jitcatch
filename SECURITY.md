# Security Policy

## Supported versions

JitCatch is pre-1.0. Only the latest release on `main` receives security fixes.

| Version | Supported |
|---------|-----------|
| `main`  | ✅        |
| `< 0.1` | ❌        |

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report privately via GitHub's [private vulnerability reporting](https://github.com/kushankurdas/jitcatch/security/advisories/new) feature, or email the maintainer (see the `author` field in `pyproject.toml` or the commit history).

Include:

1. A description of the issue and its impact.
2. Steps to reproduce, ideally with a minimal repo or diff.
3. Affected version / commit SHA.
4. Your disclosure timeline preference.

### What to expect

- **Acknowledgement** within 7 days.
- **Triage + fix plan** within 30 days for confirmed issues.
- **Coordinated disclosure.** A CVE will be requested for high-severity issues. Credit is given in the release notes unless you ask otherwise.

## Threat model

JitCatch is a developer CLI. It runs on a developer's machine (or CI) against a repository the operator already trusts. The in-scope threats are:

| Concern | In scope |
|---------|----------|
| Arbitrary code execution from a malicious diff | ✅ JitCatch clones into worktrees and runs generated tests. |
| Arbitrary code execution from a malicious LLM response | ✅ The LLM writes test code that JitCatch executes. |
| Credential leakage (API keys in logs / reports) | ✅ Anthropic / OpenAI-compatible keys are read from env and must never appear in `.jitcatch/output/` or `--log-dir`. |
| Path traversal when the LLM names a test file | ✅ Adapters must sandbox writes inside the worktree. |
| Denial of service via long-running tests | ⚠️ Mitigated by `--timeout`; not a hard sandbox. |

**Out of scope:**

- Running JitCatch against a repo you do not trust to execute. The tool runs code from the repo by design.
- Supply-chain attacks against `pip install`-time dependencies — report those upstream (`anthropic`, `pytest`).
- LLM model-quality issues (hallucinated tests, false positives). Those are bugs, not vulnerabilities.

## Hardening recommendations for operators

- Run JitCatch inside a container or VM when testing an untrusted PR.
- Use a per-repo, short-lived API key. Rotate on suspicion.
- Never pass `--log-dir` to a path shared across repos; transcripts may contain code snippets from the diff.
- Review `.jitcatch/output/*.md` before sharing — generated tests can contain strings from the diff verbatim.

## Safe harbor

Good-faith security research that follows this policy will not be met with legal action. We welcome coordinated disclosure.
