"""ScenarioSimulator: a fake car driven by a scenario file (SPEC §4.6).

It never touches a wall clock or the network, so the same scenario and seed always give
the same readings. It is what the agent evals run against.
"""

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from obdient.errors import ScenarioError
from obdient.schemas.common import Condition, SnapshotSource
from obdient.schemas.scenario import Scenario, pid_of
from obdient.schemas.snapshot import (
    CONTINUOUS_MONITORS,
    AdapterInfo,
    Dtc,
    DtcStatus,
    FreezeFrame,
    Monitor,
    MonitorName,
    ReadError,
    ReadErrorKind,
    Reading,
    SinceClear,
    StatusSinceClear,
)
from obdient.vehicle.pids import PID_TABLE, SINCE_CLEAR_PIDS

SIMULATOR_ID = "OBDient ScenarioSimulator"
SIMULATOR_ECU = "7E8"
# Every simulated scan "happens" at this instant; readings follow at 0.5 s steps.
SIMULATED_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
STEP = timedelta(seconds=0.5)

_STATUS_PID = "0101"
_DTC_LETTERS = "PCBU"


def encode_dtc(code: str) -> str:
    """The two DTC bytes as hex: the inverse of `parsers.decode_dtc_pair`."""
    high = (_DTC_LETTERS.index(code[0]) << 6) | (int(code[1]) << 4) | int(code[2], 16)
    return f"{high:02X}{code[3:]}"


class ScenarioSimulator:
    """Implements `Vehicle` from a `Scenario`."""

    source = SnapshotSource.SIMULATOR

    def __init__(self, scenario: Scenario, seed: int | None = None) -> None:
        self.scenario = scenario
        self.condition: Condition = scenario.initial_condition
        self.captured_at = SIMULATED_EPOCH
        self.ecus: set[str] = {SIMULATOR_ECU}
        self._values: dict[Condition, dict[str, float | list[float]]] = {
            condition: {_pid(key): value for key, value in values.items()}
            for condition, values in scenario.conditions.items()
        }
        self._freeze: dict[str, float] = {
            _pid(key): float(value)
            for key, value in (scenario.freeze_frame or {}).items()
            if key != "trigger"
        }
        self._since_clear = _since_clear_by_pid(scenario.since_clear)
        self._noise = scenario.noise
        noise_seed = scenario.noise.seed if scenario.noise is not None else 0
        self._rng = random.Random(seed if seed is not None else noise_seed)
        self._reads = 0  # read_pids calls so far: the deterministic clock
        self._positions: dict[tuple[Condition, str], int] = {}  # next index of list-valued PIDs
        self._errors: list[ReadError] = []

    # ---- simulator controls --------------------------------------------------

    def set_condition(self, condition: Condition) -> None:
        if condition not in self._values:
            defined = ", ".join(c.value for c in self.available_conditions())
            raise ScenarioError(
                f"Scenario {self.scenario.id} does not define condition {condition.value!r} "
                f"(it defines: {defined})"
            )
        self.condition = condition

    def available_conditions(self) -> list[Condition]:
        return list(self._values)

    # ---- Vehicle -------------------------------------------------------------

    def connect(self) -> AdapterInfo:
        return AdapterInfo(
            id_string=SIMULATOR_ID, protocol_number="6", voltage=self.scenario.adapter_voltage
        )

    def close(self) -> None:
        return None

    def supported_pids(self) -> list[str]:
        pids = {_STATUS_PID, *self._freeze, *self._since_clear}
        for values in self._values.values():
            pids.update(values)
        return sorted(pids)

    def vin(self) -> str | None:
        return self.scenario.vehicle.vin

    def status(self) -> StatusSinceClear | None:
        return StatusSinceClear(
            mil_on=self.scenario.effective_mil_on,
            dtc_count=len(self.scenario.dtcs.stored),
            monitors=[self._monitor(name) for name in MonitorName],
        )

    def read_dtcs(self) -> list[Dtc]:
        dtcs = self.scenario.dtcs
        by_status = (
            (DtcStatus.STORED, dtcs.stored),
            (DtcStatus.PENDING, dtcs.pending),
            (DtcStatus.PERMANENT, dtcs.permanent),
        )
        return [
            Dtc(code=code, status=status, ecu=SIMULATOR_ECU, raw=encode_dtc(code))
            for status, codes in by_status
            for code in codes
        ]

    def freeze_frame(self) -> FreezeFrame | None:
        frame = self.scenario.freeze_frame
        if frame is None:
            return None
        return FreezeFrame(
            trigger_code=str(frame["trigger"]),
            ecu=SIMULATOR_ECU,
            readings=[
                self._reading(pid, value, self.captured_at, Condition.UNKNOWN)
                for pid, value in sorted(self._freeze.items())
            ],
        )

    def since_clear(self) -> SinceClear:
        return self.scenario.since_clear

    def read_pids(
        self, pids: Sequence[str], condition: Condition = Condition.UNKNOWN
    ) -> list[Reading]:
        """Values for the simulator's own condition; the `condition` argument is ignored."""
        self._reads += 1
        ts = self.captured_at + self._reads * STEP
        supported = set(self.supported_pids())
        current = self._values[self.condition]
        readings: list[Reading] = []
        for pid in pids:
            if pid not in supported:
                continue  # unsupported PIDs are never "sent"
            if pid in current:
                value = self._noisy(pid, self._next(pid, current[pid]))
                readings.append(self._reading(pid, value, ts, self.condition))
            elif pid in self._since_clear:
                readings.append(self._reading(pid, self._since_clear[pid], ts, self.condition))
            else:
                detail = f"not defined for condition {self.condition.value}"
                self._errors.append(
                    ReadError(request=pid, kind=ReadErrorKind.NO_DATA, detail=detail, ts=ts)
                )
        return readings

    def drain_errors(self) -> list[ReadError]:
        errors, self._errors = self._errors, []
        return errors

    # ---- helpers -------------------------------------------------------------

    def _monitor(self, name: MonitorName) -> Monitor:
        default = "complete" if name in CONTINUOUS_MONITORS else "unsupported"
        state = self.scenario.readiness.get(name, default)
        if state == "unsupported":
            return Monitor(name=name, supported=False, complete=None)
        return Monitor(name=name, supported=True, complete=state == "complete")

    def _next(self, pid: str, value: float | list[float]) -> float:
        if not isinstance(value, list):
            return float(value)
        key = (self.condition, pid)
        index = self._positions.get(key, 0)
        self._positions[key] = (index + 1) % len(value)
        return float(value[index])

    def _noisy(self, pid: str, value: float) -> float:
        if self._noise is not None:
            value *= 1 + self._rng.uniform(-self._noise.pct, self._noise.pct) / 100
        return round(value, PID_TABLE[pid].decimals)

    def _reading(self, pid: str, value: float, ts: datetime, condition: Condition) -> Reading:
        spec = PID_TABLE[pid]
        return Reading(
            pid=pid,
            name=spec.name,
            value=float(value),
            unit=spec.unit,
            ts=ts,
            ecu=SIMULATOR_ECU,
            condition=condition,
            raw="",
        )


def _pid(key: str) -> str:
    pid = pid_of(key)
    if pid is None:  # the Scenario validator has already rejected unknown keys
        raise ScenarioError(f"Unknown PID {key!r}")
    return pid


def _since_clear_by_pid(since: SinceClear) -> dict[str, float]:
    values: dict[str, float | None] = {
        "0121": since.distance_mil_on_km,
        "0130": since.warmups,
        "0131": since.distance_km,
        "014D": since.minutes_mil_on,
        "014E": since.minutes_since_clear,
    }
    assert set(values) == set(SINCE_CLEAR_PIDS)
    return {pid: float(value) for pid, value in values.items() if value is not None}
