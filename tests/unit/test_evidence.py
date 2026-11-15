"""EvidenceStore, prefetch order, payload rules, derived metrics, complaint search (§5.6)."""

import re
from datetime import timedelta
from typing import Any

import pytest

from obdient.errors import UnknownEvidenceError
from obdient.knowledge.evidence import (
    ComplaintCorpus,
    EvidenceStore,
    add_live_readings,
    derive_live,
    fmt,
    prefetch,
    quantity,
)
from obdient.knowledge.nhtsa import NhtsaResult
from obdient.knowledge.obdex import ObdexIndex
from obdient.schemas.common import CANONICAL_UNITS, Condition, Tier
from obdient.schemas.evidence import Evidence, EvidenceKind, EvidenceSource, SourceType
from obdient.schemas.nhtsa import Complaint
from obdient.schemas.scenario import load_scenario
from obdient.schemas.snapshot import ReadError, ReadErrorKind, Reading
from support import NOW, SCENARIOS_DIR, Prefetched, make_scenario

SOURCE = EvidenceSource(type=SourceType.OBD, retrieved_at=NOW)


def add(store: EvidenceStore, payload: dict[str, Any], key: str | None = None) -> Evidence:
    return store.add(EvidenceKind.LIVE_READING, Tier.T1, SOURCE, "summary", payload, dedupe_key=key)


def fetch(obdex: ObdexIndex, name: str = "s01_pcv_lean", **overrides: Any) -> Prefetched:
    if overrides:
        return Prefetched(make_scenario(**overrides), obdex)
    return Prefetched(load_scenario(SCENARIOS_DIR / f"{name}.yaml"), obdex)


def derived(run: Prefetched, metric: str) -> list[Evidence]:
    return [e for e in run.store.by_kind(EvidenceKind.DERIVED) if e.payload["metric"] == metric]


def gaps(run: Prefetched) -> dict[str, str]:
    return {e.payload["gap"]: e.summary for e in run.store.by_kind(EvidenceKind.EVIDENCE_GAP)}


# ---- the store ---------------------------------------------------------------


def test_ids_are_sequential_and_lookups_work() -> None:
    store = EvidenceStore()
    first = add(store, {"pid": "010C"}, key="a")
    second = add(store, {"pid": "010D"})
    assert (first.id, second.id) == ("E1", "E2")
    assert store.get("E2") is second and store.has("E1") and not store.has("E3")
    assert store.all() == [first, second]
    assert store.find("a") is first and store.find("b") is None
    assert store.key_of("E1") == "a" and store.key_of("E2") is None
    assert store.by_kind(EvidenceKind.DTC) == []


@pytest.mark.parametrize("bad", ["E0", "E3", "e1", "1", "E01", "", "E1 "])
def test_unknown_ids_raise(bad: str) -> None:
    store = EvidenceStore()
    add(store, {})
    add(store, {})
    assert not store.has(bad)
    with pytest.raises(UnknownEvidenceError):
        store.get(bad)
    with pytest.raises(KeyError):  # it is also a KeyError
        store.get(bad)


def test_same_dedupe_key_returns_the_existing_item_without_a_new_id() -> None:
    store = EvidenceStore()
    first = add(store, {"pid": "010C"}, key="live:010C:idle:0")
    again = add(store, {"pid": "different"}, key="live:010C:idle:0")
    assert again is first and len(store.all()) == 1
    assert add(store, {}).id == "E2"


@pytest.mark.parametrize(
    "payload",
    [
        {"rpm": 780},
        {"rpm": 780.5},
        {"nested": {"deep": [{"value": 1}]}},
        {"list": ["ok", 3]},
        {"q": {"value": 1, "unit": "furlongs"}},
        {"q": {"value": "1", "unit": "rpm"}},
        {"q": {"value": True, "unit": "count"}},
        {"q": {"value": float("nan"), "unit": "%"}},
        {"q": {"value": float("inf"), "unit": "%"}},
        {"thing": object()},
    ],
)
def test_bare_numbers_and_bad_quantities_are_rejected(payload: dict[str, Any]) -> None:
    store = EvidenceStore()
    with pytest.raises(ValueError, match="payload"):
        add(store, payload)
    assert store.all() == []


def test_quantities_strings_bools_and_none_are_allowed() -> None:
    store = EvidenceStore()
    add(
        store,
        {
            "mean": {"value": 19.5, "unit": "%"},
            "n": {"value": 3, "unit": "count"},
            "text": "780 rpm in a string is fine",
            "crash": False,
            "started_when": None,
            "list": [{"value": 1, "unit": "bank"}, "x"],
        },
    )
    assert len(store.all()) == 1


def test_summary_is_cut_to_300_characters_and_whitespace_collapsed() -> None:
    store = EvidenceStore()
    item = store.add(EvidenceKind.RECALL, Tier.T1, SOURCE, "a  b\n" + "x" * 400, {})
    assert len(item.summary) == 300 and item.summary.startswith("a b x") and item.summary[-1] == "…"


def test_quantity_and_fmt_round_to_display_precision() -> None:
    assert quantity(19.54, "%", 1) == {"value": 19.5, "unit": "%"}
    assert quantity(780.4, "rpm") == {"value": 780, "unit": "rpm"}
    assert isinstance(quantity(3.0, "count")["value"], int)
    assert fmt(19.5, "%", 1, signed=True) == "+19.5 %"
    assert fmt(-12, "%", 1, signed=True) == "-12.0 %"
    assert fmt(-0.04, "%", 1, signed=True) == "+0.0 %"  # never "-0.0"
    assert fmt(2.4, "g/s", 2) == "2.40 g/s" and fmt(88, "°C") == "88 °C" and fmt(45, None) == "45"


# ---- prefetch order and the s01 evidence list --------------------------------


def test_s01_evidence_list_is_exactly_this(obdex: ObdexIndex) -> None:
    run = fetch(obdex)
    listing = [f"{e.id} {e.kind.value} {e.tier.value}" for e in run.store.all()]
    assert listing == [
        "E1 user_report user",
        "E2 dtc 1",
        "E3 dtc_definition 1",
        "E4 freeze_frame 1",
        "E5 readiness 1",
        "E6 since_clear 1",
        *[f"E{n} live_reading 1" for n in range(7, 19)],  # 6 PIDs at hold_2500, then at idle
        *[f"E{n} derived 1" for n in range(19, 26)],
        "E26 recall 1",
        "E27 recall 1",
        "E28 recall 1",
        "E29 complaint 3",
    ]
    summary = {e.id: e.summary for e in run.store.all()}
    assert summary["E1"] == (
        'The user reports: "Slight whistle from the engine at idle". '
        "Warning lights seen: steady_mil."
    )
    assert summary["E2"] == "Code P0171 is present (stored, permanent) on ECU 7E8."
    assert summary["E3"].startswith(
        "OBDex definition of P0171: System Too Lean Bank 1. Common causes: Air leak after"
    )
    assert summary["E4"] == (
        "Freeze frame for P0171: coolant 88 °C, STFT bank 1 +6.2 %, LTFT bank 1 +19.5 %, "
        "engine speed 780 rpm."
    )
    assert summary["E5"] == "All 7 supported monitors are complete."
    assert summary["E6"] == "Since codes were last cleared: 45 warm-ups, 820 km."
    assert summary["E8"] == "STFT bank 1 at hold_2500: -12.0 % (n=3, min -12.0 %, max -12.0 %)."
    assert summary["E15"] == "LTFT bank 1 at idle: +19.5 % (n=3, min +19.5 %, max +19.5 %)."
    assert summary["E18"] == "MAF at idle: 2.40 g/s (n=3, min 2.40 g/s, max 2.40 g/s)."
    assert summary["E19"] == (
        "Total fuel trim bank 1 at hold_2500: +7.5 % (STFT -12.0 % plus LTFT +19.5 %)."
    )
    assert (
        summary["E20"] == "Total fuel trim bank 1 at idle: +25.7 % (STFT +6.2 % plus LTFT +19.5 %)."
    )
    assert summary["E22"] == (
        "Total fuel trim bank 1 changes by -18.2 % from idle to hold_2500 "
        "(idle +25.7 %, hold_2500 +7.5 %)."
    )
    assert summary["E26"].startswith("NHTSA recall 24V110000 (FUEL SYSTEM, GASOLINE): Volkswagen")
    assert summary["E29"].startswith(
        "50 owner complaints on file under names: Golf GTI, GTI (unverified). Top components: "
    )
    assert [e.matched_name for e in run.store.by_kind(EvidenceKind.RECALL)] == ["GTI"] * 3
    assert run.store.get("E29").matched_name == "Golf GTI, GTI"
    assert len(run.corpus) == 50 and run.corpus.names_tried == ["Golf GTI", "GTI"]


def test_ids_are_stable_across_runs(obdex: ObdexIndex) -> None:
    first, second = fetch(obdex), fetch(obdex)
    assert first.summaries() == second.summaries()
    assert [e.payload for e in first.store.all()] == [e.payload for e in second.store.all()]


@pytest.mark.parametrize("path", sorted(SCENARIOS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_every_scenario_prefetches_with_valid_evidence(path: Any, obdex: ObdexIndex) -> None:
    run = Prefetched(load_scenario(path), obdex)
    items = run.store.all()
    assert [e.id for e in items] == [f"E{n}" for n in range(1, len(items) + 1)]
    assert all(e.summary and len(e.summary) <= 300 for e in items)
    order = [e.kind for e in items if e.kind is not EvidenceKind.EVIDENCE_GAP]
    ranks = {
        EvidenceKind.USER_REPORT: 1,
        EvidenceKind.DTC: 2,
        EvidenceKind.DTC_DEFINITION: 3,
        EvidenceKind.FREEZE_FRAME: 4,
        EvidenceKind.READINESS: 5,
        EvidenceKind.SINCE_CLEAR: 6,
        EvidenceKind.LIVE_READING: 7,
        EvidenceKind.DERIVED: 8,
        EvidenceKind.RECALL: 9,
        EvidenceKind.COMPLAINT: 10,
    }
    assert [ranks[k] for k in order] == sorted(ranks[k] for k in order)
    for item in items:
        if item.kind is EvidenceKind.DERIVED:
            assert all(run.store.has(i) for i in item.payload["inputs"])
            assert all(int(i[1:]) < int(item.id[1:]) for i in item.payload["inputs"])


def test_tiers_and_sources_per_kind(obdex: ObdexIndex) -> None:
    run = fetch(obdex)
    by_kind = {e.kind: e for e in run.store.all()}
    assert by_kind[EvidenceKind.USER_REPORT].tier is Tier.USER
    assert by_kind[EvidenceKind.COMPLAINT].tier is Tier.T3
    definition = by_kind[EvidenceKind.DTC_DEFINITION]
    assert definition.source.type is SourceType.OBDEX
    assert definition.source.dataset_version == "9ba1ffa"
    assert (
        definition.source.url == "https://www.law.cornell.edu/regulations/california/13-CCR-1968.2"
    )
    assert by_kind[EvidenceKind.DERIVED].source.type is SourceType.COMPUTED
    assert by_kind[EvidenceKind.RECALL].source.type is SourceType.NHTSA_RECALLS
    assert all(e.source.retrieved_at == NOW for e in run.store.all())


def test_payload_shapes(obdex: ObdexIndex) -> None:
    run = fetch(obdex)
    assert run.store.get("E2").payload == {
        "code": "P0171",
        "statuses": ["stored", "permanent"],
        "ecus": ["7E8"],
    }
    assert set(run.store.get("E3").payload) == {
        "code",
        "title",
        "description",
        "causes",
        "symptoms",
        "flags",
        "related_codes",
        "source_urls",
    }
    assert run.store.get("E4").payload == {
        "trigger_code": "P0171",
        "readings": {
            "ect_c": {"value": 88, "unit": "°C"},
            "stft_b1": {"value": 6.2, "unit": "%"},
            "ltft_b1": {"value": 19.5, "unit": "%"},
            "rpm": {"value": 780, "unit": "rpm"},
        },
    }
    readiness = run.store.get("E5").payload
    assert (
        readiness["monitors"]["egr"] == "unsupported"
        and readiness["monitors"]["evap"] == "complete"
    )
    assert readiness["supported"] == {"value": 7, "unit": "count"}
    assert run.store.get("E6").payload == {
        "warmups": {"value": 45, "unit": "count"},
        "distance_km": {"value": 820, "unit": "km"},
    }
    assert run.store.get("E15").payload == {
        "pid": "0107",
        "name": "ltft_b1",
        "condition": "idle",
        "n": {"value": 3, "unit": "count"},
        "mean": {"value": 19.5, "unit": "%"},
        "min": {"value": 19.5, "unit": "%"},
        "max": {"value": 19.5, "unit": "%"},
    }
    assert set(run.store.get("E26").payload) == {
        "campaign_number",
        "component",
        "summary",
        "consequence",
        "remedy",
        "report_date",
    }
    complaints = run.store.get("E29").payload
    assert complaints["total"] == {"value": 50, "unit": "count"}
    assert complaints["names_tried"] == ["Golf GTI", "GTI"]
    assert sum(q["value"] for q in complaints["by_component"].values()) >= 50
    units = {q["unit"] for e in run.store.all() for q in _quantities(e.payload)}
    assert units <= CANONICAL_UNITS


def _quantities(value: object) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        if set(value) == {"value", "unit"}:
            return [value]  # pyright: ignore[reportUnknownVariableType]
        return [q for item in value.values() for q in _quantities(item)]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [q for item in value for q in _quantities(item)]  # pyright: ignore[reportUnknownVariableType]
    return []


def test_dtc_statuses_merge_and_cylinder_is_a_quantity(obdex: ObdexIndex) -> None:
    run = fetch(obdex, "s03_coil_cyl2")
    codes = {e.payload["code"]: e for e in run.store.by_kind(EvidenceKind.DTC)}
    assert list(codes) == ["P0300", "P0302"]  # sorted
    assert codes["P0302"].payload["cylinder"] == {"value": 2, "unit": "cylinder"}
    assert "cylinder" not in codes["P0300"].payload
    # all dtc items come before any definition
    kinds = [e.kind.value for e in run.store.all()][1:5]
    assert kinds == ["dtc", "dtc", "dtc_definition", "dtc_definition"]


def test_empty_user_report_adds_no_item(obdex: ObdexIndex) -> None:
    run = fetch(obdex, user_report={})
    assert run.store.by_kind(EvidenceKind.USER_REPORT) == []
    assert run.store.get("E1").kind is EvidenceKind.DTC


def test_user_report_summary_lists_what_was_said(obdex: ObdexIndex) -> None:
    run = fetch(
        obdex,
        user_report={
            "symptoms_text": "smells sweet",
            "started_when": "Tuesday",
            "conditions": ["cold"],
            "checklist": {"fuel_smell": False, "brake_problem": True},
        },
    )
    item = run.store.get("E1")
    assert item.summary == (
        'The user reports: "smells sweet". Started: "Tuesday". Happens when: cold. '
        "Checklist: fuel_smell no, brake_problem yes."
    )
    assert item.payload["checklist"] == {
        "fuel_smell": False,
        "smoke_or_steam": None,
        "brake_problem": True,
        "steering_problem": None,
    }


# ---- definitions, hints and gaps ---------------------------------------------


def test_vw_p1_code_becomes_a_labelled_tier_3_hint_with_caution(obdex: ObdexIndex) -> None:
    run = fetch(obdex, dtcs={"stored": ["P0171", "P1136"]})
    (hint,) = run.store.by_kind(EvidenceKind.COMMUNITY_HINT)
    assert hint.tier is Tier.T3 and hint.source.type is SourceType.WAL33D
    assert hint.source.dataset_version == "04c43d7"
    assert hint.summary.startswith("Unverified community definition (Wal33D, MIT). P1136: ")
    assert hint.summary.endswith("may not apply to this model year.")  # a 2018 car
    assert set(hint.payload) == {"code", "description", "label", "caution"}
    assert len(run.store.by_kind(EvidenceKind.DTC_DEFINITION)) == 1


def test_unknown_p1_code_and_non_vw_p1_code_are_gaps(obdex: ObdexIndex) -> None:
    unknown = fetch(obdex, dtcs={"stored": ["P1999"]}, freeze_frame=None)
    assert "p1xxx_no_definition" in gaps(unknown)
    assert gaps(unknown)["p1xxx_no_definition"].startswith("P1999 is a manufacturer-specific code")

    ford = dict(vin="X", make="FORD", model="Focus", year=2003)
    other_make = fetch(obdex, dtcs={"stored": ["P1136"]}, freeze_frame=None, vehicle=ford)
    assert "p1xxx_no_definition" in gaps(other_make)
    assert other_make.store.by_kind(EvidenceKind.COMMUNITY_HINT) == []


def test_undefined_non_p1_code_is_a_plain_gap(obdex: ObdexIndex) -> None:
    run = fetch(obdex, dtcs={"stored": ["B3999"]}, freeze_frame=None)
    assert obdex.lookup_dtc("B3999") is None
    gap = next(e for e in run.store.by_kind(EvidenceKind.EVIDENCE_GAP) if e.payload.get("code"))
    assert gap.payload["gap"] == "no_definition" and gap.tier is Tier.T3


def test_stored_code_without_a_freeze_frame_is_a_gap(obdex: ObdexIndex) -> None:
    assert "no_freeze_frame" in gaps(fetch(obdex, "s06_catalyst"))
    assert "no_freeze_frame" not in gaps(fetch(obdex, "s08_recently_cleared"))  # no stored codes
    pending_only = fetch(obdex, dtcs={"pending": ["P0300"]}, freeze_frame=None)
    assert "no_freeze_frame" not in gaps(pending_only)


def test_no_nhtsa_records_is_a_gap_that_names_what_was_tried(obdex: ObdexIndex) -> None:
    found = gaps(fetch(obdex, "s02_maf_lean_load"))  # the scenario without a fixture
    run = fetch(obdex, "s02_maf_lean_load")
    summaries = [e.summary for e in run.store.by_kind(EvidenceKind.EVIDENCE_GAP)]
    assert "No recalls found under names: GTI. This is not proof that none exist." in summaries
    assert (
        "No owner complaints found under names: GTI. This is not proof that none exist."
        in summaries
    )
    assert "nhtsa_none_found" in found and "nhtsa_unavailable" not in found
    assert run.store.by_kind(EvidenceKind.RECALL) == [] and len(run.corpus) == 0


def test_failed_lookups_are_unavailable_gaps_not_none_found(obdex: ObdexIndex) -> None:
    run = fetch(obdex, nhtsa={"fixture": "does_not_exist"})
    found = [e.payload["gap"] for e in run.store.by_kind(EvidenceKind.EVIDENCE_GAP)]
    assert found.count("nhtsa_unavailable") == 4 and "nhtsa_none_found" not in found


def test_nhtsa_skipped_no_vin_missing_pids_and_read_errors(obdex: ObdexIndex) -> None:
    base = fetch(obdex, conditions={"idle": {"rpm": 800, "speed_kmh": 0}}, freeze_frame=None)
    errors = [
        ReadError(request="0902", kind=ReadErrorKind.NO_DATA, detail=None, ts=NOW),
        ReadError(request="0902", kind=ReadErrorKind.NO_DATA, detail=None, ts=NOW),  # duplicate
        *[
            ReadError(request=f"01{n:02X}", kind=ReadErrorKind.TIMEOUT, detail="slow", ts=NOW)
            for n in range(12)
        ],
    ]
    snapshot = base.snapshot.model_copy(
        update={"vin": None, "readiness": [], "read_errors": errors}
    )
    store = EvidenceStore()
    corpus = prefetch(snapshot, base.profile, None, obdex, store, NOW)

    found = [e.payload["gap"] for e in store.by_kind(EvidenceKind.EVIDENCE_GAP)]
    assert found.count("no_vin") == 1 and found.count("nhtsa_unavailable") == 1
    assert found.count("nhtsa_none_found") == 0
    assert sorted(
        e.payload["pid"] for e in store.all() if e.payload.get("gap") == "pid_unsupported"
    ) == [
        "0105",
        "0106",
        "0107",
    ]
    # one for the failed status request, then at most 10 distinct read errors
    assert found.count("read_error") == 1 + 10
    assert store.by_kind(EvidenceKind.READINESS) == [] and len(corpus) == 0
    assert "Reading 0902 failed: NO DATA." in [e.summary for e in store.all()]


def test_a_vin_reply_that_failed_to_parse_never_reaches_evidence(obdex: ObdexIndex) -> None:
    """elm327 puts the raw reply in a parse error's detail; for 0902 that is the VIN in hex."""
    base = fetch(obdex)
    vin_hex = "490201" + b"WVWZZZAUZJW123456".hex().upper()
    errors = [
        ReadError(request="0902", kind=ReadErrorKind.PARSE_ERROR, detail=vin_hex, ts=NOW),
        ReadError(request="0904", kind=ReadErrorKind.TIMEOUT, detail=vin_hex, ts=NOW),
        ReadError(request="010C", kind=ReadErrorKind.PARSE_ERROR, detail="41 0C zz", ts=NOW),
        ReadError(request="010D", kind=ReadErrorKind.TIMEOUT, detail="no prompt in 2.0 s", ts=NOW),
    ]
    store = EvidenceStore()
    snapshot = base.snapshot.model_copy(update={"read_errors": errors})
    prefetch(snapshot, base.profile, base.nhtsa, obdex, store, NOW)

    everything = " ".join(e.model_dump_json() for e in store.all())
    assert vin_hex not in everything and "5756575A" not in everything
    texts = [e.summary for e in store.by_kind(EvidenceKind.EVIDENCE_GAP)]
    assert "Reading 0902 failed: PARSE ERROR." in texts
    assert "Reading 0904 failed: TIMEOUT." in texts  # any mode 09 detail is withheld
    assert "Reading 010C failed: PARSE ERROR." in texts  # raw replies are withheld
    assert "Reading 010D failed: TIMEOUT (no prompt in 2.0 s)." in texts


def test_the_scenario_vin_is_in_no_evidence_item(obdex: ObdexIndex) -> None:
    run = fetch(obdex)
    assert run.snapshot.vin == "WVWZZZ_FIXTURE"
    assert "WVWZZZ" not in " ".join(e.model_dump_json() for e in run.store.all())


def test_a_failed_status_read_is_reported_once(obdex: ObdexIndex) -> None:
    base = fetch(obdex)
    error = ReadError(request="0101", kind=ReadErrorKind.TIMEOUT, detail=None, ts=NOW)
    snapshot = base.snapshot.model_copy(update={"readiness": [], "read_errors": [error]})
    store = EvidenceStore()
    prefetch(snapshot, base.profile, base.nhtsa, obdex, store, NOW)
    found = [e.payload["gap"] for e in store.by_kind(EvidenceKind.EVIDENCE_GAP)]
    assert found.count("read_error") == 1


def test_a_long_freeze_frame_summary_ends_on_a_whole_value(obdex: ObdexIndex) -> None:
    names = [
        "load_pct", "ect_c", "stft_b1", "ltft_b1", "stft_b2", "ltft_b2", "map_kpa", "rpm",
        "speed_kmh", "timing_deg", "iat_c", "maf_gs", "throttle_pct", "runtime_s", "baro_kpa",
        "lambda_b1s1", "module_v", "abs_load_pct", "lambda_cmd", "ambient_c", "oil_temp_c",
        "fuel_pressure_kpa", "fuel_level_pct",
    ]  # fmt: skip
    frame = {"trigger": "P0171", **{name: 12.3 for name in names}}
    run = fetch(obdex, freeze_frame=frame, conditions={"idle": {"rpm": 800, "speed_kmh": 0}})
    item = run.store.find("freeze_frame")
    assert item is not None and len(item.summary) <= 300
    assert re.search(r", and \d+ more values\.$", item.summary)
    assert "…" not in item.summary
    assert len(item.payload["readings"]) == len(names)  # the payload still has every value


def test_readiness_and_since_clear_summaries(obdex: ObdexIndex) -> None:
    run = fetch(obdex, "s08_recently_cleared")
    texts = [e.summary for e in run.store.all()]
    assert "3 of 7 supported monitors incomplete: catalyst, evap, o2_sensor." in texts
    assert "Since codes were last cleared: 3 warm-ups, 12 km." in texts
    empty = fetch(obdex, since_clear={})
    assert empty.store.by_kind(EvidenceKind.SINCE_CLEAR) == []


# ---- derived metrics ---------------------------------------------------------


def test_total_fuel_trim_per_phase_and_freeze_frame(obdex: ObdexIndex) -> None:
    totals = {e.payload["condition"]: e for e in derived(fetch(obdex), "total_fuel_trim")}
    assert set(totals) == {"hold_2500", "idle", "freeze_frame"}
    idle = totals["idle"].payload
    assert idle["total"] == {"value": 25.7, "unit": "%"}
    assert (idle["stft"]["value"], idle["ltft"]["value"]) == (6.2, 19.5)
    assert idle["bank"] == {"value": 1, "unit": "bank"}
    assert idle["inputs"] == ["E14", "E15"]  # the STFT and LTFT readings at idle
    assert totals["freeze_frame"].payload["inputs"] == ["E4"]


def test_total_fuel_trim_needs_both_trims(obdex: ObdexIndex) -> None:
    run = fetch(obdex, conditions={"idle": {"rpm": 800, "ltft_b1": 9.4}}, freeze_frame=None)
    assert derived(run, "total_fuel_trim") == [] and derived(run, "fuel_trim_delta") == []


def test_bank_2_is_computed_separately(obdex: ObdexIndex) -> None:
    values = {"rpm": 800, "stft_b1": 1.6, "ltft_b1": 2.3, "stft_b2": -3.1, "ltft_b2": -7.0}
    run = fetch(obdex, conditions={"idle": values}, freeze_frame=None)
    totals = {
        e.payload["bank"]["value"]: e.payload["total"]["value"]
        for e in derived(run, "total_fuel_trim")
    }
    assert totals == {1: 3.9, 2: -10.1}


def test_fuel_trim_delta_is_hold_minus_idle(obdex: ObdexIndex) -> None:
    (leak,) = derived(fetch(obdex), "fuel_trim_delta")
    assert leak.payload["delta"] == {"value": -18.2, "unit": "%"}
    assert (leak.payload["idle_total"]["value"], leak.payload["hold_total"]["value"]) == (25.7, 7.5)
    assert leak.payload["inputs"] == ["E20", "E19"]
    (maf,) = derived(fetch(obdex, "s02_maf_lean_load"), "fuel_trim_delta")
    assert maf.payload["delta"]["value"] == 24.9  # the mirror image of s01
    assert derived(fetch(obdex, "s03_coil_cyl2"), "fuel_trim_delta") == []  # idle only


@pytest.mark.parametrize(
    ("maf", "position"), [(1.7, "below"), (1.8, "within"), (3.2, "within"), (3.3, "above")]
)
def test_maf_vs_expected_positions(obdex: ObdexIndex, maf: float, position: str) -> None:
    vehicle = dict(vin="X", make="VOLKSWAGEN", model="GTI", year=2013, displacement_l=2.0)
    run = fetch(
        obdex,
        vehicle=vehicle,
        conditions={"idle": {"rpm": 800, "maf_gs": maf, "ect_c": 70}},
        freeze_frame=None,
    )
    (item,) = derived(run, "maf_vs_expected")
    assert item.payload["position"] == position
    assert item.payload["expected_low"] == {"value": 1.8, "unit": "g/s"}
    assert item.payload["expected_high"] == {"value": 3.2, "unit": "g/s"}
    assert item.payload["displacement"] == {"value": 2.0, "unit": "L"}


@pytest.mark.parametrize(
    "change",
    [
        {"conditions": {"idle": {"rpm": 800, "maf_gs": 2.4, "ect_c": 69}}},  # not warm yet
        {"conditions": {"idle": {"rpm": 800, "maf_gs": 2.4}}},  # no coolant reading
        {"conditions": {"idle": {"rpm": 800, "ect_c": 90}}},  # no MAF
        {"vehicle": dict(vin="X", make="VOLKSWAGEN", model="GTI", year=2013)},  # no displacement
        {  # MAF only at hold_2500
            "conditions": {
                "idle": {"rpm": 800, "ect_c": 90},
                "hold_2500": {"rpm": 2500, "maf_gs": 9},
            },
        },
    ],
)
def test_maf_vs_expected_not_emitted(obdex: ObdexIndex, change: dict[str, Any]) -> None:
    vehicle = dict(vin="X", make="VOLKSWAGEN", model="GTI", year=2013, displacement_l=2.0)
    base: dict[str, Any] = {
        "vehicle": vehicle,
        "conditions": {"idle": {"rpm": 800, "maf_gs": 2.4, "ect_c": 90}},
        "freeze_frame": None,
    }
    assert derived(fetch(obdex, **{**base, **change}), "maf_vs_expected") == []


def test_warmup_after_runtime(obdex: ObdexIndex) -> None:
    (slow,) = derived(fetch(obdex, "s04_coolant_leak_p0128"), "warmup_after_runtime")
    assert slow.payload["status"] == "below_normal"
    assert slow.payload["runtime"] == {"value": 21, "unit": "min"}
    assert slow.payload["coolant"] == {"value": 66, "unit": "°C"}
    assert slow.summary == (
        "After 21 min of running, coolant is 66 °C at idle: below normal operating temperature."
    )

    def status(runtime: float, coolant: float) -> list[str]:
        run = fetch(
            obdex,
            conditions={"idle": {"rpm": 800, "runtime_s": runtime, "ect_c": coolant}},
            freeze_frame=None,
        )
        return [e.payload["status"] for e in derived(run, "warmup_after_runtime")]

    assert status(600, 69) == ["below_normal"]
    assert status(600, 70) == ["normal"]
    assert status(599, 40) == []  # too early to judge


def test_warmup_rate_needs_cold_start_readings_a_minute_apart(obdex: ObdexIndex) -> None:
    base = fetch(
        obdex,
        initial_condition="cold_start",
        conditions={"cold_start": {"rpm": 1100, "ect_c": [20, 24, 31]}},
        freeze_frame=None,
    )
    assert derived(base, "warmup_rate") == []  # samples are 0.5 s apart

    def spread(seconds: float) -> list[Evidence]:
        readings = [
            r.model_copy(update={"ts": r.ts + timedelta(seconds=seconds * index // 3)})
            for index, r in enumerate(base.snapshot.readings)
        ]
        store = EvidenceStore()
        snapshot = base.snapshot.model_copy(update={"readings": readings})
        prefetch(snapshot, base.profile, base.nhtsa, obdex, store, NOW)
        return [
            e for e in store.by_kind(EvidenceKind.DERIVED) if e.payload["metric"] == "warmup_rate"
        ]

    assert spread(30) == []
    (rate,) = spread(120)
    assert rate.payload["first"]["value"] == 20 and rate.payload["last"]["value"] == 31
    assert rate.payload["rate"]["unit"] == "°C/min" and rate.payload["rate"]["value"] > 0


def test_readiness_summary_counts_non_continuous_monitors(obdex: ObdexIndex) -> None:
    (item,) = derived(fetch(obdex, "s08_recently_cleared"), "readiness_summary")
    assert item.payload["supported"] == {"value": 4, "unit": "count"}
    assert item.payload["incomplete"] == {"value": 3, "unit": "count"}


def test_recently_cleared_fires_on_s08(obdex: ObdexIndex) -> None:
    (item,) = derived(fetch(obdex, "s08_recently_cleared"), "recently_cleared")
    assert item.payload["flag"] is True
    assert item.payload["warmups"] == {"value": 3, "unit": "count"}
    assert item.payload["distance"] == {"value": 12, "unit": "km"}
    assert item.summary.startswith("Codes were probably cleared recently: 3 warm-ups and 12 km")


@pytest.mark.parametrize(
    ("since", "readiness", "expected"),
    [
        ({"warmups": 5, "distance_km": 80}, {"catalyst": "incomplete"}, [True]),
        ({"warmups": 6, "distance_km": 80}, {"catalyst": "incomplete"}, [False]),
        ({"warmups": 5, "distance_km": 81}, {"catalyst": "incomplete"}, [False]),
        ({"warmups": 1, "distance_km": 5}, {"catalyst": "complete"}, [False]),
        ({"warmups": 1, "distance_km": 5}, {"misfire": "incomplete"}, [False]),  # continuous
        ({"warmups": 1}, {"catalyst": "incomplete"}, []),  # distance unknown: not emitted
        ({"distance_km": 5}, {"catalyst": "incomplete"}, []),
    ],
)
def test_recently_cleared_thresholds(
    obdex: ObdexIndex, since: dict[str, Any], readiness: dict[str, str], expected: list[bool]
) -> None:
    run = fetch(obdex, since_clear=since, readiness=readiness)
    assert [e.payload["flag"] for e in derived(run, "recently_cleared")] == expected


# ---- live readings during a run ----------------------------------------------


def reading(pid: str, name: str, value: float, condition: Condition) -> Reading:
    return Reading(
        pid=pid, name=name, value=value, unit="%", ts=NOW, ecu="7E8", condition=condition, raw=""
    )


def test_add_live_readings_uses_a_new_batch_and_derive_live_adds_totals(obdex: ObdexIndex) -> None:
    run = fetch(obdex, "s03_coil_cyl2")  # idle only, so no delta yet
    before = len(run.store.all())
    new = add_live_readings(
        run.store,
        [
            reading("0106", "stft_b1", 8.0, Condition.HOLD_2500),
            reading("0106", "stft_b1", 10.0, Condition.HOLD_2500),
            reading("0107", "ltft_b1", 1.6, Condition.HOLD_2500),
        ],
        NOW,
    )
    assert [run.store.key_of(e.id) for e in new] == [
        "live:0106:hold_2500:1",
        "live:0107:hold_2500:1",
    ]
    assert new[0].payload["mean"]["value"] == 9.0 and new[0].payload["n"]["value"] == 2
    assert [e.id for e in new] == [f"E{before + 1}", f"E{before + 2}"]

    extra = derive_live(run.store, new, NOW)
    assert [e.payload["metric"] for e in extra] == ["total_fuel_trim", "fuel_trim_delta"]
    assert extra[0].payload["total"]["value"] == 10.6
    assert extra[1].payload["delta"]["value"] == round(10.6 - 3.9, 1)

    again = add_live_readings(
        run.store, [reading("0106", "stft_b1", 2.0, Condition.HOLD_2500)], NOW
    )
    assert run.store.key_of(again[0].id) == "live:0106:hold_2500:2"
    assert derive_live(run.store, again, NOW) == []  # no LTFT in that batch
    assert derive_live(run.store, [], NOW) == []


# ---- complaint search (§7.5.5) -----------------------------------------------


def complaint(odi: str, summary: str, components: list[str], filed: str | None) -> Complaint:
    return Complaint(
        odi_number=odi,
        date_filed=filed,
        date_of_incident=None,
        components=components,
        summary=summary,
        crash=False,
        fire=False,
        matched_names=["GTI"],
    )


CORPUS = ComplaintCorpus(
    [
        complaint("1", "The water pump leaks coolant. Pump replaced.", ["ENGINE"], "2024-01-01"),
        complaint(
            "2",
            "Coolant leak from the thermostat housing.",
            ["ENGINE AND ENGINE COOLING"],
            "2025-01-01",
        ),
        complaint("3", "Brake pedal goes soft.", ["SERVICE BRAKES"], "2026-01-01"),
        complaint("4", "Coolant leak from the thermostat housing.", ["ENGINE"], "2025-01-01"),
        complaint("5", "pump " * 30, ["FUEL SYSTEM"], None),
    ],
    ["Golf GTI", "GTI"],
)


def ids(query: str, component: str | None = None, k: int = 5) -> list[str]:
    return [c.odi_number for c in CORPUS.search(query, component, k)]


def test_search_ranks_by_distinct_tokens_then_occurrences() -> None:
    # "1" has both tokens (20 + 4 occurrences); "5" has one token, capped at 10 occurrences.
    assert ids("water pump") == ["1", "5"]
    assert ids("pump") == ["5", "1"]  # 10 + 10 beats 10 + 2


def test_search_ties_break_by_date_then_odi_number() -> None:
    assert ids("thermostat housing") == ["2", "4"]  # same score and date: lower ODI first
    assert ids("coolant") == ["2", "4", "1"]  # newer first; "1" has it once, like the others


def test_search_ignores_stop_words_case_and_punctuation() -> None:
    assert ids("The PUMP, of the water!") == ids("pump water")
    assert ids("the of and") == []
    assert ids("zzz") == []


def test_search_component_filter_and_document_includes_components() -> None:
    assert ids("coolant", component="cooling") == ["2"]
    assert ids("coolant", component="  ") == ids("coolant")
    assert ids("brakes") == ["3"]  # found in the component, not the summary
    assert ids("coolant", component="brakes") == []


def test_search_returns_at_most_k() -> None:
    assert len(ids("coolant pump brake", k=2)) == 2
    assert len(CORPUS) == 5 and CORPUS.names_tried == ["Golf GTI", "GTI"]


def test_search_on_the_real_corpus(obdex: ObdexIndex) -> None:
    hits = fetch(obdex).corpus.search("water pump coolant leak", None)
    assert 1 <= len(hits) <= 5
    words = ("water", "pump", "coolant", "leak")
    assert all(any(w in f"{h.summary} {h.components}".lower() for w in words) for h in hits)


def test_prefetch_with_empty_result_keeps_the_names(obdex: ObdexIndex) -> None:
    base = fetch(obdex)
    empty = NhtsaResult(
        candidate_names=["Golf GTI", "GTI"], recalls=[], complaints=[], matched_names={}, errors=[]
    )
    corpus = prefetch(base.snapshot, base.profile, empty, obdex, EvidenceStore(), NOW)
    assert len(corpus) == 0 and corpus.names_tried == ["Golf GTI", "GTI"]
