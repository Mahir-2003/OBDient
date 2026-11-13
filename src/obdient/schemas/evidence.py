"""Evidence items the agent may cite (SPEC §3.3)."""

from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field

from obdient.schemas.common import EvidenceId, Model, Tier, UtcDatetime


class EvidenceKind(StrEnum):
    DTC = "dtc"  # the measured fact that a code is present (X2)
    DTC_DEFINITION = "dtc_definition"
    PID_INFO = "pid_info"
    FREEZE_FRAME = "freeze_frame"
    LIVE_READING = "live_reading"
    READINESS = "readiness"
    SINCE_CLEAR = "since_clear"
    RECALL = "recall"
    COMPLAINT = "complaint"
    USER_REPORT = "user_report"
    DERIVED = "derived"
    EVIDENCE_GAP = "evidence_gap"
    COMMUNITY_HINT = "community_hint"


class SourceType(StrEnum):
    OBD = "obd"
    OBDEX = "obdex"
    WAL33D = "wal33d"
    NHTSA_RECALLS = "nhtsa_recalls"
    NHTSA_COMPLAINTS = "nhtsa_complaints"
    USER = "user"
    COMPUTED = "computed"


class GapKind(StrEnum):
    NO_DEFINITION = "no_definition"
    P1XXX_NO_DEFINITION = "p1xxx_no_definition"
    PID_UNSUPPORTED = "pid_unsupported"
    READ_ERROR = "read_error"
    NHTSA_NONE_FOUND = "nhtsa_none_found"
    NHTSA_UNAVAILABLE = "nhtsa_unavailable"
    NO_VIN = "no_vin"
    NO_FREEZE_FRAME = "no_freeze_frame"


class EvidenceSource(Model):
    type: SourceType
    url: str | None = None
    retrieved_at: UtcDatetime
    dataset_version: str | None = None  # commit hash for obdex / wal33d


class Evidence(Model):
    id: EvidenceId
    kind: EvidenceKind
    tier: Tier
    source: EvidenceSource
    summary: Annotated[str, Field(max_length=300)]  # what the model reads
    payload: dict[str, Any]  # payload rules of §3.3 are enforced by EvidenceStore.add
    matched_name: str | None = None  # NHTSA items only
