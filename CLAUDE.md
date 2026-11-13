# OBDient

Read-only OBD-II diagnostic agent. Python 3.12, uv, Pydantic, SQLite, OpenAI.
SPEC.md is the source of truth; PLAN.md says what each milestone builds and its status.

## Commands

- Install: `uv sync`
- Verify (run before every commit): `uv run ruff check . && uv run pyright && uv run pytest`
- Format: `uv run ruff format .`
- Emulator tests: `uv run pytest -m emulator`
- Network / model tests (cost money): `uv run pytest -m "network or llm"`
- Evals (cost money): `uv run obdient eval --k 3`

## Rules

- NEVER send OBD mode 04 or any write. All requests go through `vehicle/allowlist.py`.
- Never import or copy python-OBD (GPL) or ELM327-emulator code (CC BY-NC-SA).
- Model names only in `config.py`. Budgets only in `config.py`. There are no dollar limits in code.
- Escalations come from `agent/safety.py` rules, never from the model.
- Every report claim must cite evidence IDs; `verifier.py` enforces it.
- Tests run offline by default; mark `network` / `llm` / `emulator` tests.
- Imports point downward only (SPEC §1). `agent/*` sees the `Vehicle` protocol, nothing below it.
- Don't create or edit DEVLOG.md.

## Gotchas

- NHTSA recalls use "GTI", vPIC says "Golf GTI": always go through `NhtsaResolver`.
- OBD cannot report a flashing MIL; ask the user.
- Emulator default response delay is 0.5 s; set delay 0 in tests.
- Wal33D lookups: VW make + code only, never OTHER.
- `schemas/__init__.py` must stay empty: `config` imports `schemas.common` and `schemas.run` imports `config`.
- `uv` here is the pip-installed wheel under pyenv's 3.12; Homebrew builds it from source on this Mac.
