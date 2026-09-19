# Repository Guidelines

## Project Structure & Module Organization

Core Python code lives under `src/omarchy_ai/`. Keep features within the existing domains: `voice/` for realtime audio, `execution/` for desktop actions, `display/` for casting, `phone/` for the mobile bridge, and `core/` for daemon state. Tests are in `tests/` and generally mirror behavior rather than package paths. Quickshell/QML plugins live in `quickshell/plugins/`; the Kotlin Android receiver is in `android-receiver/`. Operational assets and documentation belong in `systemd/`, `wake_models/`, `scripts/`, and `docs/`. Read `STATUS.md` and `docs/ADR-0001-architecture.md` before changing architecture or hardware integrations.

## Build, Test, and Development Commands

- `bash install.sh` installs native dependencies, creates the project environment, and configures the user service.
- `uv sync --locked` synchronizes Python dependencies from `uv.lock` (the installed environment requires the setup script's system-site-packages configuration).
- `.venv/bin/python -m unittest discover -s tests` runs the Python test suite.
- `node --test tests/phone_text.test.cjs` runs the phone-page JavaScript tests.
- `uv build` creates Python distributions.
- `cd android-receiver && mise exec -- ./gradlew :app:assembleDebug` builds the debug APK with the pinned JDK/SDK environment.
- `bash scripts/build-install-package.sh` creates a release bundle; use a clean, committed checkout.

## Coding Style & Naming Conventions

Use four-space indentation and standard Python conventions: `snake_case` for functions/modules, `PascalCase` for classes, and uppercase constants. Preserve type hints where present and favor small, testable helpers around system boundaries. Shell scripts should use strict mode (`set -euo pipefail`) and quote expansions. Follow existing QML and Kotlin formatting in adjacent files. No repository-wide formatter or linter is configured, so keep diffs focused and stylistically consistent.

## Testing Guidelines

Python tests use `unittest`, including `unittest.mock`; name files `test_*.py` and methods `test_<behavior>`. Add regression coverage for fixes, especially subprocess, configuration, and session-state changes. Mock external services and desktop commands; document any validation requiring real PipeWire, Hyprland, phone, or TV hardware.

## Commit & Pull Request Guidelines

Recent commits use short imperative subjects such as `Fix Gateway key save on clean installs`; release commits use `Publish <version> ...`. Keep each commit scoped to one coherent change. Pull requests should explain behavior and risks, list test commands and results, link relevant issues or ADRs, and include screenshots or recordings for QML, phone, or receiver UI changes.

## Security & Environment

Never commit API keys, pairing data, or generated user configuration. This targets Omarchy/Arch: use `pacman` or `yay`, not Debian-family package commands. Avoid restarting PipeWire or the live `omarchy-ai.service` unless the task explicitly requires it.
