"""Snapshot of one scan and the vehicle profile (SPEC §3.1, §3.2)."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from obdient.schemas.common import (
    SCHEMA_VERSION,
    Condition,
    DtcCode,
    EcuId,
    HexStr,
    Model,
    PidId,
    SnapshotSource,
    UtcDatetime,
)


class DtcStatus(StrEnum):
    STORED = "stored"
    PENDING = "pending"
    PERMANENT = "permanent"


class Dtc(Model):
    code: DtcCode
    status: DtcStatus
    ecu: EcuId
    raw: HexStr  # the 2 DTC bytes


class Reading(Model):
    pid: PidId
    name: str  # friendly name from the PID table, or the pid if unknown
    value: float | str | None  # None when the PID has no decoder; str for enumerations
    unit: str | None
    ts: UtcDatetime
    ecu: EcuId
    condition: Condition
    raw: HexStr


class FreezeFrame(Model):
    trigger_code: DtcCode
    ecu: EcuId
    readings: list[Reading]  # condition = UNKNOWN


class MonitorName(StrEnum):
    # continuous
    MISFIRE = "misfire"
    FUEL_SYSTEM = "fuel_system"
    COMPONENTS = "components"
    # non-continuous
    CATALYST = "catalyst"
    HEATED_CATALYST = "heated_catalyst"
    EVAP = "evap"
    SECONDARY_AIR = "secondary_air"
    AC_REFRIGERANT = "ac_refrigerant"
    O2_SENSOR = "o2_sensor"
    O2_HEATER = "o2_heater"
    EGR = "egr"


CONTINUOUS_MONITORS: tuple[MonitorName, ...] = (
    MonitorName.MISFIRE,
    MonitorName.FUEL_SYSTEM,
    MonitorName.COMPONENTS,
)


class Monitor(Model):
    name: MonitorName
    supported: bool
    complete: bool | None  # None when not supported


class StatusSinceClear(Model):
    """Decoded PID 0101."""

    mil_on: bool
    dtc_count: int
    monitors: list[Monitor]  # always all 11 names, in enum order


class SinceClear(Model):
    warmups: int | None = None  # PID 0130
    distance_km: float | None = None  # PID 0131
    minutes_mil_on: float | None = None  # PID 014D
    minutes_since_clear: float | None = None  # PID 014E
    distance_mil_on_km: float | None = None  # PID 0121


class ReadErrorKind(StrEnum):
    NO_DATA = "NO DATA"
    UNABLE_TO_CONNECT = "UNABLE TO CONNECT"
    CAN_ERROR = "CAN ERROR"
    BUFFER_FULL = "BUFFER FULL"
    UNKNOWN_COMMAND = "?"
    STOPPED = "STOPPED"
    ADAPTER_ERROR = "ADAPTER ERROR"
    TIMEOUT = "TIMEOUT"
    PARSE_ERROR = "PARSE ERROR"
    UNSUPPORTED = "UNSUPPORTED"
    REPLAY_MISS = "REPLAY MISS"
    CONDITION_MISMATCH = "CONDITION MISMATCH"


class ReadError(Model):
    request: str  # the command, or a method name for non-transport errors
    kind: ReadErrorKind
    detail: str | None
    ts: UtcDatetime


class AdapterInfo(Model):
    id_string: str | None = None  # ATI
    device_description: str | None = None  # AT@1
    stn_id: str | None = None  # STI, None if "?"
    stn_device: str | None = None  # STDI, None if "?"
    protocol_number: str | None = None  # ATDPN, e.g. "A6" or "6"
    voltage: float | None = None  # ATRV


class WarningLight(StrEnum):
    FLASHING_MIL = "flashing_mil"
    STEADY_MIL = "steady_mil"
    OIL_PRESSURE = "oil_pressure"
    ABS = "abs"
    AIRBAG = "airbag"
    BATTERY = "battery"
    COOLANT_TEMP = "coolant_temp"
    OTHER = "other"


class SafetyChecklist(Model):
    """Structured intake answers. None = not asked or "don't know"."""

    fuel_smell: bool | None = None
    smoke_or_steam: bool | None = None
    brake_problem: bool | None = None
    steering_problem: bool | None = None


UserCondition = Literal["cold", "hot", "load", "idle"]


class UserReport(Model):
    symptoms_text: Annotated[str, Field(max_length=2000)] = ""
    started_when: Annotated[str, Field(max_length=200)] | None = None
    conditions: list[UserCondition] = []
    warning_lights_seen: list[WarningLight] = []
    checklist: SafetyChecklist = SafetyChecklist()
    odometer_km: int | None = None


class Snapshot(Model):
    snapshot_id: str
    schema_version: int = SCHEMA_VERSION
    captured_at: UtcDatetime
    source: SnapshotSource
    adapter: AdapterInfo
    vin: str | None
    vin_source: Literal["mode09", "user", "scenario"] | None
    mil_on: bool | None
    dtc_count: int | None
    dtcs: list[Dtc]
    freeze_frame: FreezeFrame | None
    readiness: list[Monitor]
    since_clear: SinceClear
    readings: list[Reading]
    conditions_captured: list[Condition]  # in capture order; the last one is "current"
    supported_pids: list[PidId]
    ecus: list[EcuId]
    read_errors: list[ReadError]
    user_report: UserReport


class VehicleProfile(Model):
    schema_version: int = SCHEMA_VERSION
    vin: str | None
    make: str | None  # vPIC "Make", uppercased
    model: str | None  # vPIC "Model", as returned
    model_year: int | None
    engine: str | None  # human string built from vPIC engine fields when present
    displacement_l: float | None  # vPIC "DisplacementL"
    vpic_raw: dict[str, str]  # non-empty vPIC fields only
    candidate_names: list[str]  # §5.5, order = query order
    matched_names: dict[Literal["recalls", "complaints"], list[str]]
