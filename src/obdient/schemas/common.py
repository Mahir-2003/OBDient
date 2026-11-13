"""Model base, schema version, shared types and enums (SPEC §0.4, §3.0)."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, StringConstraints

SCHEMA_VERSION = 1

DTC_PATTERN = r"^[PBCU][0-3][0-9A-F]{3}$"
PID_PATTERN = r"^[0-9A-F]{4}$"
ECU_PATTERN = r"^[0-9A-F]{3}$"
HEX_PATTERN = r"^(?:[0-9A-F]{2})*$"
EVIDENCE_ID_PATTERN = r"^E[1-9][0-9]*$"


class Model(BaseModel):
    """Base for every schema: immutable, and unknown fields are an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


# Timezone-aware, normalized to UTC; serializes as ISO 8601 with "Z".
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]

DtcCode = Annotated[str, StringConstraints(pattern=DTC_PATTERN)]
PidId = Annotated[str, StringConstraints(pattern=PID_PATTERN)]  # mode + PID, e.g. "010C"
EcuId = Annotated[str, StringConstraints(pattern=ECU_PATTERN)]  # CAN response ID, e.g. "7E8"
HexStr = Annotated[str, StringConstraints(pattern=HEX_PATTERN)]  # uppercase, no spaces
EvidenceId = Annotated[str, StringConstraints(pattern=EVIDENCE_ID_PATTERN)]


class Quantity(Model):
    value: float
    unit: Annotated[str, StringConstraints(min_length=1)]  # canonical unit string


CANONICAL_UNITS: frozenset[str] = frozenset(
    {
        "%",
        "°C",
        "V",
        "rpm",
        "g/s",
        "kPa",
        "km",
        "km/h",
        "min",
        "s",
        "°",
        "λ",
        "L",
        "count",
        "year",
        "cylinder",
        "bank",
        "sensor",
        "ratio",
        "°C/min",  # SPEC §5.6 warmup_rate
    }
)


class Condition(StrEnum):
    IDLE = "idle"
    HOLD_2500 = "hold_2500"
    COLD_START = "cold_start"
    WARM = "warm"
    UNKNOWN = "unknown"


class SnapshotSource(StrEnum):
    SIMULATOR = "simulator"
    EMULATOR = "emulator"
    BLE = "ble"
    REPLAY = "replay"


class Tier(StrEnum):
    T1 = "1"
    T2 = "2"
    T3 = "3"
    USER = "user"
