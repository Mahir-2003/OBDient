"""Escalation rules, keyword triggers and banned phrases (SPEC §6)."""

import re
from pathlib import Path
from typing import Any

import pytest

from obdient.agent import safety
from obdient.agent.safety import (
    BANNED_PHRASES,
    SafetyInputs,
    definition_labels,
    evaluate,
    find_banned,
    match_user_text,
    merge,
    user_texts,
)
from obdient.knowledge.obdex import ObdexIndex
from obdient.schemas.common import Condition
from obdient.schemas.nhtsa import Recall
from obdient.schemas.report import Escalation, EscalationLevel
from obdient.schemas.snapshot import Reading, UserReport
from support import NOW, Prefetched, make_scenario

CALM = {
    "dtcs": {"stored": ["P0171"]},
    "freeze_frame": None,
    "conditions": {"idle": {"rpm": 780, "ect_c": 90, "speed_kmh": 0}},
}


def run_rules(
    obdex: ObdexIndex,
    texts: list[str] | None = None,
    extra: list[list[Reading]] | None = None,
    snapshot_update: dict[str, Any] | None = None,
    recalls: list[Recall] | None = None,
    **overrides: Any,
) -> dict[str, Escalation]:
    fetched = Prefetched(make_scenario(**{**CALM, **overrides}), obdex)
    snapshot = fetched.snapshot
    if snapshot_update:
        snapshot = snapshot.model_copy(update=snapshot_update)
    inputs = SafetyInputs(
        snapshot=snapshot,
        extra_readings=extra or [],
        user_texts=texts if texts is not None else user_texts(snapshot.user_report),
        recalls=recalls if recalls is not None else [],
    )
    return {e.rule_id: e for e in evaluate(inputs, fetched.store)}


def reading(pid: str, value: float, condition: Condition = Condition.IDLE) -> Reading:
    return Reading(
        pid=pid, name=pid, value=value, unit=None, ts=NOW, ecu="7E8", condition=condition, raw=""
    )


def recall(component: str, number: str = "18V123000") -> Recall:
    return Recall(
        campaign_number=number,
        component=component,
        summary="s",
        consequence="c",
        remedy="r",
        report_date=None,
        matched_names=["GTI"],
    )


# ---- fixed strings -----------------------------------------------------------


def test_fixed_strings() -> None:
    assert safety.VIN_LOOKUP_URL == "https://www.nhtsa.gov/recalls"
    assert safety.COMPLAINT_LABEL == "Unverified owner reports"
    assert safety.WAL33D_LABEL == "Unverified community definition (Wal33D, MIT)."
    assert safety.DISCLAIMER.startswith(
        "OBDient is a read-only informational tool, not a mechanic."
    )
    assert "It does not tell you whether the vehicle can be driven." in safety.DISCLAIMER
    assert safety.RECALLS_NOTE == (
        "Recalls listed here are model-level, not specific to your VIN. Check your VIN at the "
        "NHTSA lookup linked below."
    )
    assert safety.BANNERS == {
        EscalationLevel.STOP: "Stop and have it checked before driving further.",
        EscalationLevel.PROMPT: "Get it inspected promptly.",
        EscalationLevel.OUT_OF_SCOPE: "OBDient can't read those systems; have them inspected.",
        EscalationLevel.RECALL: (
            "Open-recall check recommended: look up your VIN at nhtsa.gov/recalls."
        ),
    }
    assert (safety.COOLANT_STOP_C, safety.LOW_VOLTAGE_V, safety.ENGINE_RUNNING_RPM) == (
        115.0,
        12.0,
        400,
    )
    assert not find_banned(
        safety.DISCLAIMER + safety.RECALLS_NOTE + "".join(safety.BANNERS.values())
    )


def test_a_calm_scan_fires_nothing(obdex: ObdexIndex) -> None:
    assert run_rules(obdex) == {}


# ---- S1, S2 ------------------------------------------------------------------


def test_s1_fires_at_the_limit_and_cites_the_coolant_reading(obdex: ObdexIndex) -> None:
    hot = {"idle": {"rpm": 780, "ect_c": 115, "speed_kmh": 0}}
    (rule,) = run_rules(obdex, conditions=hot).values()
    assert (rule.rule_id, rule.level) == ("S1_coolant_overtemp", EscalationLevel.STOP)
    assert rule.banner == "Stop and have it checked before driving further."
    assert rule.reason == "Coolant temperature read 115 °C (limit 115 °C)."
    assert len(rule.evidence_ids) == 1

    assert run_rules(obdex, conditions={"idle": {"rpm": 780, "ect_c": 114, "speed_kmh": 0}}) == {}


def test_s1_ignores_the_freeze_frame_and_reads_extra_readings(obdex: ObdexIndex) -> None:
    frozen = {"trigger": "P0171", "ect_c": 130}
    assert run_rules(obdex, freeze_frame=frozen) == {}
    rules = run_rules(obdex, extra=[[reading("0105", 121.0)]])
    assert rules["S1_coolant_overtemp"].reason == "Coolant temperature read 121 °C (limit 115 °C)."


def two_phase(obdex: ObdexIndex, idle: dict[str, float], hold: dict[str, float]) -> Prefetched:
    conditions = {
        "idle": {"rpm": 780, "speed_kmh": 0, **idle},
        "hold_2500": {"rpm": 2500, "speed_kmh": 0, **hold},
    }
    return Prefetched(make_scenario(**{**CALM, "conditions": conditions}), obdex)


def test_s1_cites_only_the_phases_that_were_too_hot(obdex: ObdexIndex) -> None:
    fetched = two_phase(obdex, {"ect_c": 118}, {"ect_c": 96})
    (rule,) = fetched.escalations()
    (cited,) = [fetched.store.get(i) for i in rule.evidence_ids]
    assert cited.payload["condition"] == "idle" and cited.payload["pid"] == "0105"


def test_s2_fires_on_p0217_with_any_status(obdex: ObdexIndex) -> None:
    for dtcs in ({"stored": ["P0217"]}, {"pending": ["P0217"]}, {"permanent": ["P0217"]}):
        rules = run_rules(obdex, dtcs=dtcs)
        assert rules["S2_p0217"].reason == "Code P0217 (engine over-temperature) is present."
        assert rules["S2_p0217"].evidence_ids == ["E1"]
        assert "S1_coolant_overtemp" not in rules


# ---- user-reported rules -----------------------------------------------------


@pytest.mark.parametrize(
    ("report", "rule_id", "reason"),
    [
        ({"checklist": {"fuel_smell": True}}, "S3_fuel_smell", "You reported a fuel smell."),
        (
            {"checklist": {"smoke_or_steam": True}},
            "S4_smoke_or_steam",
            "You reported smoke or steam.",
        ),
        (
            {"warning_lights_seen": ["oil_pressure"]},
            "S7_oil_pressure_light",
            "You reported an oil pressure warning light.",
        ),
        ({"flashing_mil": True}, "S8_flashing_mil", "You reported a flashing check-engine light."),
    ],
)
def test_structured_answers_fire_with_the_fixed_reason(
    obdex: ObdexIndex, report: dict[str, Any], rule_id: str, reason: str
) -> None:
    rules = run_rules(obdex, user_report=report)
    assert list(rules) == [rule_id]
    assert (rules[rule_id].level, rules[rule_id].reason) == (EscalationLevel.STOP, reason)
    assert rules[rule_id].evidence_ids == ["E1"]  # the user report


@pytest.mark.parametrize(
    ("text", "rule_id", "reason"),
    [
        ("I can smell gas in the cabin", "S3_fuel_smell", 'You mentioned "smell gas".'),
        ("Strong fuel odour after parking", "S3_fuel_smell", 'You mentioned "fuel odour".'),
        ("White SMOKE on startup", "S4_smoke_or_steam", 'You mentioned "SMOKE".'),
        ("it was steaming", "S4_smoke_or_steam", 'You mentioned "steaming".'),
        ("the oil light flickers", "S7_oil_pressure_light", 'You mentioned "oil light".'),
        (
            "The check engine light is flashing",
            "S8_flashing_mil",
            'You mentioned "check engine light is flashing".',
        ),
        ("blinking   CEL at speed", "S8_flashing_mil", 'You mentioned "blinking CEL".'),
    ],
)
def test_keyword_only_reasons_quote_what_the_user_wrote(
    obdex: ObdexIndex, text: str, rule_id: str, reason: str
) -> None:
    rules = run_rules(obdex, user_report={"symptoms_text": text})
    assert list(rules) == [rule_id]
    assert rules[rule_id].reason == reason


def test_no_smoke_still_fires_and_the_quote_shows_why(obdex: ObdexIndex) -> None:
    rules = run_rules(obdex, user_report={"symptoms_text": "No smoke, no smell of fuel at all"})
    assert rules["S4_smoke_or_steam"].reason == 'You mentioned "smoke".'
    assert rules["S3_fuel_smell"].reason == 'You mentioned "smell of fuel".'


def test_a_false_checklist_answer_does_not_suppress_a_keyword(obdex: ObdexIndex) -> None:
    report = {"symptoms_text": "smoke from the exhaust", "checklist": {"smoke_or_steam": False}}
    assert "S4_smoke_or_steam" in run_rules(obdex, user_report=report)


def test_structured_answer_wins_over_a_keyword_for_the_reason(obdex: ObdexIndex) -> None:
    report = {"symptoms_text": "gas smell", "checklist": {"fuel_smell": True}}
    assert (
        run_rules(obdex, user_report=report)["S3_fuel_smell"].reason == "You reported a fuel smell."
    )


@pytest.mark.parametrize(
    "text",
    [
        "Sweet smell after driving, and I've topped up the coolant twice this month",
        "Temperature gauge went to the top",  # "gauge" is not "gas"
        "check engine light is on steady",
        "smells odd. Later I filled up with gas",  # different sentences
        "I smoked the tyres once",  # "smoked" is not in the pattern
        "esteem and absolute",  # no word-boundary matches for steam / abs
        "oil change light",
    ],
)
def test_near_misses_do_not_fire(obdex: ObdexIndex, text: str) -> None:
    assert run_rules(obdex, user_report={"symptoms_text": text}) == {}


def test_ask_user_answers_are_read_too(obdex: ObdexIndex) -> None:
    rules = run_rules(obdex, texts=["rough idle", "yes there is steam from the bonnet"])
    assert rules["S4_smoke_or_steam"].reason == 'You mentioned "steam".'


def test_curly_apostrophes_duplicates_and_long_matches() -> None:
    found = match_user_text(
        ["smoke and SMOKE and smoke", "more smoke, also smoking, smokes, steam"]
    )
    # distinct ignoring case, original case kept, in the order found, at most 3
    assert found == {"smoke_or_steam": ["smoke", "smoking", "smokes"]}
    long = "flashingly" + "y" * 10 + " " + "x" * 38 + " check engine"  # a 73-character match
    (quote,) = match_user_text([long])["flashing_mil"]
    assert len(quote) == 60
    assert match_user_text(['the "check engine" light blinks'])["flashing_mil"] == [
        "check engine light blinks"
    ]
    assert match_user_text([]) == {} and match_user_text(["all fine"]) == {}


def test_user_texts_collects_symptoms_start_and_answers() -> None:
    report = UserReport(symptoms_text="whistle", started_when="last week")
    assert user_texts(report, ["yes", ""]) == ["whistle", "last week", "yes"]
    assert user_texts(UserReport()) == []


# ---- S5 / S6 with O1 ---------------------------------------------------------


def test_brake_problem_gives_a_stop_and_an_out_of_scope(obdex: ObdexIndex) -> None:
    rules = run_rules(obdex, user_report={"checklist": {"brake_problem": True}})
    assert list(rules) == ["S5_brake_problem", "O1_unreadable_system"]
    assert rules["S5_brake_problem"].reason == "You reported a brake problem."
    scope = rules["O1_unreadable_system"]
    assert scope.level is EscalationLevel.OUT_OF_SCOPE
    assert scope.banner == "OBDient can't read those systems; have them inspected."
    assert scope.reason == "You reported a problem with brakes."


def test_o1_lists_every_reported_system(obdex: ObdexIndex) -> None:
    report = {
        "checklist": {"brake_problem": True, "steering_problem": True},
        "warning_lights_seen": ["abs", "airbag"],
    }
    rules = run_rules(obdex, user_report=report)
    assert list(rules) == ["S5_brake_problem", "S6_steering_problem", "O1_unreadable_system"]
    assert rules["O1_unreadable_system"].reason == (
        "You reported a problem with brakes, steering, ABS and airbags."
    )


def test_abs_or_airbag_light_alone_is_out_of_scope_without_a_stop(obdex: ObdexIndex) -> None:
    rules = run_rules(obdex, user_report={"warning_lights_seen": ["airbag"]})
    assert list(rules) == ["O1_unreadable_system"]
    assert rules["O1_unreadable_system"].reason == "You reported a problem with airbags."


def test_keyword_o1_quotes_every_match(obdex: ObdexIndex) -> None:
    rules = run_rules(
        obdex, user_report={"symptoms_text": "ABS light and the air bag light are on"}
    )
    assert list(rules) == ["O1_unreadable_system"]
    assert rules["O1_unreadable_system"].reason == 'You mentioned "ABS", "air bag".'

    both = run_rules(obdex, user_report={"symptoms_text": "Steering pulls when braking"})
    assert list(both) == ["S5_brake_problem", "S6_steering_problem", "O1_unreadable_system"]
    assert both["S6_steering_problem"].reason == 'You mentioned "Steering".'
    assert both["O1_unreadable_system"].reason == 'You mentioned "braking", "Steering".'


# ---- PROMPT rules ------------------------------------------------------------


def test_limp_mode_codes_do_not_escalate(obdex: ObdexIndex) -> None:
    """X13: OBDex's limp-mode flag is a label on the code, not a PROMPT."""
    rules = run_rules(obdex, dtcs={"stored": ["P0171", "P0299", "P0217"], "pending": ["P0101"]})
    assert "P1_limp_mode_code" not in rules
    assert {e.level for e in rules.values()} == {EscalationLevel.STOP}  # only S2 for P0217
    assert list(rules) == ["S2_p0217"]


def test_limp_mode_label_follows_the_obdex_flag(obdex: ObdexIndex) -> None:
    assert safety.LIMP_MODE_LABEL == "OBDex: can trigger reduced-power (limp) mode"
    for code in ("P0101", "P0299", "P0217", "P0300", "P0302"):
        assert definition_labels(obdex.lookup_dtc(code)) == [safety.LIMP_MODE_LABEL], code
    for code in ("P0171", "P0420", "P0456", "P0128"):
        assert definition_labels(obdex.lookup_dtc(code)) == [], code
    assert definition_labels(obdex.lookup_dtc("P0FFF")) == []  # flag unknown: no label
    assert definition_labels(None) == []  # no definition (a P1 code, for example)


def test_prompt_rules_are_only_misfire_and_low_voltage() -> None:
    source = (Path(safety.__file__)).read_text(encoding="utf-8")
    assert sorted(set(re.findall(r'"(P\d_[a-z_]+)"', source))) == [
        "P2_misfire",
        "P3_low_voltage",
    ]


@pytest.mark.parametrize(
    ("codes", "fires", "named"),
    [
        (["P0300"], True, "P0300"),
        (["P0301", "P0304"], True, "P0301, P0304"),
        (["P0300", "P0302"], True, "P0300, P0302"),
        (["P0302"], False, ""),  # one cylinder alone is not P2
        (["P0307", "P0308"], False, ""),  # outside P0301-P0306 (V12)
    ],
)
def test_p2_misfire(obdex: ObdexIndex, codes: list[str], fires: bool, named: str) -> None:
    rules = run_rules(obdex, dtcs={"stored": codes})
    assert ("P2_misfire" in rules) is fires
    if fires:
        assert rules["P2_misfire"].reason == f"Misfire code(s) {named} are present."
        assert rules["P2_misfire"].level is EscalationLevel.PROMPT


def test_p3_low_module_voltage_with_the_engine_running(obdex: ObdexIndex) -> None:
    def conditions(volts: float, rpm: float = 780) -> dict[str, Any]:
        return {"idle": {"rpm": rpm, "module_v": volts, "speed_kmh": 0}}

    rules = run_rules(obdex, conditions=conditions(11.94))
    assert list(rules) == ["P3_low_voltage"]
    assert rules["P3_low_voltage"].reason == (
        "System voltage read 11.94 V with the engine running (limit 12.0 V)."
    )
    assert run_rules(obdex, conditions=conditions(12.0)) == {}


def test_p3_engine_off_or_another_phase_does_not_count(obdex: ObdexIndex) -> None:
    off = {"idle": {"rpm": 0, "module_v": 11.5}}
    relabelled = run_rules(obdex, conditions=off)  # 0 rpm contradicts "idle" -> unknown
    assert "P3_low_voltage" not in relabelled
    # Low voltage at idle with the engine stopped there, running only in the other phase.
    split = {
        "idle": {"rpm": 450, "module_v": 13.9, "speed_kmh": 0},
        "hold_2500": {"rpm": 2500, "module_v": 14.0, "speed_kmh": 0},
    }
    assert run_rules(obdex, conditions=split) == {}


def test_p3_falls_back_to_adapter_voltage_only_when_the_pid_is_unsupported(
    obdex: ObdexIndex,
) -> None:
    rules = run_rules(obdex, adapter_voltage=11.6)
    assert rules["P3_low_voltage"].reason.startswith("System voltage read 11.60 V")
    supported = {"idle": {"rpm": 780, "module_v": 13.8, "speed_kmh": 0}}
    assert run_rules(obdex, adapter_voltage=11.6, conditions=supported) == {}
    assert run_rules(obdex, adapter_voltage=12.0) == {}


def test_p3_from_extra_readings(obdex: ObdexIndex) -> None:
    conditions = {"idle": {"rpm": 780, "module_v": 13.9, "speed_kmh": 0}}
    batch = [
        reading("010C", 2500.0, Condition.HOLD_2500),
        reading("0142", 11.2, Condition.HOLD_2500),
    ]
    assert "P3_low_voltage" in run_rules(obdex, conditions=conditions, extra=[batch])
    alone = [reading("0142", 11.2, Condition.HOLD_2500)]  # no rpm in that batch
    assert run_rules(obdex, conditions=conditions, extra=[alone]) == {}


def test_p3_does_not_mix_two_read_live_data_calls(obdex: ObdexIndex) -> None:
    """Engine running in one call, low voltage with the engine stopped in the next: no P3."""
    conditions = {"idle": {"rpm": 780, "module_v": 13.9, "speed_kmh": 0}}
    running = [reading("010C", 800.0)]
    stopped = [reading("010C", 0.0), reading("0142", 11.5)]
    assert run_rules(obdex, conditions=conditions, extra=[running, stopped]) == {}
    together = [[*running, *stopped]]
    assert "P3_low_voltage" in run_rules(obdex, conditions=conditions, extra=together)


def test_p3_never_prints_a_value_equal_to_the_limit(obdex: ObdexIndex) -> None:
    conditions = {"idle": {"rpm": 780, "module_v": 11.96, "speed_kmh": 0}}
    rule = run_rules(obdex, conditions=conditions)["P3_low_voltage"]
    assert "read 11.96 V" in rule.reason


def test_p3_cites_only_the_phase_that_triggered_it(obdex: ObdexIndex) -> None:
    fetched = two_phase(obdex, {"module_v": 11.7}, {"module_v": 13.9})
    (rule,) = fetched.escalations()
    cited = [fetched.store.get(i).payload for i in rule.evidence_ids]
    assert [(c["pid"], c["condition"]) for c in cited] == [("0142", "idle"), ("010C", "idle")]


# ---- R1 ----------------------------------------------------------------------


def test_r1_lists_safety_component_recalls_only(obdex: ObdexIndex) -> None:
    recalls = [
        recall("FUEL SYSTEM, GASOLINE", "24V110000"),
        recall("ELECTRICAL SYSTEM:IGNITION:ANTI-THEFT:CONTROL MODULE", "19V615000"),
        recall("Service Brakes, Hydraulic", "18V369000"),
    ]
    rules = run_rules(obdex, recalls=recalls)
    assert list(rules) == ["R1_safety_recall"]
    rule = rules["R1_safety_recall"]
    assert rule.level is EscalationLevel.RECALL
    assert rule.reason == (
        "2 model-level recall(s) touch FUEL SYSTEM, GASOLINE; Service Brakes, Hydraulic: "
        "24V110000, 18V369000."
    )
    assert run_rules(obdex, recalls=[recalls[1]]) == {}


@pytest.mark.parametrize(
    "component", ["STEERING:COLUMN", "AIR BAGS:FRONTAL", "POWER TRAIN", "ENGINE AND ENGINE COOLING"]
)
def test_r1_component_keywords(obdex: ObdexIndex, component: str) -> None:
    assert "R1_safety_recall" in run_rules(obdex, recalls=[recall(component)])


def test_r1_on_the_real_fixture_cites_the_recall_evidence(obdex: ObdexIndex) -> None:
    from obdient.schemas.scenario import load_scenario
    from support import SCENARIOS_DIR

    fetched = Prefetched(load_scenario(SCENARIOS_DIR / "s01_pcv_lean.yaml"), obdex)
    (rule,) = fetched.escalations()
    assert rule.rule_id == "R1_safety_recall" and rule.evidence_ids == ["E26", "E28"]
    assert [fetched.store.get(i).kind.value for i in rule.evidence_ids] == ["recall", "recall"]


# ---- ordering and merge ------------------------------------------------------


def test_order_is_stop_prompt_out_of_scope_recall_then_rule_id(obdex: ObdexIndex) -> None:
    rules = run_rules(
        obdex,
        dtcs={"stored": ["P0217", "P0300"]},
        conditions={"idle": {"rpm": 780, "ect_c": 118, "speed_kmh": 0}},
        user_report={"symptoms_text": "smoke", "checklist": {"steering_problem": True}},
        recalls=[recall("ENGINE")],
    )
    assert list(rules) == [
        "S1_coolant_overtemp",
        "S2_p0217",
        "S4_smoke_or_steam",
        "S6_steering_problem",
        "P2_misfire",
        "O1_unreadable_system",
        "R1_safety_recall",
    ]
    assert len(rules) == len(set(rules))  # at most one per rule


def test_evaluate_is_deterministic(obdex: ObdexIndex) -> None:
    kwargs: dict[str, Any] = {
        "dtcs": {"stored": ["P0300"]},
        "user_report": {"symptoms_text": "smoke"},
    }
    assert run_rules(obdex, **kwargs) == run_rules(obdex, **kwargs)


def escalation(rule_id: str, reason: str = "r") -> Escalation:
    return Escalation(
        rule_id=rule_id,
        level=EscalationLevel.STOP,
        banner=safety.BANNERS[EscalationLevel.STOP],
        reason=reason,
        evidence_ids=[],
    )


def test_merge_never_removes_or_edits() -> None:
    old = [escalation("S3_fuel_smell", "first reason"), escalation("S4_smoke_or_steam")]
    new = [escalation("S3_fuel_smell", "changed reason"), escalation("S1_coolant_overtemp")]
    merged = merge(old, new)
    assert [e.rule_id for e in merged] == [
        "S3_fuel_smell",
        "S4_smoke_or_steam",
        "S1_coolant_overtemp",
    ]
    assert merged[0].reason == "first reason"
    assert merge(old, []) == old and merge([], new) == new


# ---- banned phrases ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("It is safe to drive.", "safe to drive"),
        ("Fine to drive for now", "Fine to drive"),
        ("It's okay to drive", "okay to drive"),
        ("ok to drive", "ok to drive"),
        ("good to drive", "good to drive"),
        ("You can keep driving", "You can keep driving"),
        ("it’s fine to continue driving", "it's fine to continue driving"),
        ("Nothing to worry about", "Nothing to worry about"),
        ("no need to see a mechanic", "no need to see a mechanic"),
        ("no need to visit a mechanic", "no need to visit a mechanic"),
        ("This is guaranteed", "guaranteed"),
        ("a guarantee", "guarantee"),
        ("A new coil will fix it", "will fix"),
        ("this will definitely fix it", "will definitely fix"),
        ("It definitely is the coil", "definitely is"),
        ("definitely caused by a leak", "definitely caused"),
        ("definitely the cause", "definitely the cause"),
        ("100% certain", "100%"),
        ("100 % sure", "100 %"),
    ],
)
def test_banned_phrases_hit(text: str, hit: str) -> None:
    assert find_banned(text) == [hit]


@pytest.mark.parametrize(
    "text",
    [
        "Do not assume it is safe; have it checked before you drive.",
        "unsafe to drive",  # still contains the phrase? no: word boundary before "safe"
        "It may fix the problem",
        "most consistent with a leak",
        "Throttle at 100.0 percent",
        "guarantor",
        "You can stop driving",
        "definitely worth checking",
    ],
)
def test_banned_phrase_near_misses(text: str) -> None:
    assert find_banned(text) == []


def test_find_banned_returns_every_hit_and_the_list_has_eleven_patterns() -> None:
    assert find_banned("Safe to drive, guaranteed, 100%") == ["Safe to drive", "guaranteed", "100%"]
    assert len(BANNED_PHRASES) == 11
