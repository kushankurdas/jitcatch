# Contributing to JitCatch

Thanks for helping out. JitCatch is a small, focused tool — contributions that keep it small and focused are easier to land.

## Before you open a PR

1. **Open an issue first for anything non-trivial.** Bug reports are always welcome; for features, a short discussion up front avoids wasted work.
2. **Keep the change scoped.** One concern per PR. Refactors go in their own PR.
3. **Match the existing style.** No linter is enforced yet — read the surrounding code.

## Development setup

```bash
git clone https://github.com/<your-fork>/jitcatch
cd jitcatch
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest tests/
```

All tests run offline — no API keys, no network. If you add a test that needs a real LLM, guard it behind an env var and mark it with `pytest.mark.skipif`.

## Running the tool end-to-end

```bash
# stub provider — reads .jitcatch_stub.json, no network
jitcatch pr . --stub

# local model
ollama pull qwen2.5-coder:7b
jitcatch pr . --provider ollama --model qwen2.5-coder:7b
```

## Adding a language adapter

1. Subclass `jitcatch.adapters.base.Adapter`.
2. Register in `jitcatch/adapters/__init__.py`.
3. Add a fixture under `tests/fixtures/<lang>/` with a parent commit and a child commit that introduces a regression.
4. Extend `tests/test_smoke.py` to run the stub pipeline against the fixture.

## Pull request checklist

- [ ] `pytest tests/` passes locally.
- [ ] New behavior has a test.
- [ ] Public CLI flags are documented in `README.md`.
- [ ] `CHANGELOG.md` has an `Unreleased` entry describing the user-visible change.
- [ ] Commit messages explain *why*, not just *what*.

## Commit style

Short imperative subject, optional body. Example:

```
add dodgy-diff workflow retry cap

The retry loop could spin forever on a pathological LLM response.
Cap at --max-retries (default 3) and surface the reason in the report.
```

## Reporting bugs

Use the bug report template. The most useful bug reports include:

- `jitcatch --version` / commit SHA.
- Provider + model (`--provider ollama --model qwen2.5-coder:7b`).
- Minimal diff that reproduces the issue.
- The `.jitcatch/output/<name>.json` if one was produced.

## Security issues

Do **not** open a public issue for security vulnerabilities. See [SECURITY.md](SECURITY.md).

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating you agree to abide by it.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
