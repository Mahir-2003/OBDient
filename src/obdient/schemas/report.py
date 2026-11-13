"""The report: a model-written draft plus code-written fields (SPEC §3.4)."""

from enum import StrEnum
from typing import Annotated

from pydantic import Field

from obdient.schemas.common import (
    SCHEMA_VERSION,
    DtcCode,
    Model,
    SnapshotSource,
    Tier,
    UtcDatetime,
)
from obdient.schemas.evidence import Evidence
from obdient.schemas.snapshot import DtcStatus


class Mode(StrEnum):
    DIAGNOSE = "diagnose"
    USED_CAR = "used_car"  # v0.2
    HANDOFF = "handoff"  # v0.2


class Likelihood(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNCLEAR = "unclear"


class FindingCategory(StrEnum):
    # air / fuel
    UNMETERED_AIR_LEAK = "unmetered_air_leak"
    PCV_FAILURE = "pcv_failure"
    VACUUM_LEAK = "vacuum_leak"
    MAF_FAULT = "maf_fault"
    FUEL_DELIVERY = "fuel_delivery"
    INJECTOR_FAULT = "injector_fault"
    O2_SENSOR_FAULT = "o2_sensor_fault"
    # ignition
    IGNITION_COIL_OR_PLUG = "ignition_coil_or_plug"
    MISFIRE_OTHER = "misfire_other"
    # cooling
    COOLING_SYSTEM_LEAK = "cooling_system_leak"
    THERMOSTAT_FAULT = "thermostat_fault"
    OVERHEATING = "overheating"
    # boost
    BOOST_LEAK = "boost_leak"
    DIVERTER_VALVE_FAULT = "diverter_valve_fault"
    WASTEGATE_FAULT = "wastegate_fault"
    TURBO_FAULT = "turbo_fault"
    # emissions
    CATALYST_AGED = "catalyst_aged"
    EVAP_LEAK = "evap_leak"
    GAS_CAP = "gas_cap"
    # electrical
    CHARGING_OR_BATTERY = "charging_or_battery"
    # meta
    RECENTLY_CLEARED_CODES = "recently_cleared_codes"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    OTHER = "other"


class EscalationLevel(StrEnum):
    STOP = "stop"
    PROMPT = "prompt"
    OUT_OF_SCOPE = "out_of_scope"
    RECALL = "recall"


class Escalation(Model):
    rule_id: str  # §6.2
    level: EscalationLevel
    banner: str  # fixed text per level
    reason: str  # fixed template per rule, filled by code
    evidence_ids: list[str]


# ---- model-written part ----------------------------------------------------

EvidenceIds = Annotated[list[str], Field(min_length=1)]
Text300 = Annotated[str, Field(min_length=1, max_length=300)]


class Claim(Model):
    text: Annotated[str, Field(min_length=1, max_length=400)]
    evidence_ids: EvidenceIds


class Finding(Model):
    title: Annotated[str, Field(min_length=1, max_length=120)]
    category: FindingCategory
    likelihood: Likelihood
    explanation: Annotated[str, Field(min_length=1, max_length=1200)]
    claims: Annotated[list[Claim], Field(min_length=1, max_length=8)]
    suggested_checks: Annotated[list[Text300], Field(max_length=6)]


class RuledOut(Model):
    hypothesis: Annotated[str, Field(min_length=1, max_length=200)]
    reason: Annotated[str, Field(min_length=1, max_length=400)]
    evidence_ids: EvidenceIds


class ComplaintPattern(Model):
    text: Annotated[str, Field(min_length=1, max_length=400)]
    evidence_ids: EvidenceIds  # all of kind complaint (verifier check 2)


class ReportDraft(Model):
    """The argument of submit_report, and the baseline's output schema."""

    findings: Annotated[list[Finding], Field(min_length=1, max_length=5)]  # most likely first
    ruled_out: Annotated[list[RuledOut], Field(max_length=8)] = []
    complaint_patterns: Annotated[list[ComplaintPattern], Field(max_length=5)] = []
    questions_for_mechanic: Annotated[list[Text300], Field(max_length=8)] = []
    next_steps: Annotated[list[Text300], Field(max_length=8)] = []


# ---- code-written part -----------------------------------------------------


class VehicleSummary(Model):
    vin_masked: str | None  # first 3 + last 4 characters, the rest "*"
    make: str | None
    model: str | None
    model_year: int | None
    engine: str | None
    mil_on: bool | None
    source: SnapshotSource
    captured_at: UtcDatetime


class CodeEntry(Model):
    code: DtcCode
    statuses: list[DtcStatus]
    title: str | None  # OBDex title, Wal33D description, or None
    definition_tier: Tier | None
    labels: list[str]  # e.g. the Wal33D label and caution line
    evidence_ids: list[str]  # the dtc item and its definition / hint / gap


class RecallEntry(Model):
    campaign_number: str
    component: str
    summary: str
    matched_names: list[str]
    evidence_id: str


class EvidenceGapEntry(Model):
    text: str
    evidence_id: str


class ReportedComplaintPattern(Model):
    label: str  # always COMPLAINT_LABEL
    text: str
    evidence_ids: list[str]


class Violation(Model):
    check: Annotated[int, Field(ge=1, le=8)]  # §7.8
    code: str  # stable machine code, §7.8
    location: str  # e.g. "findings[1].claims[2]"
    message: str  # what the model is shown


class Verification(Model):
    passed: bool
    violations: list[Violation]  # of the last verified draft; empty when passed
    revision_count: int


class Report(Model):
    report_id: str
    schema_version: int = SCHEMA_VERSION
    snapshot_id: str
    run_id: str
    mode: Mode
    generated_at: UtcDatetime
    incomplete: bool
    incomplete_reason: str | None
    vehicle_summary: VehicleSummary
    escalations: list[Escalation]
    codes: list[CodeEntry]
    findings: list[Finding]  # empty iff incomplete
    ruled_out: list[RuledOut]
    recalls: list[RecallEntry]
    recalls_note: str  # always RECALLS_NOTE
    nhtsa_names_tried: list[str]
    vin_lookup_url: str  # always VIN_LOOKUP_URL
    complaint_patterns: list[ReportedComplaintPattern]
    evidence_gaps: list[EvidenceGapEntry]
    questions_for_mechanic: list[str]
    next_steps: list[str]
    disclaimer: str  # always DISCLAIMER
    verification: Verification
    cost_estimates: None = None  # v0.2
    evidence: list[Evidence]  # the full store, for rendering citations
