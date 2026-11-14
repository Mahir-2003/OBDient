"""Talking to the user: intake questions, condition prompts, agent questions (SPEC §8.2, §4.8)."""

import re
from collections.abc import Callable
from typing import Protocol, get_args

from obdient.schemas.common import Condition
from obdient.schemas.scenario import AskUserAnswers, Scenario
from obdient.schemas.snapshot import (
    SafetyChecklist,
    UserCondition,
    UserReport,
    WarningLight,
)

UNKNOWN_ANSWER = "unknown"

# Intake questions, in the order they are asked.
Q_SYMPTOMS = "Describe what you've noticed (Enter to skip):"
Q_STARTED = "When did it start? (Enter to skip):"
Q_CONDITIONS = "When does it happen? [cold/hot/load/idle, comma-separated, Enter to skip]:"
Q_FLASHING = "Is the check-engine light flashing? [y/n]:"
Q_FUEL = "Do you smell fuel? [y/n]:"
Q_SMOKE = "Have you seen smoke or steam? [y/n]:"
Q_BRAKES = "Any problem with the brakes? [y/n]:"
Q_STEERING = "Any problem with the steering? [y/n]:"
Q_OIL = "Is the oil pressure warning light on? [y/n]:"
Q_ABS_AIRBAG = "Is an ABS or airbag warning light on? [abs/airbag/both/n]:"
Q_ODOMETER = "Odometer in km (Enter to skip):"

# Condition prompts (fixed texts; every one carries the parked-car safety line).
_SAFETY = "Car parked, in neutral, parking brake set."
_ENGINE_BAY = "Don't reach into the engine bay while the engine is running."
PROMPT_IDLE = (
    f"{_SAFETY} Let the engine idle with no pedal input. {_ENGINE_BAY} "
    "Press Enter when ready, or type 'skip'."
)
PROMPT_HOLD_2500 = (
    f"{_SAFETY} Hold the engine at about 2,500 rpm and keep it there until told to release "
    f"(about 30 seconds). {_ENGINE_BAY} Press Enter when holding, or type 'skip'."
)
RELEASE = "Release the pedal."

MAX_SYMPTOMS = 2000
_MAX_STARTED = 200
_USER_CONDITIONS: tuple[str, ...] = get_args(UserCondition)


class UserIO(Protocol):
    def ask(self, question: str) -> str:
        """Answer one question from the agent's ask_user tool."""
        ...


class CliIO:
    """Prints the question and reads one line."""

    def __init__(
        self, read: Callable[[str], str] = input, write: Callable[[str], None] = print
    ) -> None:
        self._read = read
        self._write = write

    def ask(self, question: str) -> str:
        self._write(f"OBDient asks: {question}")
        return self._read("> ").strip() or UNKNOWN_ANSWER


class NonInteractiveIO:
    def ask(self, question: str) -> str:
        return UNKNOWN_ANSWER


class ScenarioIO:
    """Answers from `scenario.answers.ask_user`: the first rule whose regex matches wins."""

    def __init__(self, scenario: Scenario) -> None:
        answers = scenario.answers.ask_user
        self._answers = answers if isinstance(answers, AskUserAnswers) else AskUserAnswers()

    def ask(self, question: str) -> str:
        for rule in self._answers.rules:
            if re.search(rule.match, question, re.IGNORECASE):
                return rule.answer
        return self._answers.default


def _yes_no(answer: str) -> bool | None:
    """`y` / `n`; anything else, including Enter, means "don't know"."""
    text = answer.strip().lower()
    if text in ("y", "yes"):
        return True
    if text in ("n", "no"):
        return False
    return None


def run_intake(
    prompt: Callable[[str], str], mil_on: bool | None, symptoms: str | None = None
) -> UserReport:
    """Ask the intake questions in their fixed order.

    `symptoms`, when given, becomes `symptoms_text` and question 1 is not asked.
    The flashing-light question is skipped when the car says its MIL is off.
    """
    if symptoms is None:
        symptoms = prompt(Q_SYMPTOMS)
    started = prompt(Q_STARTED).strip()
    tokens = [t.strip().lower() for t in prompt(Q_CONDITIONS).split(",")]
    conditions: list[UserCondition] = [c for c in _USER_CONDITIONS if c in tokens]  # pyright: ignore[reportAssignmentType]

    lights: list[WarningLight] = []
    if mil_on is not False:
        flashing = _yes_no(prompt(Q_FLASHING))
        if flashing is True:
            lights.append(WarningLight.FLASHING_MIL)
        elif flashing is False and mil_on is True:
            lights.append(WarningLight.STEADY_MIL)

    checklist = SafetyChecklist(
        fuel_smell=_yes_no(prompt(Q_FUEL)),
        smoke_or_steam=_yes_no(prompt(Q_SMOKE)),
        brake_problem=_yes_no(prompt(Q_BRAKES)),
        steering_problem=_yes_no(prompt(Q_STEERING)),
    )
    if _yes_no(prompt(Q_OIL)) is True:
        lights.append(WarningLight.OIL_PRESSURE)
    abs_airbag = prompt(Q_ABS_AIRBAG).strip().lower()
    if abs_airbag in ("abs", "both"):
        lights.append(WarningLight.ABS)
    if abs_airbag in ("airbag", "both"):
        lights.append(WarningLight.AIRBAG)

    digits = re.sub(r"[,\s]", "", prompt(Q_ODOMETER))
    return UserReport(
        symptoms_text=symptoms.strip()[:MAX_SYMPTOMS],
        started_when=started[:_MAX_STARTED] or None,
        conditions=conditions,
        warning_lights_seen=lights,
        checklist=checklist,
        odometer_km=int(digits) if re.fullmatch(r"[0-9]{1,9}", digits) else None,
    )


class CliConditionPrompter:
    """Implements the collector's `ConditionPrompter` with the fixed texts of SPEC §4.8."""

    def __init__(
        self, read: Callable[[str], str] = input, write: Callable[[str], None] = print
    ) -> None:
        self._read = read
        self._write = write

    def prepare(self, condition: Condition) -> bool:
        # cold_start and warm are labels on an idle capture, so they use the idle prompt.
        text = PROMPT_HOLD_2500 if condition is Condition.HOLD_2500 else PROMPT_IDLE
        try:
            return self._read(text + " ").strip().lower() != "skip"
        except EOFError:  # no terminal to answer: treat as skipped
            return False

    def release(self, condition: Condition) -> None:
        """Called after sampling, so the user is not left holding the pedal."""
        if condition is Condition.HOLD_2500:
            self._write(RELEASE)
