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
- Emulator: `delay` defaults to 0, but `ATZ` and `ATDPN` each take 0.5 s and `0100` takes 1.5 s, so one `connect()` costs about 2.5 s.
- Emulator sends trouble codes without the CAN count byte (`43 01 71`); `decode_dtcs` accepts both forms (SPEC V15).
- Emulator state lives partly in module globals (`elm.obd_message.DTC_STORED`); mutate in place and restore.
- `ruff format` rewrites Python blocks inside Markdown; `*.md` is excluded in pyproject.toml. Keep it excluded.
- Wal33D lookups: VW make + code only, never OTHER.
- `schemas/__init__.py` must stay empty: `config` imports `schemas.common` and `schemas.run` imports `config`.
- `uv` here is the pip-installed wheel under pyenv's 3.12; Homebrew builds it from source on this Mac.
- The 2018 GTI has no MAF sensor (SPEC X12, V16): don't add `maf_gs` to 2018 scenarios; `s02` is a 2013 GTI for that reason.
- Scenario files: every condition must define the same PIDs, and a live-readable PID in `freeze_frame` must also be in the conditions, or the simulator records `NO DATA` for it.
- Simulator snapshots all carry `captured_at` 2026-01-01 (`SIMULATED_EPOCH`); "latest" in the database means last stored.
- Test helpers are in `tests/support.py` (`from support import ...`); pytest `pythonpath` and pyright `extraPaths` both list `tests`.
- Pydantic reports only the first branch of a failed union; `load_scenario` therefore lists several problems per file.
