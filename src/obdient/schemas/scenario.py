"""Scenario files: one simulated car plus the answer the graders expect (SPEC §11).

This is the one schema module that imports from `vehicle`: validation needs the PID table,
and `vehicle/pids.py` imports nothing from the package (PLAN placement decision 1).
"""

import re
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import yaml
from pydantic import Field, StringConstraints, ValidationError, model_validator

from obdient.errors import ScenarioError
from obdient.schemas.common import DTC_PATTERN, Condition, DtcCode, Model
from obdient.schemas.nhtsa import Complaint, Recall
from obdient.schemas.report import EscalationLevel, FindingCategory
from obdient.schemas.snapshot import (
    MonitorName,
    SafetyChecklist,
    SinceClear,
    UserCondition,
    UserReport,
    WarningLight,
)
from obdient.vehicle.pids import NAME_TO_PID, PID_TABLE, SINCE_CLEAR_PIDS

SCENARIO_ID_PATTERN = r"^[sh]\d{2}_[a-z0-9_]+$"
HELD_OUT_DIR = "held_out"
_MAX_PROBLEMS = 5

MonitorState = Literal["complete", "incomplete", "unsupported"]
GradedLevel = Literal["stop", "prompt", "out_of_scope"]  # RECALL is never graded

# `must_not` grammar (SPEC §11): entries with an argument, and entries without one.
_MUST_NOT_CATEGORY = ("top_is", "has_category", "likelihood_high")
_MUST_NOT_BARE = ("any_diagnosis", "driving_test")


def pid_of(key: str) -> str | None:
    """A scenario PID key (friendly name or raw `01XX`) as a PID identifier, or None.

    Only PIDs with a decoder count: a value for a PID we cannot decode has no unit, and
    `0101` belongs to `readiness` / `mil_on`.
    """
    if key in NAME_TO_PID:
        return NAME_TO_PID[key]
    return key if key in PID_TABLE and PID_TABLE[key].decode is not None else None


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _check_raw_values(section: str, values: object, allow_lists: bool) -> None:
    """Reject what Pydantic would silently coerce: YAML `yes` as 1.0, "12" as 12.0, and
    unquoted all-digit PID keys, which YAML reads as (octal) integers."""
    if not isinstance(values, dict):
        return
    for key, value in cast(dict[object, object], values).items():
        if not isinstance(key, str):
            raise ValueError(
                f'{section}: PID key {key!r} is not text; quote raw PID ids, e.g. "0105"'
            )
        if key == "trigger" and not allow_lists:
            continue
        items = cast(list[object], value) if allow_lists and isinstance(value, list) else [value]
        if not all(_is_number(item) for item in items):
            raise ValueError(f"{section}.{key}: must be a number")


def _unknown_pid(key: str) -> str:
    return f"unknown PID {key!r}; valid names: {', '.join(sorted(NAME_TO_PID))}, or their 01XX ids"


def _check_must_not(entry: str) -> str:
    if entry in _MUST_NOT_BARE:
        return entry
    kind, sep, argument = entry.partition(":")
    if sep and kind in _MUST_NOT_CATEGORY:
        if argument not in {c.value for c in FindingCategory}:
            raise ValueError(f"{entry!r}: {argument!r} is not a finding category")
        return entry
    if sep and kind == "escalation":
        if argument not in {level.value for level in EscalationLevel}:
            raise ValueError(f"{entry!r}: {argument!r} is not an escalation level")
        return entry
    raise ValueError(
        f"{entry!r} is not a must_not entry; use top_is:<category>, has_category:<category>, "
        "likelihood_high:<category>, any_diagnosis, driving_test or escalation:<level>"
    )


class ScenarioVehicle(Model):
    vin: str  # synthetic; any string
    make: str
    model: str
    year: int
    engine: str | None = None
    displacement_l: float | None = None


class ScenarioNhtsa(Model):
    """A fixture directory, or inline lists, or neither (zero recalls and complaints)."""

    fixture: str | None = None  # directory name under tests/fixtures/nhtsa/
    recalls: list[Recall] | None = None
    complaints: list[Complaint] | None = None

    @model_validator(mode="after")
    def _one_source(self) -> "ScenarioNhtsa":
        if self.fixture is not None and (self.recalls is not None or self.complaints is not None):
            raise ValueError("give either a fixture or inline recalls/complaints, not both")
        return self


class ScenarioDtcs(Model):
    stored: list[DtcCode] = []
    pending: list[DtcCode] = []
    permanent: list[DtcCode] = []  # need not also be in `stored`


class ScenarioNoise(Model):
    seed: int = 0
    pct: Annotated[float, Field(gt=0, le=5)]


class AnswerRule(Model):
    match: str  # regex, case-insensitive, searched in the question
    answer: str

    @model_validator(mode="after")
    def _compiles(self) -> "AnswerRule":
        try:
            re.compile(self.match)
        except re.error as exc:
            raise ValueError(f"match is not a valid regex: {exc}") from exc
        return self


class AskUserAnswers(Model):
    default: str = "unknown"
    rules: list[AnswerRule] = []  # first match wins


class ScenarioAnswers(Model):
    ask_user: Literal["default_unknown"] | AskUserAnswers = "default_unknown"
    actions: Literal["confirm_all", "decline_all"] = "confirm_all"  # v0.2


class ScenarioUserReport(Model):
    """`UserReport` plus one shortcut, `flashing_mil`."""

    symptoms_text: Annotated[str, Field(max_length=2000)] = ""
    started_when: Annotated[str, Field(max_length=200)] | None = None
    conditions: list[UserCondition] = []
    warning_lights_seen: list[WarningLight] = []
    flashing_mil: bool | None = None
    checklist: SafetyChecklist = SafetyChecklist()
    odometer_km: int | None = None

    def to_user_report(self, mil_on: bool) -> UserReport:
        """`flashing_mil: true` adds flashing_mil; `false` with the MIL on adds steady_mil."""
        lights = list(self.warning_lights_seen)
        if self.flashing_mil is True:
            extra = WarningLight.FLASHING_MIL
        elif self.flashing_mil is False and mil_on:
            extra = WarningLight.STEADY_MIL
        else:
            extra = None
        if extra is not None and extra not in lights:
            lights.append(extra)
        return UserReport(
            symptoms_text=self.symptoms_text,
            started_when=self.started_when,
            conditions=self.conditions,
            warning_lights_seen=lights,
            checklist=self.checklist,
            odometer_km=self.odometer_km,
        )


class Expected(Model):
    top_finding_any: Annotated[list[FindingCategory], Field(min_length=1)]
    escalations: list[GradedLevel] = []  # the exact set
    must_not: list[str] = []
    notes: str | None = None  # for the human labeller; not graded

    @model_validator(mode="after")
    def _grammar(self) -> "Expected":
        for entry in self.must_not:
            _check_must_not(entry)
        return self


class Scenario(Model):
    id: Annotated[str, StringConstraints(pattern=SCENARIO_ID_PATTERN)]
    title: Annotated[str, StringConstraints(min_length=1)]
    source: Annotated[list[str], Field(min_length=1)]  # real-world basis (PRD R2)
    vehicle: ScenarioVehicle
    nhtsa: ScenarioNhtsa = ScenarioNhtsa()
    mil_on: bool | None = None  # default: any stored code
    dtcs: ScenarioDtcs = ScenarioDtcs()
    freeze_frame: dict[str, str | float] | None = None  # "trigger": code; other keys: PIDs
    readiness: dict[MonitorName, MonitorState] = {}
    since_clear: SinceClear = SinceClear()
    initial_condition: Condition = Condition.IDLE
    conditions: dict[Condition, dict[str, float | list[float]]]
    adapter_voltage: float = 14.1
    noise: ScenarioNoise | None = None
    user_report: ScenarioUserReport = ScenarioUserReport()
    answers: ScenarioAnswers = ScenarioAnswers()
    expected: Expected

    @model_validator(mode="before")
    @classmethod
    def _default_matched_names(cls, data: Any) -> Any:
        """Inline recalls and complaints may omit matched_names; it becomes [vehicle.model]."""
        if not isinstance(data, dict):
            return data
        raw = cast(dict[str, Any], data)
        _check_raw_values("freeze_frame", raw.get("freeze_frame"), allow_lists=False)
        conditions = raw.get("conditions")
        if isinstance(conditions, dict):
            for name, values in cast(dict[object, object], conditions).items():
                _check_raw_values(f"conditions.{name}", values, allow_lists=True)
        vehicle, nhtsa = raw.get("vehicle"), raw.get("nhtsa")
        if not isinstance(vehicle, dict) or not isinstance(nhtsa, dict):
            return raw
        model_name = cast(dict[str, Any], vehicle).get("model")
        if not isinstance(model_name, str):
            return raw
        filled: dict[str, Any] = dict(cast(dict[str, Any], nhtsa))
        for field in ("recalls", "complaints"):
            items = filled.get(field)
            if isinstance(items, list):
                filled[field] = [
                    {"matched_names": [model_name], **cast(dict[str, Any], item)}
                    if isinstance(item, dict)
                    else item
                    for item in cast(list[Any], items)
                ]
        return {**raw, "nhtsa": filled}

    @model_validator(mode="after")
    def _consistent(self) -> "Scenario":
        if not self.conditions:
            raise ValueError("conditions: at least one condition is required")
        if Condition.UNKNOWN in self.conditions:
            raise ValueError("conditions: 'unknown' cannot be defined")
        if Condition.COLD_START in self.conditions and Condition.WARM in self.conditions:
            raise ValueError("conditions: cold_start and warm cannot both be defined")
        if self.initial_condition not in self.conditions:
            raise ValueError(
                f"conditions: must contain initial_condition {self.initial_condition.value!r}"
            )
        for condition, values in self.conditions.items():
            for key, value in values.items():
                pid = pid_of(key)
                if pid is None:
                    raise ValueError(f"conditions.{condition.value}: {_unknown_pid(key)}")
                if pid in SINCE_CLEAR_PIDS:
                    raise ValueError(
                        f"conditions.{condition.value}.{key}: set this under since_clear instead"
                    )
                if isinstance(value, list) and not value:
                    raise ValueError(f"conditions.{condition.value}.{key}: empty list")

        if self.freeze_frame is not None:
            trigger = self.freeze_frame.get("trigger")
            if not isinstance(trigger, str):
                raise ValueError("freeze_frame: 'trigger' (a code) is required")
            if not re.match(DTC_PATTERN, trigger):
                raise ValueError(f"freeze_frame.trigger: {trigger!r} is not a trouble code")
            if trigger not in self.dtcs.stored and trigger not in self.dtcs.pending:
                raise ValueError(
                    f"freeze_frame.trigger: {trigger} is not in dtcs.stored or dtcs.pending"
                )
            for key in self.freeze_frame:
                if key == "trigger":
                    continue
                if pid_of(key) is None:
                    raise ValueError(f"freeze_frame: {_unknown_pid(key)}")
        return self

    @property
    def effective_mil_on(self) -> bool:
        return self.mil_on if self.mil_on is not None else bool(self.dtcs.stored)

    def build_user_report(self) -> UserReport:
        return self.user_report.to_user_report(self.effective_mil_on)


def _problem(exc: ValidationError) -> str:
    """Every validation error as "<field>: <problem>", joined with "; " (at most 5 shown)."""
    problems: list[str] = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"])
        message = error["msg"].removeprefix("Value error, ")
        # Our own cross-field checks have no location and already start with "<field>: ".
        problems.append(f"{field}: {message}" if field else message)
    shown = problems[:_MAX_PROBLEMS]
    if len(problems) > len(shown):
        shown.append(f"and {len(problems) - len(shown)} more")
    return "; ".join(shown)


def load_scenario(path: Path) -> Scenario:
    """Raises ScenarioError("<file>: <field>: <problem>")."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScenarioError(f"{path}: cannot read file: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ScenarioError(f"{path}: the file must contain a mapping")
    try:
        scenario = Scenario.model_validate(data)
    except ValidationError as exc:
        raise ScenarioError(f"{path}: {_problem(exc)}") from exc
    if scenario.id != path.stem:
        raise ScenarioError(f"{path}: id: {scenario.id!r} must equal the file name {path.stem!r}")
    return scenario


def _files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.yaml")) if directory.is_dir() else []


def load_scenarios(directory: Path, held_out: bool = False) -> list[Scenario]:
    """The MVP set in `directory`, or the held-out set in `directory/held_out`."""
    return [load_scenario(p) for p in _files(directory / HELD_OUT_DIR if held_out else directory)]


def resolve_scenario(ref: str, scenarios_dir: Path) -> Scenario:
    """`ref` is a path, a full id, or a unique id prefix such as "s01"."""
    as_path = Path(ref)
    if as_path.suffix == ".yaml" or as_path.is_file():
        return load_scenario(as_path)
    candidates = _files(scenarios_dir) + _files(scenarios_dir / HELD_OUT_DIR)
    exact = [p for p in candidates if p.stem == ref]
    matches = exact or [p for p in candidates if p.stem.startswith(ref)]
    if not matches:
        raise ScenarioError(f"No scenario matches {ref!r} in {scenarios_dir}")
    if len(matches) > 1:
        names = ", ".join(p.stem for p in matches)
        raise ScenarioError(f"{ref!r} matches several scenarios: {names}")
    return load_scenario(matches[0])
