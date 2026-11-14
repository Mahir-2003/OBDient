"""collect(): one full read of any Vehicle, turned into a Snapshot (SPEC §4.8)."""

import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from statistics import fmean
from typing import Literal, Protocol
from uuid import uuid4

from obdient.schemas.common import Condition
from obdient.schemas.snapshot import (
    Monitor,
    ReadError,
    ReadErrorKind,
    Reading,
    Snapshot,
    UserReport,
)
from obdient.vehicle.base import Vehicle
from obdient.vehicle.pids import DEFAULT_LIVE_PIDS
from obdient.vehicle.simulator import ScenarioSimulator

VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# Sanity-check limits (tunable). A phase whose mean readings contradict its label is
# relabelled `unknown`.
IDLE_RPM_MAX = 1500.0
IDLE_RPM_MIN = 400.0
HOLD_RPM_MIN = 2200.0
HOLD_RPM_MAX = 2800.0
WARM_COOLANT_MIN_C = 70.0
COLD_COOLANT_MAX_C = 50.0

_RPM, _SPEED, _COOLANT = "010C", "010D", "0105"


class ConditionPrompter(Protocol):
    def prepare(self, condition: Condition) -> bool:
        """Ask the user to put the car in this condition. False = skipped."""
        ...

    def release(self, condition: Condition) -> None:
        """Sampling of this condition is over (e.g. "Release the pedal.")."""
        ...


def collect(
    vehicle: Vehicle,
    user_report: UserReport,
    conditions: Sequence[Condition] = (Condition.UNKNOWN,),
    prompter: ConditionPrompter | None = None,
    vin_override: str | None = None,
    live_pids: Sequence[str] = DEFAULT_LIVE_PIDS,
    samples_per_condition: int = 3,
    sample_interval_s: float = 0.5,
) -> Snapshot:
    """Read everything once, in a fixed order, and always close the vehicle.

    Raises VehicleConnectionError / UnsupportedProtocolError from `connect()`, and ValueError
    for a malformed `vin_override` or for asking for both `cold_start` and `warm`.
    """
    conditions = tuple(conditions)
    if Condition.COLD_START in conditions and Condition.WARM in conditions:
        raise ValueError("cold_start and warm cannot both be captured in one scan.")
    if vin_override is not None:
        vin_override = normalize_vin(vin_override)

    simulator = vehicle if isinstance(vehicle, ScenarioSimulator) else None
    errors: list[ReadError] = []
    try:
        adapter = vehicle.connect()
        captured_at = simulator.captured_at if simulator is not None else datetime.now(UTC)
        supported = vehicle.supported_pids()
        read_vin = vehicle.vin()
        status = vehicle.status()
        dtcs = vehicle.read_dtcs()
        freeze_frame = vehicle.freeze_frame()
        since_clear = vehicle.since_clear()

        wanted = [pid for pid in live_pids if pid in set(supported)]
        readings: list[Reading] = []
        captured: list[Condition] = []
        for condition in _phases(simulator, conditions):
            prompted = (
                simulator is None and prompter is not None and condition is not Condition.UNKNOWN
            )
            if simulator is not None and condition is not Condition.UNKNOWN:
                simulator.set_condition(condition)
            if prompted and prompter is not None and not prompter.prepare(condition):
                continue  # skipped by the user: not captured
            phase: list[Reading] = []
            try:
                for sample in range(samples_per_condition):
                    if sample and simulator is None and sample_interval_s > 0:
                        time.sleep(sample_interval_s)
                    phase.extend(vehicle.read_pids(wanted, condition))
            finally:
                if prompted and prompter is not None:
                    prompter.release(condition)  # never leave the user holding the pedal
            label = condition
            problem = _contradiction(condition, phase)
            if problem is not None:
                label = Condition.UNKNOWN
                ts = phase[-1].ts if phase else captured_at
                phase = [r.model_copy(update={"condition": label}) for r in phase]
                errors.append(
                    ReadError(
                        request="collect",
                        kind=ReadErrorKind.CONDITION_MISMATCH,
                        detail=f"Declared {condition.value}, but {problem}.",
                        ts=ts,
                    )
                )
            readings.extend(phase)
            if label in captured:
                captured.remove(label)  # each label once, and the last entry is the current one
            captured.append(label)
        errors = [*vehicle.drain_errors(), *errors]
    finally:
        if simulator is not None:
            simulator.set_condition(simulator.scenario.initial_condition)
        vehicle.close()

    vin, vin_source = _vin(read_vin, vin_override, simulator is not None, errors, captured_at)
    ecus = {d.ecu for d in dtcs} | {r.ecu for r in readings} | _known_ecus(vehicle)
    if freeze_frame is not None:
        ecus.add(freeze_frame.ecu)
    readiness: list[Monitor] = status.monitors if status is not None else []
    return Snapshot(
        snapshot_id=uuid4().hex,
        captured_at=captured_at,
        source=vehicle.source,
        adapter=adapter,
        vin=vin,
        vin_source=vin_source,
        mil_on=status.mil_on if status is not None else None,
        dtc_count=status.dtc_count if status is not None else None,
        dtcs=dtcs,
        freeze_frame=freeze_frame,
        readiness=readiness,
        since_clear=since_clear,
        readings=readings,
        conditions_captured=captured,
        supported_pids=supported,
        ecus=sorted(ecus),
        read_errors=errors,
        user_report=user_report,
    )


def normalize_vin(text: str) -> str:
    """A user-typed VIN, trimmed and uppercased. Raises ValueError if it is not a VIN."""
    vin = text.strip().upper()
    if not VIN_PATTERN.match(vin):
        raise ValueError("A VIN is 17 characters: digits and capital letters except I, O, Q.")
    return vin


def _phases(
    simulator: ScenarioSimulator | None, conditions: tuple[Condition, ...]
) -> list[Condition]:
    """The conditions to capture, in order.

    A simulator asked for the default captures everything its scenario defines, with the
    initial condition last so that it is the "current" one (I13).
    """
    if simulator is None or conditions != (Condition.UNKNOWN,):
        return list(conditions)
    initial = simulator.scenario.initial_condition
    return [c for c in simulator.available_conditions() if c is not initial] + [initial]


def _mean(readings: Sequence[Reading], pid: str) -> float | None:
    values = [r.value for r in readings if r.pid == pid and isinstance(r.value, float)]
    return fmean(values) if values else None


def _contradiction(condition: Condition, readings: Sequence[Reading]) -> str | None:
    """Why the phase's mean readings contradict its label, or None. Missing data never does."""
    rpm, speed, coolant = (_mean(readings, pid) for pid in (_RPM, _SPEED, _COOLANT))
    if condition in (Condition.IDLE, Condition.HOLD_2500) and speed is not None and speed > 0:
        return f"vehicle speed averaged {speed:.0f} km/h"
    if condition is Condition.IDLE and rpm is not None and not IDLE_RPM_MIN <= rpm <= IDLE_RPM_MAX:
        return f"engine speed averaged {rpm:.0f} rpm"
    if (
        condition is Condition.HOLD_2500
        and rpm is not None
        and not HOLD_RPM_MIN <= rpm <= HOLD_RPM_MAX
    ):
        return f"engine speed averaged {rpm:.0f} rpm"
    if condition is Condition.WARM and coolant is not None and coolant < WARM_COOLANT_MIN_C:
        return f"coolant averaged {coolant:.0f} °C"
    if condition is Condition.COLD_START and coolant is not None and coolant > COLD_COOLANT_MAX_C:
        return f"coolant averaged {coolant:.0f} °C"
    return None


def _vin(
    read_vin: str | None,
    override: str | None,
    is_simulator: bool,
    errors: list[ReadError],
    ts: datetime,
) -> tuple[str | None, Literal["mode09", "user", "scenario"] | None]:
    if override is not None:
        return override, "user"
    if read_vin is None:
        return None, None
    if is_simulator:
        return read_vin, "scenario"  # scenario VINs are synthetic and exempt from the pattern
    if VIN_PATTERN.match(read_vin):
        return read_vin, "mode09"
    errors.append(
        ReadError(
            request="0902",
            kind=ReadErrorKind.PARSE_ERROR,
            detail="The vehicle returned a VIN that is not 17 valid characters; ignored.",
            ts=ts,
        )
    )
    return None, None


def _known_ecus(vehicle: Vehicle) -> set[str]:
    """ECUs the vehicle object has seen answer; not part of the protocol, so optional."""
    known: object = getattr(vehicle, "ecus", ())
    if isinstance(known, set | frozenset | list | tuple):
        return {ecu for ecu in known if isinstance(ecu, str)}  # pyright: ignore[reportUnknownVariableType]
    return set()
