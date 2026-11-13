# OBDient — Implementation Plan (M1–M12)

Sources: [docs/PRD.md](docs/PRD.md) and [SPEC.md](SPEC.md). SPEC wins where they differ. `§` means a SPEC section; `V#` means an item in SPEC §16.

## Overview

| # | Milestone | Tier | OpenAI credits | Adapter (OBDLink CX) | Status |
| --- | --- | --- | --- | --- | --- |
| M1 | Repo skeleton | v0.1 | none | no | done |
| M2 | Vehicle layer | v0.1 | none | no | not started |
| M3 | Simulator + scenarios | v0.1 | none | no | not started |
| M4 | Knowledge layer | v0.1 | none | no | not started |
| M5 | Baseline | v0.1 | **spends** (a few single calls) | no | not started |
| M6 | Verifier + evals | v0.1 | **spends** (baseline eval, 30 runs) | no | not started |
| M7 | Agent loop | v0.1 | **spends** (heaviest: prompt iteration + 60-run eval) | no | not started |
| — | Buy the OBDLink CX | | | | |
| M8 | Real car | v0.1 | **spends** (a few real-car runs) | **yes** | not started |
| M9 | Modes | v0.2 | **spends** | no | not started |
| M10 | Closed loop | v0.2 | **spends** | **partly** (simulator first, then the parked GTI) | not started |
| M11 | MCP server | v0.2 | **spends** (only the `diagnose` tool) | no | not started |
| M12 | Judge + costs | v0.2 | **spends** | no | not started |

Status values: `not started`, `in progress`, `done`. Update the table and the milestone's own status line in the commit that changes it.

## How every milestone runs

1. Fresh session, plan mode. Read SPEC.md and this file, propose the milestone's steps.
2. Write tests first where the behaviour is fixed (allowlist, parsers, verifier, resolver, tool errors).
3. Implement. Run the verification command until green: `uv run ruff check . && uv run pyright && uv run pytest`
4. A read-only reviewer subagent checks the diff against SPEC.md.
5. Commit, set the status here to `done`, add surprises to the CLAUDE.md gotchas.

Default `pytest` is offline: no network, no model, no hardware. Markers `emulator`, `network` and `llm` are opt-in.

## Spending rule (M5 onward)

SPEC X10 removed every dollar cap from the code. The OpenAI dashboard hard limit is the only guard, and credit is about $7. A full `eval --strategy both --k 3` is 60 runs, which costs $15 if each run hits the PRD's $0.25 ceiling.

- Before any full eval: run one scenario with `--k 1`, read `total_usd` from the summary, multiply by the planned run count, and stop if the product exceeds the remaining credit.
- Iterate prompts on `--role dev` (Luna). Use `--role diagnostician` only for recorded numbers.
- Paid commands (`uv run obdient eval`, `uv run pytest -m llm`) are already on the `ask` list in `.claude/settings.json`.

## Prerequisites not yet in the repo

These are M0 leftovers and setup-checklist items. Each blocks the milestone named.

| Item | Blocks |
| --- | --- |
| `ELM327-emulator` exact version to pin; raw-TCP spike with `DTC_STORED = ['0171']` (V3) | M1 (pin), M2 (tests) |
| OpenAI model IDs, prices including cached input, API surface (V1) | M1 (config defaults), M5 |
| NHTSA products endpoint behaviour (V2); `nhtsa_products_enabled` stays `False` until answered | M4 |
| Real-world sources for scenarios 2, 3, 5; the VW warranty bulletin for scenario 4 (V11) | M3 |
| NHTSA spike responses in `tests/fixtures/nhtsa/gti_2018/`, VIN replaced | M4 |
| `data/obdex/` (commit `9ba1ffa`) and the Wal33D VW subset (commit `04c43d7`), each with its LICENSE | M4 |
| `OPENAI_API_KEY` in `.env`; hard spend limit set in the dashboard | M5 |

## Placement decisions (not stated in PRD or SPEC)

1. `schemas/scenario.py` is built in M3, not M1: its validation needs `PID_TABLE` from M2.
2. `Recall` and `Complaint` live in `schemas/nhtsa.py` and are re-exported by `knowledge/nhtsa.py`. `Scenario` references them, and §1 forbids `schemas` importing `knowledge`. **SPEC §5.4 needs a one-line note.**
3. `storage/db.py` and `userio.py` are built in M3: `obdient scan` stores snapshots and runs intake.
4. `tracing.py`, `runner.py`, and the `BudgetState` / `ToolContext` classes of `agent/tools.py` are built in M5: `run_baseline` and `build_context` take them. M6 adds transcripts and eval logs; M7 adds the tool specs and handlers.
5. The verifier arrives in M6 (PRD order). In M5 the runner validates the draft with Pydantic only; M6 replaces that with `verify_draft` / `verify_report`.
6. `vehicle/replay.py` and the recorder are built in M8 part A, before the adapter is plugged in, and unit-tested against the simulator.
7. Held-out scenarios H1, H2, H3, H5 are written at the end of M7, after prompts are frozen. H4 comes from the M8 recording.
8. `SerialTransport` is not planned (PRD: optional, low priority).
9. Runtime dependencies are added by the milestone that first imports them.

---

## M1 — Repo skeleton

**Status:** done, except `.env.example`: the deny rule `Read(./.env.*)` blocks Claude from creating it, so Mahir adds it by hand (`OPENAI_API_KEY=`, `OBDIENT_HOME=`) · **Credits:** none · **Adapter:** no

**Creates**
- `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `.env.example`, `CLAUDE.md`
- `src/obdient/`: `__init__.py`, `config.py`, `errors.py`, `cli.py` (app, `--home`, `-v`, `--version` only)
- `src/obdient/schemas/`: `__init__.py`, `common.py`, `snapshot.py`, `evidence.py`, `report.py`, `run.py`
- `tests/conftest.py`, `tests/unit/test_schemas.py`, `tests/unit/test_config.py`, `tests/unit/test_cli.py`
- Edits `.claude/settings.json`: add deny rules for `**/secrets*` and writes to `.env.*` (the rest exist)

**Steps**
1. `uv init` with Python 3.12 and a `src` layout. Configure ruff, pyright and pytest; register the three markers and exclude them by default. Pin `ELM327-emulator` as a dev dependency. Add the `obdient` entry point.
2. `errors.py` per §1.1.
3. `schemas/common.py` (§3.0, §0.4), then `snapshot.py` with `VehicleProfile` (§3.1–3.2), `evidence.py` (§3.3), `report.py` (§3.4), `run.py` (§3.5).
4. `config.py` per §2: `Settings`, `Budgets`, `ModelRole`, `ModelPrice`, `load_settings`, the two commit constants.
5. `cli.py` skeleton with the global options and exit codes of §13.
6. `.gitignore` (`.env`, `*.db`, `evals/transcripts/`, `tests/fixtures/recordings/raw/`), `.env.example` (`OPENAI_API_KEY=`, `OBDIENT_HOME=`; no `OBDIENT_MAX_USD`, per X10), `CLAUDE.md` from the PRD starter with the dollar-budget line removed.

**Tests**
- `test_schemas.py`: models are frozen and reject extra fields; JSON round-trip with `Z` datetimes; DTC, PID, ECU and evidence-ID patterns; `ReportDraft` length limits; `Run.steps` discriminates `llm` / `tool`.
- `test_config.py`: `OBDIENT_HOME` overrides `home`; subdirectories are created; a missing API key is not an error at load; a configured model missing from `prices` is.
- `test_cli.py`: `--version` exits 0.

**Done when:** the verification command is green and `uv run obdient --version` prints a version.

---

## M2 — Vehicle layer

**Status:** not started · **Credits:** none · **Adapter:** no

**Creates**
- `src/obdient/vehicle/`: `__init__.py`, `base.py`, `allowlist.py`, `pids.py`, `parsers.py`, `elm327.py`
- `src/obdient/vehicle/transports/`: `__init__.py` (`Transport` protocol), `tcp.py`
- `tests/unit/test_allowlist.py`, `test_parsers.py`, `test_pids.py`
- `tests/integration/test_emulator.py` (marker `emulator`)
- `tests/fixtures/elm_responses/` (hand-written byte strings)

**Steps**
1. Write `test_allowlist.py` from the full §4.2 list, then `allowlist.py`: normalization, the AT/ST set, the six mode regexes, classification order, guarded `AllowedCommand`, the refusal message and the `WARNING` log.
2. `pids.py`: the 30 decoders of §4.5, the length-only PIDs checked against J1979 (V5), `DEFAULT_LIVE_PIDS`.
3. `parsers.py`: support bitmaps, PID 0101 status, DTC pairs and payloads, VIN, freeze-frame value, `split_mode01`.
4. `elm327.py` reply handling: `clean_lines`, `parse_reply` (headers on and off), `reassemble` (ISO-TP, per ECU), error tokens, negative responses.
5. `vehicle/base.py` (`Vehicle` protocol) and `transports/tcp.py`.
6. `ELM327Vehicle`: the 13-step `connect()` of §4.4, request planning, engine-ECU preference, one retry on timeout, `drain_errors`.
7. `test_emulator.py`: call every `Vehicle` method over TCP in collector order with `delay 0`; inject `NO DATA`, delays and `SEARCHING...`; assert `cmd_dtc_cleared` is false and `DTC_STORED` is unchanged.
8. Record in SPEC §16 whether V4 (AT/ST list) changed.

**Tests**
- `test_allowlist.py`: every §4.2 case, the one-byte property test, the construction guard, the source-scan test.
- `test_parsers.py`: each decoder; multi-frame VIN; multi-ECU; headers on/off; spaces on/off; all line endings; each error token; out-of-order ISO-TP.
- `test_pids.py`: every §4.5 row against a hand-computed J1979 example.
- `uv run pytest -m emulator`: the integration test above.

**Done when:** every `Vehicle` method returns decoded data from the emulator over TCP, every §4.2 refusal is tested, and `cmd_dtc_cleared` is still false after a full read.

---

## M3 — Simulator + scenarios

**Status:** not started · **Credits:** none · **Adapter:** no

**Creates**
- `src/obdient/schemas/scenario.py`, `src/obdient/schemas/nhtsa.py` (decision 2)
- `src/obdient/vehicle/simulator.py`, `src/obdient/vehicle/collector.py`
- `src/obdient/storage/__init__.py`, `src/obdient/storage/db.py`, `src/obdient/userio.py`
- `obdient scan` in `cli.py`
- `scenarios/s01_pcv_lean.yaml` … `s10_*.yaml` (ten files; only the `s01` name is fixed by SPEC)
- `tests/unit/test_simulator.py`, `test_collector.py`, `test_scenarios.py`, `test_db.py`; additions to `test_cli.py`

**Steps**
1. Resolve V11 first: find a TSB or documented case for scenarios 2, 3 and 5 and open the warranty bulletin for scenario 4. Mahir approves the sources. A scenario without a source is not written.
2. `schemas/scenario.py` per §11: models, validation rules with `ScenarioError("<file>: <field>: <problem>")`, the `must_not` grammar, `load_scenario`, `load_scenarios`, `resolve_scenario`.
3. `ScenarioSimulator` per §4.6: deterministic timestamps, condition switching, list-valued PIDs, seeded noise.
4. `collect()` per §4.8: call order, multi-condition capture with `initial_condition` last, VIN rules, the sanity-check table, `close()` in `finally`.
5. `Database` per §10.1 (all six tables now; it also implements `NhtsaCache` for M4).
6. `userio.py` per §8.2 and §4.8: `run_intake`, `CliIO`, `NonInteractiveIO`, `ScenarioIO`, `CliConditionPrompter` with the fixed prompt texts.
7. `obdient scan` per §13.1 for `simulator` and `emulator` sources.
8. Write the ten scenarios from the PRD table. `expected.escalations` is provisional until M4 runs the rules.
9. Change `test_emulator.py` to run a full `collect()`.

**Tests**
- `test_simulator.py`: two runs give identical Snapshot JSON; condition switching; list-valued PIDs wrap; noise is reproducible by seed.
- `test_collector.py`: call order; multi-condition capture; every sanity-check row; `close()` runs on error.
- `test_scenarios.py`: every scenario loads; each validation rule rejects a bad file.
- `test_db.py`: snapshot insert-only; prefix lookup; `latest_snapshot`; cache get / put / stale.
- `test_cli.py`: `scan` flag validation and exit codes 2 and 3.

**Done when:** `uv run obdient scan --scenario s01` stores and prints a Snapshot, and all ten scenarios load and collect.

---

## M4 — Knowledge layer

**Status:** not started · **Credits:** none · **Adapter:** no

**Creates**
- `src/obdient/knowledge/`: `__init__.py`, `obdex.py`, `wal33d.py`, `nhtsa.py`, `evidence.py`
- `src/obdient/agent/__init__.py`, `src/obdient/agent/safety.py`
- `data/wal33d/vw.json` (built once from the pinned subset)
- `obdient nhtsa` in `cli.py`
- `tests/unit/test_obdex.py`, `test_wal33d.py`, `test_nhtsa.py`, `test_evidence.py`, `test_safety.py`
- `tests/integration/test_nhtsa_live.py` (marker `network`)

**Steps**
1. Inspect the pinned OBDex YAML. Write the field mapping into the `obdex.py` docstring and seed `MANUFACTURER_DOMAINS` from the hosts present (V6). Mahir reviews the domain list.
2. `build_index`, `ensure_index`, `ObdexIndex`: fail loudly unless 9,533 codes load; tier rule computed at build.
3. `wal33d.py`: VW-only, P1 only, year caution, never for codes OBDex defines.
4. Confirm NHTSA JSON field names against the fixtures (V7). `NhtsaClient`: URL allowlist, cache keys and TTLs, delay, retries, stale fallback, `offline`. `FixtureCache`.
5. `NhtsaResolver`: `FAMILY_PREFIXES`, union, de-duplication, `matched_names`, per-query errors.
6. `EvidenceStore` with payload-rule validation and dedupe keys; `ComplaintCorpus.search` (the §7.5.5 ranking); `prefetch` in the fixed 11-step order; `add_live_readings`; `derive`; `derive_live`.
7. `safety.py`: fixed strings (§3.4), thresholds, the 13 rules of §6.2, user-text triggers, keyword-quoting reasons, `merge`, `BANNED_PHRASES`, `find_banned`.
8. Extend `test_scenarios.py`: run prefetch + `evaluate` offline for every scenario and assert the set equals `expected.escalations`. Resolve V9: if `P1_limp_mode_code` fires on s01, s06 or s07, narrow the rule and record it in SPEC §16.
9. `obdient nhtsa` per §13.5.
10. Record the V8 threshold check in SPEC §16.

**Tests**
- `test_obdex.py`: `stats().codes == 9533`; P0171 has 5 causes, top cause an air leak after the MAF, tier 1; P0420 tier per rule; a wrong code count raises.
- `test_wal33d.py`: VW only; `OTHER` absent; caution for 2005 and later and for unknown year.
- `test_nhtsa.py`: the permanent GTI regression (candidates `["Golf GTI", "GTI"]`, 3 recalls all matched under `GTI`, complaints merged from both names); cache TTLs; stale fallback; offline miss; URL allowlist.
- `test_evidence.py`: prefetch order and stable IDs; a bare number in a payload raises; every derived metric firing and not firing.
- `test_safety.py`: every rule fires and does not over-fire; "no smoke" still fires and quotes the match; S5 + O1 together; `merge` never removes; every banned regex with a hit and a near miss.
- `test_scenarios.py`: offline escalations equal `expected.escalations` for all ten.

**Done when:** the index holds 9,533 codes, the GTI fixture resolves to 3 recalls, and every scenario's rule-computed escalations equal its expected set.

---

## M5 — Baseline

**Status:** not started · **Credits: spends** (a few single calls) · **Adapter:** no

**Creates**
- `src/obdient/agent/`: `llm.py`, `context.py`, `baseline.py`, `fallback.py`, `tools.py` (`BudgetState` and `ToolContext` only)
- `src/obdient/agent/prompts/`: `system.md`, `mode_diagnose.md`, `baseline_note.md`
- `src/obdient/report/`: `__init__.py`, `render.py`, `templates/report.md.j2`, `templates/report.html.j2`
- `src/obdient/runner.py`, `src/obdient/tracing.py`
- `obdient diagnose` and `obdient report` in `cli.py`
- `tests/unit/test_llm.py`, `test_context.py`, `test_fallback.py`, `test_render.py`, `test_runner.py`
- `tests/integration/test_baseline_smoke.py` (marker `llm`)

**Steps**
1. Check the official OpenAI docs: API surface, strict structured-output rules (V14), whether reasoning tokens count against the output cap (V13), model IDs and prices (V1). Update `config.py` and SPEC §16.
2. `llm.py`: `OpenAiClient` (the only module importing `openai`), `FakeLlmClient`, `cost_usd`, `strict_schema`, retries, the unknown-model error.
3. Prompt files per §7.2, with `prompt_hash`.
4. `build_context` per §7.3. The VIN never appears.
5. `assemble_report` per §7.9, with and without a draft.
6. `run_baseline` per §7.7. Until M6 a draft is accepted when it validates as `ReportDraft`; otherwise the fallback report ships.
7. `render.py` and both templates per §9: section order, autoescape, self-contained HTML.
8. `Tracer` (§10.2) and `runner.diagnose` (§8.1). The Run is saved in a `finally` block.
9. `obdient diagnose` (§13.2, `--strategy baseline` as the default until M7) and `obdient report` (§13.3).
10. One paid run with `--role dev`, then one with `--role diagnostician`. Note the cost of each.

**Tests**
- `test_llm.py`: `cost_usd` arithmetic; `strict_schema(ReportDraft)` has every property required, no `default`, `additionalProperties: false` at every level.
- `test_context.py`: exact block structure; VIN absent; `<` and `>` escaped in user text.
- `test_fallback.py`: draft and no-draft assembly; fixed `next_steps`; `incomplete_reason` per stop reason.
- `test_render.py`: escalations precede findings; an incomplete report has no findings section; a zero-recall report shows the names tried and the lookup link and never says "no recalls".
- `test_runner.py`: with `FakeLlmClient`, a full run stores a report and a Run; a crash still saves a Run with `stop_reason=error`.
- `uv run pytest -m llm`: one scenario end to end on the dev model.

**Done when:** `uv run obdient diagnose --scenario s01 --strategy baseline` writes Markdown and HTML with escalations first and stores a Run with its USD cost.

---

## M6 — Verifier + evals

**Status:** not started · **Credits: spends** (baseline eval: 10 scenarios × 3 trials) · **Adapter:** no

**Creates**
- `src/obdient/agent/verifier.py`
- `evals/__init__.py`, `evals/harness.py`, `evals/graders.py`, `evals/results/` (JSONL and summaries are committed; transcripts are not)
- `obdient eval` in `cli.py`
- `tests/unit/test_verifier.py`, `test_graders.py`, `test_harness.py`

**Steps**
1. Write `test_verifier.py`: every violation code with a passing and a failing draft, plus the tokenizer edge cases.
2. `extract_numbers` and `evidence_numbers` per §7.8.3.
3. `verify_draft` (checks 1–5 and 7) and `verify_report` (checks 1–8), with the exact messages of §7.8.
4. Wire in: baseline rejections lead to the fallback report; any `verify_report` violation raises `InternalReportError`.
5. `graders.py`: the five graders of §12.
6. `harness.py`: per-trial simulator seed, `ScenarioIO`, fixture-backed NHTSA, JSONL appended per trial, `EvalSummary`, transcripts.
7. `obdient eval` per §13.4, including `--label`.
8. Apply the spending rule, then run `uv run obdient eval --strategy baseline --k 3`.
9. Mahir labels each report with `--label`. Read at least 3 failing transcripts. Record the baseline numbers in this file.

**Tests**
- `test_verifier.py`: as step 1; ranges, signs, thousands separators, identifiers (`B1S2`, `EA888`), foreign units, model year.
- `test_fallback.py` (extended): the fallback report returns `[]` from `verify_report` for every stop reason.
- `test_graders.py`: each grader passing and failing; `RECALL` ignored; an incomplete report passes `must_not`.
- `test_harness.py`: with `FakeLlmClient`, rows append per trial and summary rates are computed correctly.

**Done when:** baseline results for 10 scenarios × 3 trials are in `evals/results/` and every stored report returns `[]` from `verify_report`.

---

## M7 — Agent loop

**Status:** not started · **Credits: spends** (heaviest milestone) · **Adapter:** no

**Creates**
- `src/obdient/agent/tools.py` (specs, six handlers, `dispatch`, `render_tool_result`), `src/obdient/agent/loop.py`
- `tests/unit/test_tools.py`, `test_loop.py`; `tests/integration/test_agent_smoke.py` (marker `llm`)
- `scenarios/held_out/h01_*.yaml`, `h02_*.yaml`, `h03_*.yaml`, `h05_*.yaml`
- `README.md` (rewritten), demo GIF

**Steps**
1. Write `test_tools.py`: every error message of §7.4 and §7.5 verbatim.
2. The six tools per §7.5, each with a Pydantic argument model and `strict_schema` parameters. Validation errors consume no budget. Frame escaping per §7.4.
3. `run_agent` per §7.6: turn counting, the no-tool-call nudge, `submit_report` alone in its turn, one revision, the two-turns-left warning.
4. Re-evaluate escalations after every `ask_user` and `read_live_data` (X8); merge only adds.
5. Runner and CLI: `--strategy agent` becomes the default.
6. Smoke test on the dev model.
7. Iterate prompts on `--role dev`. Read at least 3 failing transcripts before each prompt change.
8. Apply the spending rule, then run `uv run obdient eval --strategy both --k 3`. The gate is §12: higher `pass_all_k_rate`, or equal and higher `trial_pass_rate`. If the agent does not beat the baseline, baseline stays the default and the result is recorded here.
9. Freeze prompts. Write H1, H2, H3 and H5. Run `--held-out` once.
10. Check the v0.1 success-criteria table in the PRD against the eval summary.
11. Mahir: rewrite the README, record the GIF, make the repo public, update the resume.

**Tests**
- `test_tools.py`: as step 1; strict-schema shape of every tool; budgets untouched by validation errors; frame escaping.
- `test_loop.py` (`FakeLlmClient`): accepted first try; one revision then accepted; revision exhausted gives the fallback; max turns; an unparsable `submit_report` does not consume the revision; cost summed into `Run.total_usd`; the nudge; `submit_report` mixed with other calls.
- `uv run pytest -m llm`: one scenario through the full loop.
- `uv run obdient eval --strategy both --k 3` and `--held-out`.

**Done when:** the both-strategy eval is recorded, the keep-or-drop decision is written here, the v0.1 success criteria hold for the kept strategy, and the repo is public.

---

## M8 — Real car

**Status:** not started · **Credits: spends** (a few real-car runs) · **Adapter: yes** (part B)

**Creates**
- `src/obdient/vehicle/replay.py`, `src/obdient/vehicle/transports/ble.py`, `src/obdient/vehicle/transports/recording.py`
- `scan` flags `--source ble`, `--ble-name`, `--replay`, `--record`, `--record-raw`
- `tests/unit/test_replay.py`, `test_ble.py`; `tests/integration/test_replay.py`
- `tests/fixtures/recordings/` (redacted sessions), new files in `tests/fixtures/elm_responses/`
- `scenarios/held_out/h04_*.yaml`, demo video

**Steps — part A, no adapter**
1. `RecordingVehicle`, `ReplayVehicle`, `redact_session` per §4.7; `RecordingTransport`.
2. `BleTransport` per §4.3 against a fake `bleak` client: discovery by name prefix, notify subscription, chunked writes, a private event-loop thread.
3. Wire the CLI flags.

**Steps — part B, at the car**
4. Follow the PRD hardware-day checklist.
5. Fill in V10 (service and characteristic UUIDs, chunk size) and V4 (any CX-specific AT/ST command, each with an allowlist line and a test).
6. `uv run obdient scan --source ble --record --record-raw` with ignition on, engine off. Repeat with the engine idling and warm, then with `--condition idle --condition hold_2500`.
7. Fix parsers from the raw log. Each fix adds a fixture to `tests/fixtures/elm_responses/`.
8. Redact the sessions and commit them to `tests/fixtures/recordings/`. Raw sessions stay in `~/.obdient/sessions/`.
9. Update `DEFAULT_LIVE_PIDS` from the GTI's supported bitmap.
10. `uv run obdient diagnose --source ble`. Build H4 from the recording. Report simulator and real-recording pass rates separately.
11. Record the demo video.

**Tests**
- `test_replay.py` (unit): record a simulator session, replay it, get an identical Snapshot; `REPLAY_MISS`; `redact_session` removes the VIN as text and as hex.
- `test_ble.py`: chunking and prompt detection against the fake client.
- `tests/integration/test_replay.py`: each committed GTI session replays to the same Snapshot.
- A grep test: the real VIN appears nowhere under `tests/fixtures/`.

**Done when:** one full scan and report on the GTI is recorded, and its redacted session replays to the same Snapshot in the default test run.

---

## v0.2 milestones (M9–M12)

SPEC §15 gives v0.2 only as reserved names. **Step 1 of each milestone below is a SPEC addendum**, written and reviewed before any code. File names marked *(proposed)* are settled there.

## M9 — Modes

**Status:** not started · **Credits: spends** · **Adapter:** no

**Creates**
- `src/obdient/agent/prompts/mode_used_car.md`, `mode_handoff.md`
- `src/obdient/report/templates/` used-car and handoff templates
- `src/obdient/smog.py`, `src/obdient/diff.py` *(proposed)*
- `diagnose --mode used_car|handoff`; `obdient smog`, `obdient diff` *(proposed)*
- New used-car scenarios; `tests/unit/test_smog.py`, `test_diff.py`, `test_modes.py`

**Steps**
1. SPEC addendum: mode outputs, the seller question sheet, California readiness rules with their source, diff semantics.
2. Smog readiness predictor: deterministic, from `Monitor` and `SinceClear`.
3. Scan diff: two snapshots for one VIN (codes added and cleared, readiness changes, trim changes).
4. Used-car mode: prompt, template, `recently_cleared` surfaced first.
5. Handoff sheet: one-page template (codes, freeze frame, ruled out, questions).
6. Extend verifier check 8 and the renderer tests for the new templates.
7. Run the eval on the used-car scenarios. Check the PRD's revisit trigger: if the agent confuses mode instructions, record it.

**Tests**
- `test_smog.py`: each readiness rule passing and failing.
- `test_diff.py`: added and cleared codes; unchanged snapshots give an empty diff.
- `test_modes.py` and `test_render.py`: each mode renders its required sections; the v0.1 diagnose output is unchanged.
- `uv run obdient eval` on the used-car scenarios.

**Done when:** scenario 8 produces a used-car report that flags the recent clear, and `handoff`, `smog` and `diff` each produce verified output from stored snapshots.

---

## M10 — Closed loop

**Status:** not started · **Credits: spends** · **Adapter: partly** (simulator first, then the parked GTI)

**Creates**
- `request_user_action` in `agent/tools.py`; `UserIO.confirm_action` in `userio.py`
- Changes to `loop.py`, `context.py` (actions budget), `system.md`, `simulator.py`, `collector.py`, `evals/harness.py`
- Closed-loop scenarios; additions to `test_tools.py`, `test_loop.py`, `test_simulator.py`

**Steps**
1. SPEC addendum: tool schema, the six allowed actions, instruction texts, how a confirmed action sets the condition that `read_live_data` reads.
2. A way to scan only the initial condition. In v0.1 the collector captures every simulator condition up front (I13), which leaves the agent nothing to ask for.
3. The tool: fixed action list, both error messages of §15, `max_user_actions`, the decline path.
4. Every instruction carries "Car parked, in neutral, parking brake set." and the engine-bay warning.
5. `ScenarioIO.confirm_action` from `answers.actions`; the simulator switches condition on confirmation.
6. Eval: closed loop against the M7 agent on scenarios where idle alone is ambiguous (s01 against s02).
7. With the adapter: one parked-test run on the GTI, recorded.

**Tests**
- `test_tools.py`: disallowed action and limit messages verbatim; the safety prefix on every instruction; a declined action changes nothing.
- `test_loop.py`: at most 3 actions; readings after a confirmed action carry the new condition.
- `must_not: driving_test` on every closed-loop scenario.
- The recorded GTI session replays.

**Done when:** on the simulator the agent requests `hold_2500_rpm_30s` and uses the result to separate scenario 1 from scenario 2, and one parked-test run on the GTI is recorded.

---

## M11 — MCP server

**Status:** not started · **Credits: spends** (only when `diagnose` is called) · **Adapter:** no

**Creates**
- `src/obdient/mcp_server.py`; the `mcp` dependency
- `tests/unit/test_mcp_server.py`; `docs/mcp.md` (Claude Desktop setup)

**Steps**
1. SPEC addendum: tool schemas, which `Vehicle` source the server uses, how `ask_user` behaves without a terminal.
2. Expose exactly five tools over `runner.diagnose`, `collect` and `Database`: `scan`, `read_dtcs`, `read_pids`, `diagnose`, `get_report`.
3. The VIN is masked in every tool result.
4. Claude Desktop config and a recorded demo against the simulator.

**Tests**
- `test_mcp_server.py` (in-memory client, `FakeLlmClient`): the tool list is exactly the five names; `scan` and `get_report` round-trip against the simulator; no result contains the VIN.
- The allowlist source-scan test still passes.

**Done when:** Claude Desktop answers "what's wrong with my car?" against a scenario using only the five read-only tools.

---

## M12 — Judge + costs

**Status:** not started · **Credits: spends** · **Adapter:** no

**Creates**
- `src/obdient/agent/judge.py`; a judge prompt file
- LLM re-ranking behind `ComplaintCorpus.search`
- `CostEstimate` in `schemas/report.py`; repair-estimate loading in `knowledge/obdex.py`
- PDF export *(proposed: `report/pdf.py`)*; `report show --format pdf`
- `tests/unit/test_judge.py`, `test_costs.py`, `test_pdf.py`

**Steps**
1. SPEC addendum: `JudgeVerdict`, the agreement threshold, the cost model and labor-rate input, the PDF library. It must also settle the PRD's "cited web sources" for costs against the no-web-fetch non-goal.
2. Collect at least 20 manually labelled reports (`eval --label`, started in M6).
3. Judge: binary pass / fail on faithfulness and usefulness. Measure agreement with the labels; the judge enters the workflow only if it meets the threshold.
4. Complaint ranking: filter by component, LLM re-rank, top 3–5. The keyword ranking stays as the offline fallback.
5. Costs: load OBDex repair estimates (index rebuild), convert euros to USD, label "rough estimate" with the source, add the label to verifier check 8.
6. PDF export from the self-contained HTML.

**Tests**
- `test_judge.py`: verdict parsing with `FakeLlmClient`; the agreement computation.
- `test_costs.py`: conversion and labelling; a report with costs but no label fails check 8.
- `test_evidence.py` (extended): re-ranking falls back to keyword order on `LlmError`.
- `test_pdf.py`: a report exports to a non-empty PDF.
- A paid run: judge against the 20 labelled reports.

**Done when:** the judge meets the agreed agreement rate on at least 20 labelled reports, and a report shows ranked complaints, a labelled rough cost range, and exports to PDF.
