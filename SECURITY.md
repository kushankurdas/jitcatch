# Security Policy

## Supported versions

JitCatch is pre-1.0. Only the latest release on the `main` branch receives security fixes. Older versions should be upgraded rather than patched.

| Version | Supported |
|---|---|
| `main` / latest release | Yes |
| Anything else | No |

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report privately via GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) on this repository.

Please include:

- A description of the vulnerability and its impact.
- Steps to reproduce (ideally a minimal test case).
- The commit SHA or version you tested against.
- Your name/handle for credit (optional).

### What to expect

- **Acknowledgement** within 72 hours of report.
- **Initial assessment** within 7 days.
- **Fix or mitigation plan** communicated within 30 days for confirmed vulnerabilities. Complex issues may take longer. If so, you'll get a status update at the 30-day mark.
- **Credit** in the release notes and/or a GitHub security advisory, unless you prefer anonymity.

## Scope

In scope:

- Code execution or privilege escalation triggered by running JitCatch on an attacker-controlled repository.
- Secret leakage (API keys, credentials) from JitCatch's code or default configuration.
- Path-traversal or sandbox-escape bugs in the worktree runner.
- Prompt-injection vectors that cause JitCatch to exfiltrate secrets or execute unintended commands.

Out of scope:

- Bugs in your LLM provider (Anthropic, Ollama, or any OpenAI-compatible endpoint). Report those upstream.
- Vulnerabilities in `pytest`, `node`, or other tools JitCatch shells out to.
- Issues that require the attacker to already have code execution on the host machine.
- Social engineering of maintainers.

## Threat model assumptions

JitCatch is designed to be run **locally, against repositories the user trusts**. It executes generated tests in a worktree with the same privileges as the invoking user. Running JitCatch on untrusted code is equivalent to running that untrusted code directly. The worktree sandbox is for isolation of revs, not for security containment.

For CI use, run JitCatch inside a disposable container or sandboxed job with the minimum secrets and filesystem access required.
