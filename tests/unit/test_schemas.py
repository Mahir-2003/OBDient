import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from obdient.config import Budgets, ModelRole
from obdient.schemas.common import SCHEMA_VERSION, Condition, Quantity, SnapshotSource, Tier
from obdient.schemas.evidence import Evidence, EvidenceKind, EvidenceSource, SourceType
from obdient.schemas.report import (
    Claim,
    Finding,
    FindingCategory,
    Likelihood,
    Mode,
    ReportDraft,
)
from obdient.schemas.run import LlmStep, Run, RunConfig, StopReason, Strategy, ToolStep
from obdient.schemas.snapshot import (
    AdapterInfo,
    Dtc,
    DtcStatus,
    MonitorName,
    Reading,
    SinceClear,
    Snapshot,
    UserReport,
)

NOW = datetime(2026, 11, 12, 18, 30, tzinfo=UTC)


def make_reading(**overrides: Any) -> Reading:
    fields: dict[str, Any] = {
        "pid": "010C",
        "name": "rpm",
        "value": 780.0,
        "unit": "rpm",
        "ts": NOW,
        "ecu": "7E8",
        "condition": Condition.IDLE,
        "raw": "0C30",
    }
    return Reading(**{**fields, **overrides})


def make_snapshot() -> Snapshot:
    return Snapshot(
        snapshot_id="a" * 32,
        captured_at=NOW,
        source=SnapshotSource.SIMULATOR,
        adapter=AdapterInfo(id_string="ELM327 v1.5", protocol_number="6", voltage=14.1),
        vin=None,
        vin_source=None,
        mil_on=True,
        dtc_count=1,
        dtcs=[Dtc(code="P0171", status=DtcStatus.STORED, ecu="7E8", raw="0171")],
        freeze_frame=None,
        readiness=[],
        since_clear=SinceClear(warmups=45, distance_km=820),
        readings=[make_reading()],
        conditions_captured=[Condition.IDLE],
        supported_pids=["010C"],
        ecus=["7E8"],
        read_errors=[],
        user_report=UserReport(symptoms_text="whistle at idle"),
    )


def make_finding(**overrides: Any) -> Finding:
    fields: dict[str, Any] = {
        "title": "Unmetered air leak",
        "category": FindingCategory.UNMETERED_AIR_LEAK,
        "likelihood": Likelihood.HIGH,
        "explanation": "Trim is high at idle and falls with rpm.",
        "claims": [Claim(text="LTFT is +19.5 % at idle.", evidence_ids=["E4"])],
        "suggested_checks": [],
    }
    return Finding(**{**fields, **overrides})


# ---- base model behaviour ---------------------------------------------------


def test_models_are_frozen() -> None:
    q = Quantity(value=1.0, unit="%")
    with pytest.raises(ValidationError):
        q.value = 2.0  # pyright: ignore[reportAttributeAccessIssue]


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Quantity.model_validate({"value": 1.0, "unit": "%", "extra": 1})


def test_quantity_unit_may_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        Quantity(value=1.0, unit="")


def test_datetimes_must_be_aware_and_serialize_as_utc_z() -> None:
    with pytest.raises(ValidationError):
        make_reading(ts=datetime(2026, 11, 12, 18, 30))
    plus_two = timezone(timedelta(hours=2))
    r = make_reading(ts=datetime(2026, 11, 12, 20, 30, tzinfo=plus_two))
    assert json.loads(r.model_dump_json())["ts"] == "2026-11-12T18:30:00Z"


# ---- identifier patterns ----------------------------------------------------


@pytest.mark.parametrize("code", ["P0171", "U0100", "B1A2F", "C3FFF"])
def test_dtc_codes_accepted(code: str) -> None:
    assert Dtc(code=code, status=DtcStatus.PENDING, ecu="7E8", raw="0171").code == code


@pytest.mark.parametrize("code", ["p0171", "P4000", "P017", "P01711", "X0171", "P01G1"])
def test_dtc_codes_rejected(code: str) -> None:
    with pytest.raises(ValidationError):
        Dtc(code=code, status=DtcStatus.STORED, ecu="7E8", raw="0171")


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", "10C"),
        ("pid", "010c"),
        ("ecu", "7e8"),
        ("ecu", "07E8"),
        ("raw", "0c"),
        ("raw", "0C3"),
    ],
)
def test_reading_identifier_patterns(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        make_reading(**{field: value})


def test_simulator_reading_may_have_empty_raw() -> None:
    assert make_reading(raw="").raw == ""


@pytest.mark.parametrize("bad_id", ["E0", "E01", "e1", "1", "E"])
def test_evidence_id_pattern(bad_id: str) -> None:
    source = EvidenceSource(type=SourceType.OBD, retrieved_at=NOW)
    ok = Evidence(
        id="E12", kind=EvidenceKind.DTC, tier=Tier.T1, source=source, summary="x", payload={}
    )
    assert ok.id == "E12"
    with pytest.raises(ValidationError):
        Evidence(
            id=bad_id, kind=EvidenceKind.DTC, tier=Tier.T1, source=source, summary="x", payload={}
        )


def test_evidence_summary_is_capped_at_300_chars() -> None:
    source = EvidenceSource(type=SourceType.OBD, retrieved_at=NOW)
    with pytest.raises(ValidationError):
        Evidence(
            id="E1",
            kind=EvidenceKind.DTC,
            tier=Tier.T1,
            source=source,
            summary="x" * 301,
            payload={},
        )


# ---- snapshot ---------------------------------------------------------------


def test_snapshot_round_trips_through_json() -> None:
    snap = make_snapshot()
    assert snap.schema_version == SCHEMA_VERSION
    again = Snapshot.model_validate_json(snap.model_dump_json())
    assert again == snap
    assert again.model_dump_json() == snap.model_dump_json()


def test_monitor_names_are_eleven_in_spec_order() -> None:
    assert [m.value for m in MonitorName] == [
        "misfire",
        "fuel_system",
        "components",
        "catalyst",
        "heated_catalyst",
        "evap",
        "secondary_air",
        "ac_refrigerant",
        "o2_sensor",
        "o2_heater",
        "egr",
    ]


def test_user_report_limits() -> None:
    with pytest.raises(ValidationError):
        UserReport(symptoms_text="x" * 2001)
    with pytest.raises(ValidationError):
        UserReport.model_validate({"conditions": ["warmish"]})


# ---- report draft -----------------------------------------------------------


def test_draft_needs_one_to_five_findings() -> None:
    with pytest.raises(ValidationError):
        ReportDraft(findings=[])
    with pytest.raises(ValidationError):
        ReportDraft(findings=[make_finding()] * 6)
    assert len(ReportDraft(findings=[make_finding()] * 5).findings) == 5


def test_claim_needs_text_and_at_least_one_citation() -> None:
    with pytest.raises(ValidationError):
        Claim(text="LTFT is high.", evidence_ids=[])
    with pytest.raises(ValidationError):
        Claim(text="", evidence_ids=["E1"])
    with pytest.raises(ValidationError):
        Claim(text="x" * 401, evidence_ids=["E1"])


def test_finding_limits() -> None:
    claim = Claim(text="ok", evidence_ids=["E1"])
    with pytest.raises(ValidationError):
        make_finding(claims=[])
    with pytest.raises(ValidationError):
        make_finding(claims=[claim] * 9)
    with pytest.raises(ValidationError):
        make_finding(suggested_checks=["check"] * 7)
    with pytest.raises(ValidationError):
        make_finding(category="made_up_category")


def test_draft_optional_lists_default_to_empty() -> None:
    draft = ReportDraft(findings=[make_finding()])
    assert draft.ruled_out == []
    assert draft.questions_for_mechanic == []
    with pytest.raises(ValidationError):
        ReportDraft(findings=[make_finding()], next_steps=["x" * 301])


# ---- run --------------------------------------------------------------------


def test_run_steps_discriminate_on_type() -> None:
    config = RunConfig(
        strategy=Strategy.BASELINE,
        mode=Mode.DIAGNOSE,
        role=ModelRole.DEV,
        model="gpt-6-luna",
        budgets=Budgets(),
        prompt_hash="0123456789ab",
        obdex_commit="9ba1ffa",
        wal33d_commit="04c43d7",
    )
    run = Run(
        run_id="b" * 32,
        started_at=NOW,
        finished_at=None,
        snapshot_id="a" * 32,
        scenario_id="s01_pcv_lean",
        config=config,
        steps=[
            LlmStep(
                turn=1,
                model="gpt-6-luna",
                input_tokens=1000,
                cached_input_tokens=0,
                output_tokens=200,
                usd=0.0002,
                latency_ms=900,
                tool_calls=["lookup_dtc"],
            ),
            ToolStep(
                turn=1,
                name="lookup_dtc",
                args={"code": "P0171"},
                result_summary="definition returned",
                evidence_ids=["E9"],
                latency_ms=2,
            ),
        ],
        stop_reason=StopReason.SUBMITTED,
        error=None,
        turns=1,
        revisions=0,
        total_usd=0.0002,
        report_id=None,
    )
    again = Run.model_validate_json(run.model_dump_json())
    assert isinstance(again.steps[0], LlmStep)
    assert isinstance(again.steps[1], ToolStep)
    assert again == run


def test_run_step_with_unknown_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Run.model_validate({"steps": [{"type": "other"}]})
