# Contributing to JARVIS

Thanks for your interest in the project. This guide covers local setup, the
conventions the codebase follows, and how to get a change merged.

All source lives in [`jarvis_v1/`](jarvis_v1/) — `cd` there first.

## Development setup

```bash
cd jarvis_v1
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -e ".[dev]"         # app + pytest, ruff, mypy
```

For the full runtime you also need [Ollama](https://ollama.com) with the models
pulled (see the [root README](README.md#prerequisites)). The **test suite does
not require Ollama, a GPU, or a microphone** — heavy dependencies are skipped
via `pytest.importorskip`, so you can develop and test most changes on any machine.

## Before you open a PR

Run the same checks CI runs ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)):

```bash
cd jarvis_v1
ruff check .            # lint — BLOCKING in CI, must pass
pytest tests/ -v        # 118 tests
mypy .                  # type check (advisory)
```

- **Lint is a hard gate.** `ruff` config lives in `pyproject.toml`; fix findings
  rather than adding blanket ignores.
- **Add tests** for new behavior. Put them in `tests/` (not the deprecated
  root-level `test_*.py` scripts). Prefer tests that run without GPU/Ollama.
- **Keep secrets and personal data out of commits.** The user's Obsidian vault
  (`vault/`), local models, and `data/` runtime artifacts are git-ignored by
  design — don't force-add them.

## Conventions

- **Python 3.10+**, type hints on public functions.
- **Style:** match the surrounding code. Compact one-liners (`if cond: return x`)
  are a deliberate, ruff-permitted style here.
- **Security-sensitive code** (`action/agent_executor.py`): any change to the
  shell allowlist, filesystem sandbox, or confirmation flow must come with a
  corresponding test in `tests/test_security.py`. These are fail-closed
  boundaries — keep them that way.
- **Architecture:** prefer adding to V3 (`core_v3/`). Shared primitives go in
  `core_common/` so V2 and V3 stay decoupled. `legacy/` is frozen — don't build
  on it.
- **Commits:** present-tense, explain *why* in the body when it isn't obvious.

## Project layout

A module-by-module map is in
[`jarvis_v1/PROJECT_STRUCTURE.md`](jarvis_v1/PROJECT_STRUCTURE.md); the design
rationale (with diagrams) is in
[`jarvis_v1/docs/JARVIS_Design_Document_v0.1.md`](jarvis_v1/docs/JARVIS_Design_Document_v0.1.md).

## Reporting issues

When filing a bug, include: OS, Python version, whether Ollama is running and
which models are pulled, the launch command (`run.ps1` flags / `JARVIS_MODE`),
and the relevant log lines. For voice issues, note your mic/speaker setup and
the mode (`wake` / `voice` / `text`).

## License

By contributing you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
