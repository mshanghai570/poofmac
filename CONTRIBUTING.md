# Contributing to PoofMac

Thank you for your interest in contributing! PoofMac is an open-source
AI-powered Mac disk cleaner built in Python. Contributions of all kinds
are welcome.

## Quick start

```bash
# 1. Fork & clone
git clone https://github.com/your-username/poofmac
cd poofmac

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# 3. Install in editable mode (all dependencies included)
pip install -e ".[dev]"
# or, with uv (faster):
uv pip install -e ".[dev]"

# 4. First run — the setup wizard will ask for your API key
poofmac --chat
```

## Project layout

```
mac_cleaner/
  __init__.py   — version, metadata
  main.py       — entry point + Textual TUI
  gui.py        — PySide6 desktop GUI
  cli.py        — Rich CLI + AI chat mode
  config.py     — settings, MODEL_REGISTRY
  llm.py        — LLM agent loop (LiteLLM)
  tools.py      — tool schema + dispatcher
  scanner.py    — disk scan functions
  executor.py   — safe file deletion
  audit.py      — JSONL audit log
  safety.py     — path safety guardrails

benchmarks/
  run_benchmark.py   — Ollama model benchmark tool

assets/
  icon.png      — macOS app bundle icon (1024×1024)

.github/workflows/
  build-dmg.yml — GitHub Actions DMG builder
```

## Safety first

`safety.py` is the most critical module. It contains hard-coded lists of
paths that will **never** be touched, regardless of what the LLM says.
If you are adding new scan categories, make sure:

1. The path is not in `SYSTEM_PROTECTED` or `USER_PROTECTED`.
2. The LLM is never given a mechanism to call shell commands directly.
3. All new deletions go through `Executor.delete()` which calls
   `assert_safe()` before touching anything.

## Pull requests

- **One concern per PR** — keep changes focused.
- **Tests are required** — there is a `pytest` suite under `tests/`, and CI
  runs it on every push. See "Running the tests" below.
- **No breaking changes to safety.py without discussion** — open an issue
  first if you need to modify the protected path lists.
- Use [Conventional Commits](https://www.conventionalcommits.org/) style
  for commit messages: `feat:`, `fix:`, `docs:`, `refactor:`, `chore:`.

## Running the tests

```bash
pip install -e ".[dev]"

# The whole suite (macOS: includes the offscreen Qt tests)
pytest -q

# Everything except the Qt widget tests (works on Linux)
pytest -q -m "not gui"

# One area
pytest tests/test_safety.py -v

# With the coverage report CI enforces
pytest -q --cov=mac_cleaner --cov-report=term-missing:skip-covered
```

The suite is hermetic: it never touches your real config directory, never
installs a LaunchAgent, and every system command is stubbed, so a run cannot
delete, kill or thin anything on the machine it runs on.

`--cov-fail-under` in `.github/workflows/tests.yml` is a ratchet. It only
ever goes up; raise it in the same pull request that adds coverage.

## Running the benchmark

```bash
# Test Ollama cloud models (requires Ollama subscription)
python benchmarks/run_benchmark.py

# Fast mode (3 scenarios instead of 5)
python benchmarks/run_benchmark.py --fast

# Test local models
python benchmarks/run_benchmark.py --local
```

## Code style

- Python 3.11+, type hints everywhere.
- Imports at top of file (no inline imports except where unavoidable).
- `ruff` for linting, `black` for formatting (optional but encouraged).

---

## Publishing to PyPI

> Only for maintainers with PyPI access.

### One-time setup

```bash
# Install build tools
pip install build twine

# Create a PyPI account at https://pypi.org
# Then create an API token at https://pypi.org/manage/account/token/
```

### Release workflow

```bash
# 1. Bump version in pyproject.toml and mac_cleaner/__init__.py
#    e.g. version = "0.3.0" and __version__ = "0.3.0"

# 2. Commit and tag
git add pyproject.toml mac_cleaner/__init__.py
git commit -m "chore: bump version to 0.3.0"
git tag v0.3.0
git push && git push --tags

# 3. Build the distribution
python -m build
# Creates dist/poofmac-0.3.0.tar.gz and dist/poofmac-0.3.0-py3-none-any.whl

# 4. Upload to PyPI
twine upload dist/*
# Enter your PyPI API token when prompted
```

After upload, anyone can install with:
```bash
pip install poofmac
uv tool install poofmac
uvx poofmac --chat
```

### DMG release (macOS app bundle)

Pushing the version tag also triggers the GitHub Actions workflow
(`.github/workflows/build-dmg.yml`) which automatically builds and attaches
a signed `.dmg` to the GitHub Release — no manual steps needed.

---

## License

MIT — see [LICENSE](LICENSE).
