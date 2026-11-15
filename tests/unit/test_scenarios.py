"""Scenario schema: the ten MVP files load and collect, and each validation rule rejects."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from obdient.errors import ScenarioError
from obdient.knowledge.obdex import ObdexIndex
from obdient.schemas.common import Condition, SnapshotSource
from obdient.schemas.report import FindingCategory
from obdient.schemas.scenario import (
    Scenario,
    load_scenario,
    load_scenarios,
    pid_of,
    resolve_scenario,
)
from obdient.schemas.snapshot import DtcStatus, UserReport, WarningLight
from obdient.vehicle.collector import collect
from obdient.vehicle.pids import DEFAULT_LIVE_PIDS
from obdient.vehicle.simulator import ScenarioSimulator
from support import SCENARIOS_DIR, Prefetched, make_scenario, scenario_dict

MVP = load_scenarios(SCENARIOS_DIR)


def write(tmp_path: Path, data: dict[str, Any], name: str | None = None) -> Path:
    path = tmp_path / f"{name or data['id']}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def rejected(tmp_path: Path, **overrides: Any) -> str:
    with pytest.raises(ScenarioError) as info:
        load_scenario(write(tmp_path, scenario_dict(**overrides)))
    return str(info.value)


# ---- the ten MVP scenarios ---------------------------------------------------


def test_there_are_ten_mvp_scenarios_numbered_s01_to_s10() -> None:
    assert [s.id[:3] for s in MVP] == [f"s{n:02d}" for n in range(1, 11)]


def test_no_held_out_scenarios_exist_yet() -> None:
    assert load_scenarios(SCENARIOS_DIR, held_out=True) == []


@pytest.mark.parametrize("scenario", MVP, ids=lambda s: s.id)
def test_every_scenario_collects_cleanly(scenario: Scenario) -> None:
    snapshot = collect(ScenarioSimulator(scenario), scenario.build_user_report())

    assert snapshot.source is SnapshotSource.SIMULATOR
    assert snapshot.read_errors == []  # no PID missing from a condition, no label contradicted
    assert set(snapshot.conditions_captured) == set(scenario.conditions)
    assert snapshot.conditions_captured[-1] is scenario.initial_condition
    assert {d.code for d in snapshot.dtcs if d.status is DtcStatus.STORED} == set(
        scenario.dtcs.stored
    )
    assert snapshot.mil_on is scenario.effective_mil_on
    assert (snapshot.freeze_frame is None) == (scenario.freeze_frame is None)
    assert snapshot.readings


@pytest.mark.parametrize("scenario", MVP, ids=lambda s: s.id)
def test_every_condition_defines_the_same_pids(scenario: Scenario) -> None:
    """Otherwise the PID missing from one condition would be a NO DATA error in that phase."""
    key_sets = {frozenset(values) for values in scenario.conditions.values()}
    assert len(key_sets) == 1


@pytest.mark.parametrize("scenario", MVP, ids=lambda s: s.id)
def test_live_freeze_frame_pids_are_also_readable_live(scenario: Scenario) -> None:
    frozen = {pid_of(k) for k in (scenario.freeze_frame or {}) if k != "trigger"}
    live = {pid_of(k) for values in scenario.conditions.values() for k in values}
    assert (frozen & set(DEFAULT_LIVE_PIDS)) <= live


def test_s01_is_the_canonical_example() -> None:
    s01 = MVP[0]
    assert s01.id == "s01_pcv_lean"
    assert s01.title == "Crankcase air leak (PCV or breather), lean at idle"
    assert s01.nhtsa.fixture == "gti_2018"
    assert s01.expected.top_finding_any == [
        FindingCategory.UNMETERED_AIR_LEAK,
        FindingCategory.PCV_FAILURE,
        FindingCategory.VACUUM_LEAK,
    ]
    assert s01.expected.must_not == ["top_is:o2_sensor_fault"]
    assert s01.conditions[Condition.HOLD_2500]["stft_b1"] == -12.0


def test_s02_is_a_car_that_has_a_maf_sensor() -> None:
    s02 = MVP[1]
    assert (s02.vehicle.year, s02.vehicle.model) == (2013, "GTI")
    assert s02.nhtsa.fixture is None and s02.nhtsa.recalls is None


def test_only_s01_reads_a_maf_on_the_2018_car() -> None:
    """The 2018 GTI has no MAF sensor; s01 keeps `maf_gs` because SPEC §11 fixes that file."""
    with_maf = [
        s.id
        for s in MVP
        if s.vehicle.year == 2018 and any("maf_gs" in v for v in s.conditions.values())
    ]
    assert with_maf == ["s01_pcv_lean"]


# ---- loading and resolving ---------------------------------------------------


def test_resolve_by_path_id_and_prefix() -> None:
    by_prefix = resolve_scenario("s01", SCENARIOS_DIR)
    assert by_prefix.id == "s01_pcv_lean"
    assert resolve_scenario("s01_pcv_lean", SCENARIOS_DIR) == by_prefix
    assert resolve_scenario(str(SCENARIOS_DIR / "s01_pcv_lean.yaml"), SCENARIOS_DIR) == by_prefix


def test_resolve_also_searches_held_out(tmp_path: Path) -> None:
    (tmp_path / "held_out").mkdir()
    write(tmp_path / "held_out", scenario_dict(id="h01_test"))
    assert resolve_scenario("h01", tmp_path).id == "h01_test"
    assert [s.id for s in load_scenarios(tmp_path, held_out=True)] == ["h01_test"]
    assert load_scenarios(tmp_path) == []


def test_resolve_errors(tmp_path: Path) -> None:
    write(tmp_path, scenario_dict(id="s01_a"))
    write(tmp_path, scenario_dict(id="s01_b"))
    with pytest.raises(ScenarioError, match="matches several scenarios: s01_a, s01_b"):
        resolve_scenario("s01", tmp_path)
    with pytest.raises(ScenarioError, match="No scenario matches 's07'"):
        resolve_scenario("s07", tmp_path)
    with pytest.raises(ScenarioError, match="cannot read file"):
        resolve_scenario(str(tmp_path / "missing.yaml"), tmp_path)


def test_error_names_the_file_and_the_field(tmp_path: Path) -> None:
    message = rejected(tmp_path, title="")
    assert message.startswith(str(tmp_path / "s99_test.yaml") + ": title: ")


def test_file_stem_must_equal_id(tmp_path: Path) -> None:
    path = write(tmp_path, scenario_dict(), name="s98_other")
    with pytest.raises(ScenarioError, match="id: 's99_test' must equal the file name 's98_other'"):
        load_scenario(path)


def test_not_yaml_and_not_a_mapping(tmp_path: Path) -> None:
    bad = tmp_path / "s99_test.yaml"
    bad.write_text("id: [unclosed", encoding="utf-8")
    with pytest.raises(ScenarioError, match="not valid YAML"):
        load_scenario(bad)
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ScenarioError, match="must contain a mapping"):
        load_scenario(bad)


# ---- validation rules --------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["S01_x", "s1_x", "x01_y", "s01-pcv", "s01_"])
def test_id_pattern(tmp_path: Path, bad_id: str) -> None:
    assert ": id: " in rejected(tmp_path, id=bad_id)


def test_source_is_required(tmp_path: Path) -> None:
    assert ": source: " in rejected(tmp_path, source=[])


def test_unknown_pid_in_conditions_lists_valid_names(tmp_path: Path) -> None:
    message = rejected(tmp_path, conditions={"idle": {"boost_psi": 3}})
    assert "conditions.idle: unknown PID 'boost_psi'" in message
    assert "ltft_b1" in message and "rpm" in message


def test_raw_pid_ids_are_accepted_if_they_have_a_decoder() -> None:
    scenario = make_scenario(conditions={"idle": {"010C": 800, "0105": 90}})
    assert set(scenario.conditions[Condition.IDLE]) == {"010C", "0105"}
    for no_decoder in ("01FF", "0145", "0101"):
        with pytest.raises(ValueError, match=f"unknown PID '{no_decoder}'"):
            make_scenario(conditions={"idle": {no_decoder: 1}})


# ---- review fixes ------------------------------------------------------------


def test_unquoted_digit_pid_keys_in_yaml_get_a_clear_error(tmp_path: Path) -> None:
    """YAML reads an unquoted 0105 as the octal integer 69; say so instead of 'PID 69'."""
    text = yaml.safe_dump(scenario_dict(freeze_frame=None, conditions={"idle": {"rpm": 800}}))
    path = tmp_path / "s99_test.yaml"
    path.write_text(text.replace("rpm: 800", "0105: 90"), encoding="utf-8")
    with pytest.raises(ScenarioError, match=r"conditions\.idle: PID key 69 is not text; quote"):
        load_scenario(path)

    path.write_text(text.replace("rpm: 800", "'0105': 90"), encoding="utf-8")
    assert load_scenario(path).conditions[Condition.IDLE] == {"0105": 90.0}


@pytest.mark.parametrize("bad", [True, "12", None, [800, "x"], [True]])
def test_condition_values_must_be_real_numbers(tmp_path: Path, bad: object) -> None:
    message = rejected(tmp_path, freeze_frame=None, conditions={"idle": {"rpm": bad}})
    assert "conditions.idle.rpm: must be a number" in message


@pytest.mark.parametrize("bad", [True, "780", [780]])
def test_freeze_frame_values_must_be_real_numbers(tmp_path: Path, bad: object) -> None:
    message = rejected(tmp_path, freeze_frame={"trigger": "P0171", "rpm": bad})
    assert "freeze_frame.rpm: must be a number" in message


def test_yaml_yes_is_not_one_rpm(tmp_path: Path) -> None:
    text = yaml.safe_dump(scenario_dict(freeze_frame=None, conditions={"idle": {"rpm": 800}}))
    path = tmp_path / "s99_test.yaml"
    path.write_text(text.replace("rpm: 800", "rpm: yes"), encoding="utf-8")
    with pytest.raises(ScenarioError, match=r"conditions\.idle\.rpm: must be a number"):
        load_scenario(path)


def test_since_clear_pids_belong_under_since_clear(tmp_path: Path) -> None:
    message = rejected(tmp_path, conditions={"idle": {"rpm": 800, "warmups_since_clear": 3}})
    assert "conditions.idle.warmups_since_clear: set this under since_clear instead" in message


def test_cold_start_and_warm_cannot_both_be_defined(tmp_path: Path) -> None:
    message = rejected(
        tmp_path,
        freeze_frame=None,
        initial_condition="warm",
        conditions={"cold_start": {"ect_c": 20}, "warm": {"ect_c": 90}},
    )
    assert "cold_start and warm cannot both be defined" in message


def test_unknown_pid_in_freeze_frame(tmp_path: Path) -> None:
    message = rejected(tmp_path, freeze_frame={"trigger": "P0171", "boost": 1})
    assert "freeze_frame: unknown PID 'boost'" in message


def test_freeze_frame_needs_a_trigger_that_is_present(tmp_path: Path) -> None:
    assert "freeze_frame: 'trigger' (a code) is required" in rejected(
        tmp_path, freeze_frame={"rpm": 800}
    )
    assert "P0420 is not in dtcs.stored or dtcs.pending" in rejected(
        tmp_path, freeze_frame={"trigger": "P0420", "rpm": 800}
    )
    # A pending code may trigger; a permanent-only one may not.
    make_scenario(freeze_frame={"trigger": "P0300", "rpm": 800})
    assert "not in dtcs.stored or dtcs.pending" in rejected(
        tmp_path,
        dtcs={"stored": [], "pending": [], "permanent": ["P0171"]},
        freeze_frame={"trigger": "P0171"},
    )


def test_freeze_frame_values_must_be_numbers(tmp_path: Path) -> None:
    assert "freeze_frame.rpm: must be a number" in rejected(
        tmp_path, freeze_frame={"trigger": "P0171", "rpm": "fast"}
    )


def test_dtc_strings_must_match_the_pattern(tmp_path: Path) -> None:
    assert ": dtcs.stored.0: " in rejected(tmp_path, dtcs={"stored": ["p0171"]})
    assert ": dtcs.permanent.0: " in rejected(tmp_path, dtcs={"permanent": ["P017"]})


def test_permanent_code_need_not_be_stored() -> None:
    scenario = make_scenario(
        dtcs={"stored": [], "pending": [], "permanent": ["P0420"]}, freeze_frame=None
    )
    assert scenario.effective_mil_on is False


def test_conditions_rules(tmp_path: Path) -> None:
    assert "'unknown' cannot be defined" in rejected(
        tmp_path, conditions={"idle": {"rpm": 800}, "unknown": {"rpm": 800}}
    )
    assert "must contain initial_condition 'idle'" in rejected(
        tmp_path, conditions={"hold_2500": {"rpm": 2500}}
    )
    assert "at least one condition is required" in rejected(tmp_path, conditions={})
    assert "conditions.idle.rpm: empty list" in rejected(tmp_path, conditions={"idle": {"rpm": []}})
    assert ": conditions.cruise" in rejected(tmp_path, conditions={"cruise": {"rpm": 2000}})


def test_readiness_names_and_states(tmp_path: Path) -> None:
    assert ": readiness.o2" in rejected(tmp_path, readiness={"o2": "complete"})
    assert ": readiness.evap: " in rejected(tmp_path, readiness={"evap": "done"})


def test_nhtsa_fixture_and_inline_are_exclusive(tmp_path: Path) -> None:
    assert "either a fixture or inline" in rejected(
        tmp_path, nhtsa={"fixture": "gti_2018", "recalls": []}
    )


def test_inline_nhtsa_defaults_matched_names_to_the_model() -> None:
    recall = {
        "campaign_number": "18V123000",
        "component": "FUEL SYSTEM, GASOLINE",
        "summary": "s",
        "consequence": "c",
        "remedy": "r",
        "report_date": None,
    }
    complaint = {
        "odi_number": "1",
        "date_filed": None,
        "date_of_incident": None,
        "components": ["ENGINE"],
        "summary": "s",
        "crash": False,
        "fire": False,
        "matched_names": ["GTI"],
    }
    scenario = make_scenario(nhtsa={"recalls": [recall], "complaints": [complaint]})
    assert scenario.nhtsa.recalls is not None and scenario.nhtsa.complaints is not None
    assert scenario.nhtsa.recalls[0].matched_names == ["Golf GTI"]
    assert scenario.nhtsa.complaints[0].matched_names == ["GTI"]


def test_expected_categories_are_checked_at_load(tmp_path: Path) -> None:
    assert ": expected.top_finding_any.0: " in rejected(
        tmp_path, expected={"top_finding_any": ["vaccum_leak"]}
    )
    assert ": expected.top_finding_any: " in rejected(tmp_path, expected={"top_finding_any": []})
    assert ": expected.escalations.0: " in rejected(
        tmp_path, expected={"top_finding_any": ["other"], "escalations": ["recall"]}
    )


@pytest.mark.parametrize(
    "entry",
    [
        "top_is:o2_sensor_fault",
        "has_category:maf_fault",
        "likelihood_high:catalyst_aged",
        "any_diagnosis",
        "driving_test",
        "escalation:stop",
        "escalation:recall",
    ],
)
def test_must_not_grammar_accepts(entry: str) -> None:
    make_scenario(expected={"top_finding_any": ["other"], "must_not": [entry]})


@pytest.mark.parametrize(
    "entry",
    [
        "top_is:o2_sensor",
        "replace_o2_sensor_as_top",
        "escalation:urgent",
        "any_diagnosis:x",
        "top_is",
        "",
    ],
)
def test_must_not_grammar_rejects(tmp_path: Path, entry: str) -> None:
    message = rejected(tmp_path, expected={"top_finding_any": ["other"], "must_not": [entry]})
    assert ": expected: " in message


def test_noise_bounds_and_bad_answer_regex(tmp_path: Path) -> None:
    assert ": noise.pct: " in rejected(tmp_path, noise={"pct": 0})
    assert ": noise.pct: " in rejected(tmp_path, noise={"pct": 5.1})
    make_scenario(noise={"pct": 5})
    assert "not a valid regex" in rejected(
        tmp_path, answers={"ask_user": {"rules": [{"match": "(", "answer": "x"}]}}
    )


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    assert ": escalation: " in rejected(tmp_path, escalation="none")


# ---- user report shortcut ----------------------------------------------------


def test_flashing_mil_shortcut() -> None:
    def lights(flashing: bool | None, mil_on: bool | None = None) -> list[WarningLight]:
        scenario = make_scenario(
            user_report={"flashing_mil": flashing, "warning_lights_seen": ["abs"]}, mil_on=mil_on
        )
        return scenario.build_user_report().warning_lights_seen

    assert lights(True) == [WarningLight.ABS, WarningLight.FLASHING_MIL]
    assert lights(False) == [WarningLight.ABS, WarningLight.STEADY_MIL]  # a code is stored
    assert lights(False, mil_on=False) == [WarningLight.ABS]
    assert lights(None) == [WarningLight.ABS]


def test_user_report_carries_the_other_fields() -> None:
    scenario = make_scenario(
        user_report={
            "symptoms_text": "whistle",
            "started_when": "last week",
            "conditions": ["cold"],
            "checklist": {"fuel_smell": False},
            "odometer_km": 98000,
        }
    )
    report = scenario.build_user_report()
    assert isinstance(report, UserReport)
    assert (report.symptoms_text, report.started_when, report.odometer_km) == (
        "whistle",
        "last week",
        98000,
    )
    assert report.conditions == ["cold"] and report.checklist.fuel_smell is False


# ---- escalations: the rules, run offline, must give the expected set ---------


@pytest.mark.parametrize("scenario", MVP, ids=lambda s: s.id)
def test_rule_computed_escalations_equal_expected(scenario: Scenario, obdex: ObdexIndex) -> None:
    """The PRD's "escalations fire 100 %" criterion, without a model (SPEC §11)."""
    escalations = Prefetched(scenario, obdex).escalations()
    levels = {e.level.value for e in escalations} - {"recall"}  # RECALL is never graded
    assert levels == set(scenario.expected.escalations), [e.rule_id for e in escalations]


def test_which_rules_fire_on_which_scenario(obdex: ObdexIndex) -> None:
    fired = {
        s.id[:3]: [
            e.rule_id for e in Prefetched(s, obdex).escalations() if e.level.value != "recall"
        ]
        for s in MVP
    }
    assert fired == {
        "s01": [],
        "s02": [],
        "s03": ["P2_misfire"],
        "s04": [],
        "s05": [],
        "s06": [],
        "s07": [],
        "s08": [],
        "s09": [],
        "s10": ["S1_coolant_overtemp", "S2_p0217"],
    }


def test_limp_mode_is_a_label_on_these_scenario_codes(obdex: ObdexIndex) -> None:
    """X13: the flag shows up next to the code; it changes no expected escalation."""
    from obdient.agent.safety import LIMP_MODE_LABEL, definition_labels

    labelled = {
        code
        for scenario in MVP
        for code in {*scenario.dtcs.stored, *scenario.dtcs.pending, *scenario.dtcs.permanent}
        if definition_labels(obdex.lookup_dtc(code)) == [LIMP_MODE_LABEL]
    }
    assert labelled == {"P0101", "P0217", "P0299", "P0300", "P0302"}


def test_no_scenario_user_text_trips_a_keyword_by_accident() -> None:
    from obdient.agent.safety import match_user_text, user_texts

    assert {s.id: match_user_text(user_texts(s.build_user_report())) for s in MVP} == {
        s.id: {} for s in MVP
    }


def test_fixture_scenarios_see_three_recalls_and_s02_sees_none(obdex: ObdexIndex) -> None:
    for scenario in MVP:
        result = Prefetched(scenario, obdex).nhtsa
        assert len(result.recalls) == (3 if scenario.nhtsa.fixture else 0), scenario.id
        assert result.errors == []
