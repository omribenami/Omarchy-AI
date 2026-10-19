# Repository Guidelines

## Project Structure & Module Organization

Core Python code lives under `src/omarchy_ai/`. Keep features within the existing domains: `voice/` for realtime audio, `execution/` for desktop actions, `display/` for casting, `phone/` for the mobile bridge, and `core/` for daemon state. Tests are in `tests/` and generally mirror behavior rather than package paths. Quickshell/QML plugins live in `quickshell/plugins/`; the Kotlin Android receiver is in `android-receiver/`. Operational assets and documentation belong in `systemd/`, `wake_models/`, `scripts/`, and `docs/`. Read `docs/ADR-0001-architecture.md` before changing architecture or hardware integrations.

## Build, Test, and Development Commands

- `bash install.sh` installs native dependencies, creates the project environment, and configures the user service.
- `uv sync --locked` synchronizes Python dependencies from `uv.lock` (the installed environment requires the setup script's system-site-packages configuration).
- `uv sync --frozen --group dev` installs the dev tools (pytest, ruff, mypy, coverage) that the CI gates use.
- Run the CI gates (`.github/workflows/ci.yml`) before a pull request: `uv lock --check`, `uv run --frozen ruff check`, `uv run --frozen ruff format --check`, `uv run --frozen mypy`, `CI=1 uv run --frozen pytest -q`. `CI=1` skips tests marked `live` or `hardware`; pytest also collects `evals/`.
- `.venv/bin/python -m unittest discover -s tests` still runs the Python test suite, with the same XDG isolation as pytest (`tests/_isolated_dirs/`, which discovery imports first). The per-test fixtures in `tests/conftest.py` and the `tests/no_live_desktop.py` guard apply only under pytest.
- `node --test tests/phone_text.test.cjs` runs the phone-page JavaScript tests.
- `uv run --frozen pytest tests/chaos -m chaos` runs the fault-injection tests (`core/crashpoints.py`, `tests/chaos/harness.py`, `tests/fakes/myapi_server.py`). `OMARCHY_CHAOS_RUNS=<n>` lengthens the randomized crash loop (CI runs 9).
- `uv run --frozen python -m evals compare` scores the behavior evals for the legacy prompt and prompt v2 against `evals/baselines/deterministic.json` (`--update` rewrites it). The injection suite is `evals/injection/`. See `evals/README.md`.
- `uv build` creates Python distributions.
- `cd android-receiver && mise exec -- ./gradlew :app:assembleDebug` builds the debug APK with the pinned JDK/SDK environment.
- `bash scripts/build-install-package.sh` creates a release bundle; use a clean, committed checkout.

## Coding Style & Naming Conventions

Use four-space indentation and standard Python conventions: `snake_case` for functions/modules, `PascalCase` for classes, and uppercase constants. Preserve type hints where present and favor small, testable helpers around system boundaries. Shell scripts should use strict mode (`set -euo pipefail`) and quote expansions. Follow existing QML and Kotlin formatting in adjacent files. Ruff and mypy are configured in `pyproject.toml`. `ruff format` applies to every file not listed in `[tool.ruff.format] exclude`, and mypy checks every module not in the `ignore_errors` override; both lists are existing debt, so a file you fix leaves the list in the same change. New modules must pass both. Keep diffs focused and stylistically consistent.

## Testing Guidelines

Python tests use `unittest`, including `unittest.mock`; name files `test_*.py` and methods `test_<behavior>`. Add regression coverage for fixes, especially subprocess, configuration, and session-state changes. Mock external services and desktop commands; document any validation requiring real PipeWire, Hyprland, phone, or TV hardware. Mark tests that need a real network service `live` and tests that need real hardware `hardware`.

Tests never touch the live desktop or the user's state. `tests/_isolated_dirs/` points the XDG directories at a throwaway directory before `omarchy_ai` is imported, under both pytest (`tests/conftest.py` imports it first) and `unittest discover -s tests` (discovery imports it before `chaos/` and every `test_*.py`), and fails loudly if `omarchy_ai` was imported earlier. `tests/no_live_desktop.py` raises `LiveDesktopCall` when a test starts a desktop or device binary (`hyprctl`, `omarchy*`, `wtype`, `wl-copy`, `wpctl`, `pactl`, `notify-send`, `systemctl`, `tmux`, `secret-tool`, `adb` and others) and stands in for the keyring; a fake binary inside a temp dir or the repo still runs. When a test trips the guard, mock the call at its patch target. Generate test secrets at run time; never commit a secret-looking literal.

## Commit & Pull Request Guidelines

Recent commits use short imperative subjects such as `Fix Gateway key save on clean installs`; release commits use `Publish <version> ...`. Keep each commit scoped to one coherent change. Pull requests should explain behavior and risks, list test commands and results, link relevant issues or ADRs, and include screenshots or recordings for QML, phone, or receiver UI changes.

## Security & Environment

Never commit API keys, pairing data, or generated user configuration. This targets Omarchy/Arch: use `pacman` or `yay`, not Debian-family package commands. Avoid restarting PipeWire or the live `omarchy-ai.service` unless the task explicitly requires it.
