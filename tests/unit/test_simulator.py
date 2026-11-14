"""ScenarioSimulator (SPEC §4.6): deterministic, condition-switching, list values, noise."""

from datetime import timedelta

import pytest

from obdient.errors import ScenarioError
from obdient.schemas.common import Condition, SnapshotSource
from obdient.schemas.snapshot import DtcStatus, MonitorName, ReadErrorKind, UserReport
from obdient.vehicle import parsers
from obdient.vehicle.collector import collect
from obdient.vehicle.simulator import (
    SIMULATED_EPOCH,
    SIMULATOR_ID,
    ScenarioSimulator,
    encode_dtc,
)
from support import make_scenario


def test_two_runs_give_identical_snapshot_json() -> None:
    scenario = make_scenario(noise={"pct": 2.0, "seed": 7})

    def snapshot_json() -> str:
        snapshot = collect(ScenarioSimulator(scenario), UserReport())
        return snapshot.model_copy(update={"snapshot_id": "x"}).model_dump_json()

    assert snapshot_json() == snapshot_json()


def test_connect_reports_the_simulator() -> None:
    simulator = ScenarioSimulator(make_scenario(adapter_voltage=12.6))
    info = simulator.connect()
    assert (info.id_string, info.protocol_number, info.voltage) == (SIMULATOR_ID, "6", 12.6)
    assert simulator.source is SnapshotSource.SIMULATOR
    assert simulator.vin() == "TESTVIN"


def test_supported_pids_are_the_union_plus_status_and_since_clear() -> None:
    scenario = make_scenario(
        freeze_frame={"trigger": "P0171", "load_pct": 50},
        conditions={"idle": {"rpm": 800}, "hold_2500": {"rpm": 2500, "maf_gs": 11}},
        since_clear={"warmups": 3},
    )
    assert ScenarioSimulator(scenario).supported_pids() == [
        "0101",
        "0104",
        "010C",
        "0110",
        "0130",
    ]


def test_condition_switching() -> None:
    simulator = ScenarioSimulator(make_scenario())
    assert simulator.condition is Condition.IDLE
    assert simulator.available_conditions() == [Condition.IDLE, Condition.HOLD_2500]

    (idle,) = simulator.read_pids(["010C"])
    simulator.set_condition(Condition.HOLD_2500)
    (held,) = simulator.read_pids(["010C"], Condition.IDLE)  # the argument is ignored

    assert (idle.value, idle.condition) == (780.0, Condition.IDLE)
    assert (held.value, held.condition) == (2500.0, Condition.HOLD_2500)
    with pytest.raises(ScenarioError, match="does not define condition 'warm'"):
        simulator.set_condition(Condition.WARM)


def test_initial_condition_is_honoured() -> None:
    simulator = ScenarioSimulator(make_scenario(initial_condition="hold_2500"))
    assert simulator.read_pids(["010C"])[0].value == 2500.0


def test_readings_carry_table_metadata_and_a_deterministic_clock() -> None:
    simulator = ScenarioSimulator(make_scenario())
    first = simulator.read_pids(["010C", "0107"])
    second = simulator.read_pids(["010C"])

    assert [(r.pid, r.name, r.unit) for r in first] == [
        ("010C", "rpm", "rpm"),
        ("0107", "ltft_b1", "%"),
    ]
    assert all((r.ecu, r.raw) == ("7E8", "") for r in first)
    assert {r.ts for r in first} == {SIMULATED_EPOCH + timedelta(seconds=0.5)}
    assert second[0].ts == SIMULATED_EPOCH + timedelta(seconds=1.0)


def test_unsupported_pids_are_skipped_silently() -> None:
    simulator = ScenarioSimulator(make_scenario())
    assert simulator.read_pids(["0110"]) == []
    assert simulator.drain_errors() == []


def test_supported_pid_missing_from_the_condition_is_no_data() -> None:
    scenario = make_scenario(
        conditions={"idle": {"rpm": 800}, "hold_2500": {"rpm": 2500, "maf_gs": 11}}
    )
    simulator = ScenarioSimulator(scenario)

    readings = simulator.read_pids(["010C", "0110"])
    errors = simulator.drain_errors()

    assert [r.pid for r in readings] == ["010C"]
    assert [(e.request, e.kind) for e in errors] == [("0110", ReadErrorKind.NO_DATA)]
    assert simulator.drain_errors() == []  # drained


def test_list_valued_pids_step_and_wrap_per_condition() -> None:
    scenario = make_scenario(
        conditions={
            "idle": {"rpm": [720, 790, 750]},
            "hold_2500": {"rpm": [2500, 2520]},
        }
    )
    simulator = ScenarioSimulator(scenario)

    def rpm() -> float | str | None:
        return simulator.read_pids(["010C"])[0].value

    assert [rpm() for _ in range(4)] == [720.0, 790.0, 750.0, 720.0]
    simulator.set_condition(Condition.HOLD_2500)
    assert [rpm() for _ in range(3)] == [2500.0, 2520.0, 2500.0]
    simulator.set_condition(Condition.IDLE)
    assert rpm() == 790.0  # each condition keeps its own position


def test_no_noise_by_default_and_values_are_rounded_to_the_pid_decimals() -> None:
    scenario = make_scenario(conditions={"idle": {"rpm": 780.4, "ltft_b1": 19.54}})
    readings = ScenarioSimulator(scenario).read_pids(["010C", "0107"])
    assert [r.value for r in readings] == [780.0, 19.5]


def test_noise_is_bounded_and_reproducible_by_seed() -> None:
    scenario = make_scenario(conditions={"idle": {"rpm": 1000}}, noise={"pct": 5, "seed": 3})

    def series(seed: int | None) -> list[float | str | None]:
        simulator = ScenarioSimulator(scenario, seed=seed)
        return [simulator.read_pids(["010C"])[0].value for _ in range(20)]

    default = series(None)
    assert default == series(3)  # no seed argument: the scenario's own seed
    assert series(1) == series(1)
    assert series(1) != series(2)
    assert len(set(default)) > 1
    assert all(isinstance(v, float) and 950.0 <= v <= 1050.0 for v in default)


def test_dtcs_by_status_with_raw_bytes_that_decode_back() -> None:
    dtcs = ScenarioSimulator(make_scenario()).read_dtcs()
    assert [(d.code, d.status) for d in dtcs] == [
        ("P0171", DtcStatus.STORED),
        ("P0300", DtcStatus.PENDING),
        ("P0171", DtcStatus.PERMANENT),
    ]
    assert all(d.ecu == "7E8" for d in dtcs)
    assert dtcs[0].raw == "0171"


@pytest.mark.parametrize("code", ["P0171", "P1A2B", "C0035", "B1234", "U0100", "P3FFF"])
def test_encode_dtc_is_the_inverse_of_the_parser(code: str) -> None:
    raw = bytes.fromhex(encode_dtc(code))
    assert parsers.decode_dtc_pair(raw[0], raw[1]) == code


def test_status_maps_mil_count_and_readiness_defaults() -> None:
    status = ScenarioSimulator(make_scenario()).status()
    assert status is not None
    assert (status.mil_on, status.dtc_count) == (True, 1)
    assert [m.name for m in status.monitors] == list(MonitorName)
    by_name = {m.name: (m.supported, m.complete) for m in status.monitors}
    assert by_name[MonitorName.CATALYST] == (True, True)
    assert by_name[MonitorName.EVAP] == (True, False)
    assert by_name[MonitorName.MISFIRE] == (True, True)  # continuous: complete by default
    assert by_name[MonitorName.EGR] == (False, None)  # not listed: unsupported


def test_mil_can_be_forced_off_and_continuous_monitors_overridden() -> None:
    scenario = make_scenario(mil_on=False, readiness={"misfire": "incomplete"})
    status = ScenarioSimulator(scenario).status()
    assert status is not None and status.mil_on is False
    assert status.monitors[0].complete is False


def test_freeze_frame_and_since_clear_map_directly() -> None:
    simulator = ScenarioSimulator(make_scenario())
    frame = simulator.freeze_frame()
    assert frame is not None
    assert (frame.trigger_code, frame.ecu) == ("P0171", "7E8")
    assert [(r.name, r.value, r.condition) for r in frame.readings] == [
        ("ltft_b1", 19.5, Condition.UNKNOWN),
        ("rpm", 780.0, Condition.UNKNOWN),
    ]
    since = simulator.since_clear()
    assert (since.warmups, since.distance_km, since.minutes_mil_on) == (45, 820.0, None)

    assert ScenarioSimulator(make_scenario(freeze_frame=None)).freeze_frame() is None


def test_since_clear_pids_can_be_read_live() -> None:
    (reading,) = ScenarioSimulator(make_scenario()).read_pids(["0131"])
    assert (reading.name, reading.value) == ("distance_since_clear_km", 820.0)
