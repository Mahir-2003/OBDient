"""collect() (SPEC §4.8): call order, conditions, sanity checks, VIN rules, close()."""

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from obdient.errors import ScenarioError, VehicleConnectionError
from obdient.schemas.common import Condition, SnapshotSource
from obdient.schemas.snapshot import (
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
    UserReport,
)
from obdient.vehicle.collector import collect
from obdient.vehicle.simulator import ScenarioSimulator
from support import make_scenario

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
REAL_VIN = "WVWZZZAUZJW000000"
NAMES = {"010C": "rpm", "010D": "speed_kmh", "0105": "ect_c", "0107": "ltft_b1"}


class FakeVehicle:
    """A scripted Vehicle that records the order of calls."""

    source = SnapshotSource.EMULATOR

    def __init__(
        self,
        values: dict[str, float] | None = None,
        vin: str | None = REAL_VIN,
        status_fails: bool = False,
        fail_on: str | None = None,
    ) -> None:
        self.values = values if values is not None else {"010C": 800.0, "010D": 0.0, "0105": 90.0}
        self._vin = vin
        self._status_fails = status_fails
        self._fail_on = fail_on
        self.calls: list[str] = []
        self.read_args: list[tuple[list[str], Condition]] = []
        self.ecus = {"7E8", "7E9"}
        self.pending_errors: list[ReadError] = []

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name == self._fail_on:
            raise RuntimeError(f"{name} blew up")

    def connect(self) -> AdapterInfo:
        self.calls.append("connect")
        if self._fail_on == "connect":
            raise VehicleConnectionError("Adapter not responding")
        return AdapterInfo(id_string="FAKE", voltage=14.0)

    def close(self) -> None:
        self.calls.append("close")

    def supported_pids(self) -> list[str]:
        self._call("supported_pids")
        return ["0101", *sorted(self.values)]

    def vin(self) -> str | None:
        self._call("vin")
        return self._vin

    def status(self) -> StatusSinceClear | None:
        self._call("status")
        if self._status_fails:
            return None
        monitors = [Monitor(name=n, supported=True, complete=True) for n in MonitorName]
        return StatusSinceClear(mil_on=True, dtc_count=1, monitors=monitors)

    def read_dtcs(self) -> list[Dtc]:
        self._call("read_dtcs")
        return [Dtc(code="P0171", status=DtcStatus.STORED, ecu="7EA", raw="0171")]

    def freeze_frame(self) -> FreezeFrame | None:
        self._call("freeze_frame")
        return FreezeFrame(trigger_code="P0171", ecu="7EB", readings=[])

    def since_clear(self) -> SinceClear:
        self._call("since_clear")
        return SinceClear(warmups=4)

    def read_pids(
        self, pids: Sequence[str], condition: Condition = Condition.UNKNOWN
    ) -> list[Reading]:
        self._call("read_pids")
        self.read_args.append((list(pids), condition))
        return [
            Reading(
                pid=pid,
                name=NAMES.get(pid, pid),
                value=self.values[pid],
                unit=None,
                ts=NOW,
                ecu="7E8",
                condition=condition,
                raw="00",
            )
            for pid in pids
        ]

    def drain_errors(self) -> list[ReadError]:
        self._call("drain_errors")
        errors, self.pending_errors = self.pending_errors, []
        return errors


class Prompter:
    def __init__(self, skip: Sequence[Condition] = ()) -> None:
        self.skip = set(skip)
        self.events: list[str] = []

    def prepare(self, condition: Condition) -> bool:
        self.events.append(f"prepare {condition.value}")
        return condition not in self.skip

    def release(self, condition: Condition) -> None:
        self.events.append(f"release {condition.value}")


def run(vehicle: FakeVehicle, *conditions: Condition, **kwargs: object):
    return collect(
        vehicle,
        UserReport(symptoms_text="whistle"),
        conditions=conditions or (Condition.UNKNOWN,),
        sample_interval_s=0,
        **kwargs,  # pyright: ignore[reportArgumentType]
    )


# ---- order and contents ------------------------------------------------------


def test_calls_happen_in_the_spec_order_and_close_is_last() -> None:
    vehicle = FakeVehicle()
    run(vehicle)
    assert vehicle.calls == [
        "connect",
        "supported_pids",
        "vin",
        "status",
        "read_dtcs",
        "freeze_frame",
        "since_clear",
        "read_pids",
        "read_pids",
        "read_pids",
        "drain_errors",
        "close",
    ]


def test_snapshot_fields_come_from_the_vehicle() -> None:
    vehicle = FakeVehicle()
    before = datetime.now(UTC)
    snapshot = run(vehicle)

    assert len(snapshot.snapshot_id) == 32
    assert before <= snapshot.captured_at <= datetime.now(UTC)
    assert snapshot.source is SnapshotSource.EMULATOR
    assert snapshot.adapter.id_string == "FAKE"
    assert (snapshot.vin, snapshot.vin_source) == (REAL_VIN, "mode09")
    assert (snapshot.mil_on, snapshot.dtc_count, len(snapshot.readiness)) == (True, 1, 11)
    assert [d.code for d in snapshot.dtcs] == ["P0171"]
    assert snapshot.freeze_frame is not None
    assert snapshot.since_clear.warmups == 4
    assert snapshot.supported_pids == ["0101", "0105", "010C", "010D"]
    assert snapshot.ecus == ["7E8", "7E9", "7EA", "7EB"]  # readings, vehicle, DTCs, freeze frame
    assert snapshot.user_report.symptoms_text == "whistle"
    assert snapshot.conditions_captured == [Condition.UNKNOWN]
    assert len(snapshot.readings) == 9 and snapshot.read_errors == []


def test_only_supported_live_pids_are_requested_in_live_pid_order() -> None:
    vehicle = FakeVehicle()
    run(vehicle, live_pids=("010D", "0110", "010C"), samples_per_condition=2)
    assert vehicle.read_args == [(["010D", "010C"], Condition.UNKNOWN)] * 2


def test_two_snapshots_get_different_ids() -> None:
    assert run(FakeVehicle()).snapshot_id != run(FakeVehicle()).snapshot_id


def test_vehicle_errors_are_drained_into_the_snapshot() -> None:
    vehicle = FakeVehicle()
    vehicle.pending_errors = [
        ReadError(request="0902", kind=ReadErrorKind.NO_DATA, detail=None, ts=NOW)
    ]
    assert [e.request for e in run(vehicle).read_errors] == ["0902"]


def test_failed_status_leaves_mil_count_and_readiness_empty() -> None:
    snapshot = run(FakeVehicle(status_fails=True))
    assert (snapshot.mil_on, snapshot.dtc_count, snapshot.readiness) == (None, None, [])


# ---- close() -----------------------------------------------------------------


@pytest.mark.parametrize("step", ["supported_pids", "read_dtcs", "read_pids", "drain_errors"])
def test_close_runs_when_a_step_raises(step: str) -> None:
    vehicle = FakeVehicle(fail_on=step)
    with pytest.raises(RuntimeError, match="blew up"):
        run(vehicle)
    assert vehicle.calls[-1] == "close"


def test_connection_failure_propagates_and_still_closes() -> None:
    vehicle = FakeVehicle(fail_on="connect")
    with pytest.raises(VehicleConnectionError, match="Adapter not responding"):
        run(vehicle)
    assert vehicle.calls == ["connect", "close"]


# ---- conditions on a real vehicle --------------------------------------------


def test_conditions_are_captured_in_the_order_given() -> None:
    vehicle = FakeVehicle()
    prompter = Prompter()
    snapshot = run(vehicle, Condition.IDLE, Condition.WARM, prompter=prompter)

    assert snapshot.conditions_captured == [Condition.IDLE, Condition.WARM]
    assert [c for _, c in vehicle.read_args] == [Condition.IDLE] * 3 + [Condition.WARM] * 3
    assert prompter.events == ["prepare idle", "release idle", "prepare warm", "release warm"]


def test_a_skipped_condition_is_not_captured() -> None:
    vehicle = FakeVehicle()
    prompter = Prompter(skip=[Condition.IDLE])
    snapshot = run(vehicle, Condition.IDLE, Condition.WARM, prompter=prompter)

    assert snapshot.conditions_captured == [Condition.WARM]
    assert {r.condition for r in snapshot.readings} == {Condition.WARM}
    assert prompter.events == ["prepare idle", "prepare warm", "release warm"]


def test_a_repeated_label_is_listed_once_and_last() -> None:
    """The last entry must be the phase captured last (SPEC §3.1), even after relabelling."""
    values = {"010C": 1600.0, "010D": 0.0, "0105": 60.0}  # contradicts idle and warm, not hold
    values_by_condition = {Condition.HOLD_2500: {**values, "010C": 2500.0}}

    class Changing(FakeVehicle):
        def read_pids(
            self, pids: Sequence[str], condition: Condition = Condition.UNKNOWN
        ) -> list[Reading]:
            self.values = values_by_condition.get(condition, values)
            return super().read_pids(pids, condition)

    snapshot = run(Changing(values), Condition.IDLE, Condition.HOLD_2500, Condition.WARM)
    assert snapshot.conditions_captured == [Condition.HOLD_2500, Condition.UNKNOWN]
    assert [e.kind for e in snapshot.read_errors] == [ReadErrorKind.CONDITION_MISMATCH] * 2

    twice = run(FakeVehicle(), Condition.IDLE, Condition.IDLE)
    assert twice.conditions_captured == [Condition.IDLE]
    assert len(twice.readings) == 18


def test_unknown_is_never_prompted() -> None:
    prompter = Prompter()
    run(FakeVehicle(), prompter=prompter)
    assert prompter.events == []


def test_without_a_prompter_conditions_are_taken_as_stated() -> None:
    snapshot = run(FakeVehicle(), Condition.IDLE)
    assert snapshot.conditions_captured == [Condition.IDLE]


def test_release_is_called_even_if_reading_fails() -> None:
    prompter = Prompter()
    with pytest.raises(RuntimeError):
        run(FakeVehicle(fail_on="read_pids"), Condition.HOLD_2500, prompter=prompter)
    assert prompter.events == ["prepare hold_2500", "release hold_2500"]


def test_cold_start_and_warm_cannot_be_combined() -> None:
    vehicle = FakeVehicle()
    with pytest.raises(ValueError, match="cold_start and warm"):
        run(vehicle, Condition.COLD_START, Condition.WARM)
    assert vehicle.calls == []  # refused before touching the car


# ---- sanity check ------------------------------------------------------------


@pytest.mark.parametrize(
    ("condition", "values", "says"),
    [
        (Condition.IDLE, {"010C": 1600.0}, "engine speed averaged 1600 rpm"),
        (Condition.IDLE, {"010C": 300.0}, "engine speed averaged 300 rpm"),
        (Condition.IDLE, {"010C": 800.0, "010D": 5.0}, "vehicle speed averaged 5 km/h"),
        (Condition.HOLD_2500, {"010C": 2100.0}, "engine speed averaged 2100 rpm"),
        (Condition.HOLD_2500, {"010C": 2900.0}, "engine speed averaged 2900 rpm"),
        (Condition.HOLD_2500, {"010C": 2500.0, "010D": 12.0}, "vehicle speed averaged 12 km/h"),
        (Condition.WARM, {"0105": 69.0}, "coolant averaged 69 °C"),
        (Condition.COLD_START, {"0105": 51.0}, "coolant averaged 51 °C"),
    ],
)
def test_contradicted_condition_is_relabelled_unknown(
    condition: Condition, values: dict[str, float], says: str
) -> None:
    snapshot = run(FakeVehicle(values), condition)

    assert snapshot.conditions_captured == [Condition.UNKNOWN]
    assert {r.condition for r in snapshot.readings} == {Condition.UNKNOWN}
    (error,) = snapshot.read_errors
    assert error.kind is ReadErrorKind.CONDITION_MISMATCH
    assert error.detail == f"Declared {condition.value}, but {says}."


@pytest.mark.parametrize(
    ("condition", "values"),
    [
        (Condition.IDLE, {"010C": 1500.0, "010D": 0.0}),
        (Condition.IDLE, {"010C": 400.0}),
        (Condition.IDLE, {"0105": 20.0}),  # rpm missing does not contradict
        (Condition.HOLD_2500, {"010C": 2200.0}),
        (Condition.HOLD_2500, {"010C": 2800.0}),
        (Condition.WARM, {"0105": 70.0}),
        (Condition.WARM, {"010C": 3000.0, "010D": 80.0}),  # warm is only about coolant
        (Condition.COLD_START, {"0105": 50.0}),
        (Condition.COLD_START, {"010C": 1100.0}),  # coolant missing does not contradict
        (Condition.UNKNOWN, {"010C": 5000.0, "010D": 120.0}),
    ],
)
def test_consistent_or_unknowable_conditions_keep_their_label(
    condition: Condition, values: dict[str, float]
) -> None:
    snapshot = run(FakeVehicle(values), condition)
    assert snapshot.conditions_captured == [condition]
    assert snapshot.read_errors == []


def test_the_check_uses_the_mean_of_the_samples() -> None:
    scenario = make_scenario(
        conditions={"idle": {"rpm": [1400, 1500, 1700], "speed_kmh": 0}}, freeze_frame=None
    )
    snapshot = collect(ScenarioSimulator(scenario), UserReport())  # mean 1533 rpm
    assert snapshot.conditions_captured == [Condition.UNKNOWN]
    assert snapshot.read_errors[0].detail == "Declared idle, but engine speed averaged 1533 rpm."


def test_only_the_contradicted_phase_is_relabelled() -> None:
    scenario = make_scenario(
        conditions={
            "idle": {"rpm": 780, "speed_kmh": 0},
            "hold_2500": {"rpm": 1900, "speed_kmh": 0},
        },
        freeze_frame=None,
    )
    snapshot = collect(ScenarioSimulator(scenario), UserReport())
    assert snapshot.conditions_captured == [Condition.UNKNOWN, Condition.IDLE]
    assert [e.kind for e in snapshot.read_errors] == [ReadErrorKind.CONDITION_MISMATCH]


# ---- VIN ---------------------------------------------------------------------


def test_vin_override_wins_and_is_normalized() -> None:
    snapshot = run(FakeVehicle(), vin_override=" wvwzzzauzjw123456 ")
    assert (snapshot.vin, snapshot.vin_source) == ("WVWZZZAUZJW123456", "user")


@pytest.mark.parametrize("bad", ["SHORT", "WVWZZZAUZJW00000O", "WVWZZZAUZJW0000000"])
def test_bad_vin_override_is_refused_before_connecting(bad: str) -> None:
    vehicle = FakeVehicle()
    with pytest.raises(ValueError, match="17 characters"):
        run(vehicle, vin_override=bad)
    assert vehicle.calls == []


def test_no_vin_from_the_car() -> None:
    snapshot = run(FakeVehicle(vin=None))
    assert (snapshot.vin, snapshot.vin_source) == (None, None)


def test_malformed_vin_from_the_car_is_dropped_and_recorded() -> None:
    snapshot = run(FakeVehicle(vin="NOT A VIN"))
    assert (snapshot.vin, snapshot.vin_source) == (None, None)
    assert [(e.request, e.kind) for e in snapshot.read_errors] == [
        ("0902", ReadErrorKind.PARSE_ERROR)
    ]


# ---- simulator ---------------------------------------------------------------


def test_simulator_captures_every_condition_with_the_initial_one_last() -> None:
    simulator = ScenarioSimulator(make_scenario())
    snapshot = collect(simulator, UserReport())

    assert snapshot.conditions_captured == [Condition.HOLD_2500, Condition.IDLE]
    assert [r.condition for r in snapshot.readings if r.pid == "010C"] == (
        [Condition.HOLD_2500] * 3 + [Condition.IDLE] * 3
    )
    assert [r.value for r in snapshot.readings if r.pid == "010C"] == [2500.0] * 3 + [780.0] * 3
    assert simulator.condition is Condition.IDLE  # left in the initial condition
    assert (snapshot.vin, snapshot.vin_source) == ("TESTVIN", "scenario")  # exempt from pattern
    assert snapshot.ecus == ["7E8"]


def test_simulator_initial_condition_other_than_idle_is_still_last() -> None:
    simulator = ScenarioSimulator(make_scenario(initial_condition="hold_2500"))
    snapshot = collect(simulator, UserReport())
    assert snapshot.conditions_captured == [Condition.IDLE, Condition.HOLD_2500]
    assert simulator.condition is Condition.HOLD_2500


def test_simulator_with_explicit_conditions_captures_only_those() -> None:
    simulator = ScenarioSimulator(make_scenario())
    snapshot = collect(simulator, UserReport(), conditions=[Condition.HOLD_2500])
    assert snapshot.conditions_captured == [Condition.HOLD_2500]
    assert simulator.condition is Condition.IDLE


def test_simulator_asked_for_an_undefined_condition_raises() -> None:
    with pytest.raises(ScenarioError, match="does not define condition 'warm'"):
        collect(ScenarioSimulator(make_scenario()), UserReport(), conditions=[Condition.WARM])


def test_simulator_is_reset_to_its_initial_condition_when_collect_fails() -> None:
    simulator = ScenarioSimulator(make_scenario())
    with pytest.raises(ScenarioError):
        collect(simulator, UserReport(), conditions=[Condition.HOLD_2500, Condition.WARM])
    assert simulator.condition is Condition.IDLE


def test_simulator_is_never_prompted() -> None:
    prompter = Prompter()
    collect(ScenarioSimulator(make_scenario()), UserReport(), prompter=prompter)
    assert prompter.events == []
