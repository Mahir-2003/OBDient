"""Intake, condition prompts and the three UserIO implementations (SPEC §8.2, §4.8)."""

from collections.abc import Callable

from obdient import userio
from obdient.schemas.common import Condition
from obdient.schemas.snapshot import WarningLight
from obdient.userio import (
    CliConditionPrompter,
    CliIO,
    NonInteractiveIO,
    ScenarioIO,
    run_intake,
)
from support import make_scenario

ALL_QUESTIONS = [
    userio.Q_SYMPTOMS,
    userio.Q_STARTED,
    userio.Q_CONDITIONS,
    userio.Q_FLASHING,
    userio.Q_FUEL,
    userio.Q_SMOKE,
    userio.Q_BRAKES,
    userio.Q_STEERING,
    userio.Q_OIL,
    userio.Q_ABS_AIRBAG,
    userio.Q_ODOMETER,
]


def scripted(answers: dict[str, str]) -> tuple[Callable[[str], str], list[str]]:
    asked: list[str] = []

    def prompt(question: str) -> str:
        asked.append(question)
        return answers.get(question, "")

    return prompt, asked


def test_question_texts_are_the_spec_texts() -> None:
    assert ALL_QUESTIONS == [
        "Describe what you've noticed (Enter to skip):",
        "When did it start? (Enter to skip):",
        "When does it happen? [cold/hot/load/idle, comma-separated, Enter to skip]:",
        "Is the check-engine light flashing? [y/n]:",
        "Do you smell fuel? [y/n]:",
        "Have you seen smoke or steam? [y/n]:",
        "Any problem with the brakes? [y/n]:",
        "Any problem with the steering? [y/n]:",
        "Is the oil pressure warning light on? [y/n]:",
        "Is an ABS or airbag warning light on? [abs/airbag/both/n]:",
        "Odometer in km (Enter to skip):",
    ]


def test_all_questions_in_order_and_all_skipped_gives_an_empty_report() -> None:
    prompt, asked = scripted({})
    report = run_intake(prompt, mil_on=None)

    assert asked == ALL_QUESTIONS
    assert report.symptoms_text == "" and report.started_when is None
    assert report.conditions == [] and report.warning_lights_seen == []
    assert report.checklist.fuel_smell is None and report.checklist.brake_problem is None
    assert report.odometer_km is None


def test_full_answers() -> None:
    prompt, _ = scripted(
        {
            userio.Q_SYMPTOMS: "  whistle at idle ",
            userio.Q_STARTED: "last week",
            userio.Q_CONDITIONS: "Idle, cold , warp",
            userio.Q_FLASHING: "n",
            userio.Q_FUEL: "Y",
            userio.Q_SMOKE: "no",
            userio.Q_BRAKES: "yes",
            userio.Q_STEERING: "maybe",
            userio.Q_OIL: "y",
            userio.Q_ABS_AIRBAG: "both",
            userio.Q_ODOMETER: "98,000",
        }
    )
    report = run_intake(prompt, mil_on=True)

    assert (report.symptoms_text, report.started_when) == ("whistle at idle", "last week")
    assert report.conditions == ["cold", "idle"]
    assert report.warning_lights_seen == [
        WarningLight.STEADY_MIL,
        WarningLight.OIL_PRESSURE,
        WarningLight.ABS,
        WarningLight.AIRBAG,
    ]
    checklist = report.checklist
    assert (checklist.fuel_smell, checklist.smoke_or_steam) == (True, False)
    assert (checklist.brake_problem, checklist.steering_problem) == (True, None)
    assert report.odometer_km == 98000


def test_flashing_question_is_skipped_when_the_mil_is_off() -> None:
    prompt, asked = scripted({userio.Q_FLASHING: "y"})
    report = run_intake(prompt, mil_on=False)
    assert userio.Q_FLASHING not in asked and len(asked) == 10
    assert report.warning_lights_seen == []


def test_flashing_yes_and_steady_only_when_the_mil_is_known_on() -> None:
    def lights(answer: str, mil_on: bool | None) -> list[WarningLight]:
        prompt, _ = scripted({userio.Q_FLASHING: answer})
        return run_intake(prompt, mil_on).warning_lights_seen

    assert lights("y", True) == [WarningLight.FLASHING_MIL]
    assert lights("y", None) == [WarningLight.FLASHING_MIL]
    assert lights("n", True) == [WarningLight.STEADY_MIL]
    assert lights("n", None) == []  # we do not know the light is on at all
    assert lights("", True) == []


def test_symptoms_argument_skips_question_one() -> None:
    prompt, asked = scripted({})
    report = run_intake(prompt, mil_on=None, symptoms="rough idle")
    assert userio.Q_SYMPTOMS not in asked
    assert report.symptoms_text == "rough idle"


def test_abs_airbag_variants_and_bad_odometer() -> None:
    def report_for(answer: str, odometer: str = ""):
        prompt, _ = scripted({userio.Q_ABS_AIRBAG: answer, userio.Q_ODOMETER: odometer})
        return run_intake(prompt, mil_on=False)

    assert report_for("abs").warning_lights_seen == [WarningLight.ABS]
    assert report_for("AIRBAG").warning_lights_seen == [WarningLight.AIRBAG]
    assert report_for("n").warning_lights_seen == []
    assert report_for("n", "about 90k").odometer_km is None
    assert report_for("n", "-5").odometer_km is None
    assert report_for("n", "120 500").odometer_km == 120500
    assert report_for("n", "²").odometer_km is None  # str.isdigit() is true for this


def test_long_answers_are_cut_to_the_schema_limits() -> None:
    prompt, _ = scripted({userio.Q_SYMPTOMS: "x" * 3000, userio.Q_STARTED: "y" * 300})
    report = run_intake(prompt, mil_on=None)
    assert (len(report.symptoms_text), len(report.started_when or "")) == (2000, 200)


# ---- condition prompts -------------------------------------------------------


def test_prompt_texts_are_the_spec_texts() -> None:
    assert userio.PROMPT_IDLE == (
        "Car parked, in neutral, parking brake set. Let the engine idle with no pedal input. "
        "Don't reach into the engine bay while the engine is running. "
        "Press Enter when ready, or type 'skip'."
    )
    assert userio.PROMPT_HOLD_2500 == (
        "Car parked, in neutral, parking brake set. Hold the engine at about 2,500 rpm and "
        "keep it there until told to release (about 30 seconds). "
        "Don't reach into the engine bay while the engine is running. "
        "Press Enter when holding, or type 'skip'."
    )
    assert userio.RELEASE == "Release the pedal."


def test_condition_prompter() -> None:
    shown: list[str] = []
    written: list[str] = []
    replies = iter(["", " SKIP ", "", ""])

    def read(text: str) -> str:
        shown.append(text)
        return next(replies)

    prompter = CliConditionPrompter(read=read, write=written.append)

    assert prompter.prepare(Condition.IDLE) is True
    assert prompter.prepare(Condition.HOLD_2500) is False
    assert prompter.prepare(Condition.COLD_START) is True
    assert prompter.prepare(Condition.WARM) is True
    assert [s.strip() for s in shown] == [
        userio.PROMPT_IDLE,
        userio.PROMPT_HOLD_2500,
        userio.PROMPT_IDLE,  # cold_start and warm are labels on an idle capture
        userio.PROMPT_IDLE,
    ]
    prompter.release(Condition.IDLE)
    assert written == []

    def closed(_: str) -> str:
        raise EOFError

    assert CliConditionPrompter(read=closed).prepare(Condition.IDLE) is False  # no terminal
    prompter.release(Condition.HOLD_2500)
    assert written == ["Release the pedal."]


# ---- UserIO ------------------------------------------------------------------


def test_cli_io_prints_the_question_and_reads_a_line() -> None:
    written: list[str] = []
    io = CliIO(read=lambda _: "  about a month ", write=written.append)
    assert io.ask("When did it start?") == "about a month"
    assert written == ["OBDient asks: When did it start?"]
    assert CliIO(read=lambda _: "", write=written.append).ask("Anything?") == "unknown"


def test_non_interactive_io_always_says_unknown() -> None:
    assert NonInteractiveIO().ask("Is the light flashing?") == "unknown"


def test_scenario_io_default_unknown() -> None:
    assert ScenarioIO(make_scenario()).ask("Any smoke?") == "unknown"


def test_scenario_io_first_matching_rule_wins_case_insensitively() -> None:
    scenario = make_scenario(
        answers={
            "ask_user": {
                "default": "not sure",
                "rules": [
                    {"match": "coolant|antifreeze", "answer": "yes, topped up twice"},
                    {"match": "smell", "answer": "sweet smell"},
                ],
            }
        }
    )
    io = ScenarioIO(scenario)
    assert io.ask("Have you had to add COOLANT? Any smell?") == "yes, topped up twice"
    assert io.ask("Is there a smell?") == "sweet smell"
    assert io.ask("How old are the tyres?") == "not sure"
