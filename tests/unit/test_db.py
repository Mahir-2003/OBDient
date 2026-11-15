"""Database (SPEC §10.1): snapshots are insert-only; prefix lookup; the NHTSA cache."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from obdient.config import Budgets, ModelRole
from obdient.errors import NotFoundError
from obdient.schemas.common import SnapshotSource
from obdient.schemas.report import (
    Mode,
    Report,
    VehicleSummary,
    Verification,
)
from obdient.schemas.run import Run, RunConfig, StopReason, Strategy
from obdient.schemas.snapshot import Snapshot, UserReport, VehicleProfile
from obdient.storage.db import Database
from obdient.vehicle.collector import collect
from obdient.vehicle.simulator import ScenarioSimulator
from support import make_scenario

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db(tmp_path: Path, clock: Clock):
    database = Database(tmp_path / "nested" / "obdient.db", clock=clock)
    yield database
    database.close()


def snapshot(snapshot_id: str, vin: str | None = "TESTVIN") -> Snapshot:
    base = collect(ScenarioSimulator(make_scenario()), UserReport())
    return base.model_copy(update={"snapshot_id": snapshot_id, "vin": vin})


def run(run_id: str = "r" * 32, usd: float = 0.0, stop: StopReason | None = None) -> Run:
    return Run(
        run_id=run_id,
        started_at=T0,
        finished_at=None,
        snapshot_id="a" * 32,
        scenario_id="s01_pcv_lean",
        config=RunConfig(
            strategy=Strategy.BASELINE,
            role=ModelRole.DEV,
            model="gpt-6-luna",
            mode=Mode.DIAGNOSE,
            budgets=Budgets(),
            prompt_hash="abc",
            obdex_commit="9ba1ffa",
            wal33d_commit="04c43d7",
        ),
        steps=[],
        stop_reason=stop,
        error=None,
        turns=0,
        revisions=0,
        total_usd=usd,
        report_id=None,
    )


def test_creates_the_file_its_directory_and_all_six_tables(tmp_path: Path, db: Database) -> None:
    path = tmp_path / "nested" / "obdient.db"
    assert path.is_file()
    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        mode = conn.execute("PRAGMA journal_mode").fetchone()
    assert tables == {"meta", "vehicles", "snapshots", "reports", "runs", "nhtsa_cache"}
    assert version == ("1",)
    assert mode == ("wal",)


def test_reopening_keeps_the_data(tmp_path: Path) -> None:
    path = tmp_path / "obdient.db"
    first = Database(path)
    first.save_snapshot(snapshot("a" * 32))
    first.close()
    second = Database(path)
    assert second.get_snapshot("a" * 32).snapshot_id == "a" * 32
    second.close()


def test_snapshot_round_trips_exactly(db: Database) -> None:
    original = snapshot("a" * 32)
    db.save_snapshot(original)
    assert db.get_snapshot("a" * 32) == original


def test_snapshots_are_insert_only(db: Database) -> None:
    db.save_snapshot(snapshot("a" * 32))
    with pytest.raises(sqlite3.IntegrityError):
        db.save_snapshot(snapshot("a" * 32, vin="OTHER"))
    assert db.get_snapshot("a" * 32).vin == "TESTVIN"
    assert not hasattr(db, "update_snapshot") and not hasattr(db, "delete_snapshot")


def test_prefix_lookup(db: Database) -> None:
    db.save_snapshot(snapshot("abcdef0123" + "0" * 22))
    db.save_snapshot(snapshot("abcdef9999" + "0" * 22))
    db.save_snapshot(snapshot("123456" + "f" * 26))

    assert db.get_snapshot("123456").snapshot_id.startswith("123456")
    assert db.get_snapshot("abcdef0").snapshot_id.startswith("abcdef0123")
    with pytest.raises(NotFoundError, match="Several snapshots"):
        db.get_snapshot("abcdef")
    with pytest.raises(NotFoundError, match="at least 6 characters"):
        db.get_snapshot("12345")
    with pytest.raises(NotFoundError, match="No snapshot with an id starting with 'ffffff'"):
        db.get_snapshot("ffffff")


def test_prefix_wildcards_are_literal(db: Database) -> None:
    db.save_snapshot(snapshot("a" * 32))
    with pytest.raises(NotFoundError):
        db.get_snapshot("aaaaa%")
    with pytest.raises(NotFoundError):
        db.get_snapshot("aaaaa_")


def test_latest_snapshot_is_the_last_one_stored(db: Database) -> None:
    assert db.latest_snapshot() is None
    db.save_snapshot(snapshot("a" * 32, vin="VIN_ONE"))
    db.save_snapshot(snapshot("b" * 32, vin="VIN_TWO"))
    db.save_snapshot(snapshot("c" * 32, vin="VIN_ONE"))
    db.save_snapshot(snapshot("d" * 32, vin=None))

    latest = db.latest_snapshot()
    assert latest is not None and latest.snapshot_id == "d" * 32
    for_one = db.latest_snapshot("VIN_ONE")
    assert for_one is not None and for_one.snapshot_id == "c" * 32
    assert db.latest_snapshot("NOBODY") is None


def test_profile_upsert_and_no_vin_is_skipped(db: Database) -> None:
    def profile(vin: str | None, year: int) -> VehicleProfile:
        return VehicleProfile(
            vin=vin,
            make="VOLKSWAGEN",
            model="Golf GTI",
            model_year=year,
            engine=None,
            displacement_l=2.0,
            vpic_raw={},
            candidate_names=["Golf GTI", "GTI"],
            matched_names={},
        )

    db.save_profile(profile("VIN_ONE", 2017))
    db.save_profile(profile("VIN_ONE", 2018))
    db.save_profile(profile(None, 2018))

    stored = db.get_profile("VIN_ONE")
    assert stored is not None and stored.model_year == 2018
    assert db.get_profile("VIN_TWO") is None


def test_run_upsert_and_lookup(db: Database) -> None:
    db.save_run(run())
    db.save_run(run(usd=0.12, stop=StopReason.SUBMITTED))

    stored = db.get_run("r" * 32)
    assert (stored.total_usd, stored.stop_reason) == (0.12, StopReason.SUBMITTED)
    assert db.get_run("rrrrrr") == stored
    with pytest.raises(NotFoundError, match="No run"):
        db.get_run("x" * 32)


def report(report_id: str, generated_at: datetime = T0) -> Report:
    return Report(
        report_id=report_id,
        snapshot_id="a" * 32,
        run_id="r" * 32,
        mode=Mode.DIAGNOSE,
        generated_at=generated_at,
        incomplete=True,
        incomplete_reason="test",
        vehicle_summary=VehicleSummary(
            vin_masked=None,
            make=None,
            model=None,
            model_year=None,
            engine=None,
            mil_on=None,
            source=SnapshotSource.SIMULATOR,
            captured_at=T0,
        ),
        escalations=[],
        codes=[],
        findings=[],
        ruled_out=[],
        recalls=[],
        recalls_note="note",
        nhtsa_names_tried=[],
        vin_lookup_url="https://www.nhtsa.gov/recalls",
        complaint_patterns=[],
        evidence_gaps=[],
        questions_for_mechanic=[],
        next_steps=[],
        disclaimer="disclaimer",
        verification=Verification(passed=True, violations=[], revision_count=0),
        evidence=[],
    )


def test_report_save_get_latest_and_list(db: Database, tmp_path: Path) -> None:
    first, second = report("1" * 32), report("2" * 32, T0 + timedelta(hours=1))
    db.save_report(first, tmp_path / "1.md", None)
    db.save_report(second, None, tmp_path / "2.html")

    assert db.get_report("1" * 32) == first
    assert db.get_report("222222") == second
    assert db.latest_report() == second
    rows = db.list_reports()
    assert [(r.report_id, r.md_path, r.html_path) for r in rows] == [
        ("2" * 32, None, tmp_path / "2.html"),
        ("1" * 32, tmp_path / "1.md", None),
    ]
    assert rows[0].generated_at == T0 + timedelta(hours=1) and rows[0].mode is Mode.DIAGNOSE
    assert [r.report_id for r in db.list_reports(limit=1)] == ["2" * 32]
    with pytest.raises(sqlite3.IntegrityError):
        db.save_report(first, None, None)


def test_reports_start_empty(db: Database) -> None:
    assert db.latest_report() is None
    assert db.list_reports() == []
    with pytest.raises(NotFoundError, match="No report"):
        db.get_report("a" * 32)


def test_cache_put_get_and_overwrite(db: Database) -> None:
    assert db.get("recalls|make=VW", None) is None
    db.put("recalls|make=VW", {"results": [1, 2]})
    assert db.get("recalls|make=VW", None) == {"results": [1, 2]}
    db.put("recalls|make=VW", ["replaced"])
    assert db.get("recalls|make=VW", timedelta(hours=24)) == ["replaced"]


def test_cache_expiry_and_stale_read(db: Database, clock: Clock) -> None:
    db.put("k", "v")
    clock.now = T0 + timedelta(hours=24)
    assert db.get("k", timedelta(hours=24)) == "v"  # exactly at the limit is still fresh
    clock.now = T0 + timedelta(hours=24, seconds=1)
    assert db.get("k", timedelta(hours=24)) is None
    assert db.get("k", None) == "v"  # no max age: never expires
    assert db.get_stale("k") == "v"
    assert db.get_stale("missing") is None

    db.put("k", "fresh")  # refreshing resets the age
    assert db.get("k", timedelta(hours=24)) == "fresh"
