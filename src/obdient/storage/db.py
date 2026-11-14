"""SQLite storage (SPEC §10.1): snapshots, profiles, reports, runs and the NHTSA cache."""

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from obdient.errors import NotFoundError
from obdient.schemas.common import SCHEMA_VERSION, Model, UtcDatetime
from obdient.schemas.report import Mode, Report
from obdient.schemas.run import Run
from obdient.schemas.snapshot import Snapshot, VehicleProfile

MIN_PREFIX = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta      (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vehicles  (vin TEXT PRIMARY KEY, profile_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (snapshot_id TEXT PRIMARY KEY, vin TEXT, captured_at TEXT NOT NULL,
                                      source TEXT NOT NULL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports   (report_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, run_id TEXT NOT NULL,
                                      mode TEXT NOT NULL, generated_at TEXT NOT NULL, json TEXT NOT NULL,
                                      md_path TEXT, html_path TEXT);
CREATE TABLE IF NOT EXISTS runs      (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, total_usd REAL NOT NULL,
                                      stop_reason TEXT, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nhtsa_cache (key TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_snapshots_vin ON snapshots (vin, captured_at);
CREATE INDEX IF NOT EXISTS idx_reports_snapshot ON reports (snapshot_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at);
"""  # noqa: E501


class ReportRow(Model):
    """One line of `list_reports`: the columns, without the report body."""

    report_id: str
    snapshot_id: str
    run_id: str
    mode: Mode
    generated_at: UtcDatetime
    md_path: Path | None
    html_path: Path | None


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class Database:
    """One SQLite file. Also implements `NhtsaCache` (SPEC §5.4)."""

    def __init__(self, path: Path, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- snapshots (immutable: there is no update) ----------------------------

    def save_snapshot(self, s: Snapshot) -> None:
        """INSERT only; saving an id that exists raises sqlite3.IntegrityError."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO snapshots (snapshot_id, vin, captured_at, source, json) "
                "VALUES (?, ?, ?, ?, ?)",
                (s.snapshot_id, s.vin, _iso(s.captured_at), s.source.value, s.model_dump_json()),
            )

    def get_snapshot(self, snapshot_id: str) -> Snapshot:
        return Snapshot.model_validate_json(
            self._by_id("snapshots", "snapshot_id", snapshot_id, "snapshot")
        )

    def latest_snapshot(self, vin: str | None = None) -> Snapshot | None:
        """The most recently stored snapshot, optionally for one VIN."""
        if vin is None:
            row = self._conn.execute(
                "SELECT json FROM snapshots ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT json FROM snapshots WHERE vin = ? ORDER BY rowid DESC LIMIT 1", (vin,)
            ).fetchone()
        return Snapshot.model_validate_json(row[0]) if row else None

    # ---- profiles -------------------------------------------------------------

    def save_profile(self, p: VehicleProfile) -> None:
        """Upsert by VIN; a profile without a VIN is not stored."""
        if p.vin is None:
            return
        with self._conn:
            self._conn.execute(
                "INSERT INTO vehicles (vin, profile_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(vin) DO UPDATE SET profile_json = excluded.profile_json, "
                "updated_at = excluded.updated_at",
                (p.vin, p.model_dump_json(), _iso(self._clock())),
            )

    def get_profile(self, vin: str) -> VehicleProfile | None:
        row = self._conn.execute(
            "SELECT profile_json FROM vehicles WHERE vin = ?", (vin,)
        ).fetchone()
        return VehicleProfile.model_validate_json(row[0]) if row else None

    # ---- reports --------------------------------------------------------------

    def save_report(self, r: Report, md_path: Path | None, html_path: Path | None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO reports (report_id, snapshot_id, run_id, mode, generated_at, json, "
                "md_path, html_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r.report_id,
                    r.snapshot_id,
                    r.run_id,
                    r.mode.value,
                    _iso(r.generated_at),
                    r.model_dump_json(),
                    str(md_path) if md_path else None,
                    str(html_path) if html_path else None,
                ),
            )

    def get_report(self, report_id: str) -> Report:
        return Report.model_validate_json(self._by_id("reports", "report_id", report_id, "report"))

    def latest_report(self) -> Report | None:
        row = self._conn.execute("SELECT json FROM reports ORDER BY rowid DESC LIMIT 1").fetchone()
        return Report.model_validate_json(row[0]) if row else None

    def list_reports(self, limit: int = 20) -> list[ReportRow]:
        rows = self._conn.execute(
            "SELECT report_id, snapshot_id, run_id, mode, generated_at, md_path, html_path "
            "FROM reports ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            ReportRow(
                report_id=row[0],
                snapshot_id=row[1],
                run_id=row[2],
                mode=Mode(row[3]),
                generated_at=datetime.fromisoformat(row[4]),
                md_path=Path(row[5]) if row[5] else None,
                html_path=Path(row[6]) if row[6] else None,
            )
            for row in rows
        ]

    # ---- runs -----------------------------------------------------------------

    def save_run(self, run: Run) -> None:
        """Upsert: a run is saved again when it finishes."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs (run_id, started_at, total_usd, stop_reason, json) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
                "started_at = excluded.started_at, total_usd = excluded.total_usd, "
                "stop_reason = excluded.stop_reason, json = excluded.json",
                (
                    run.run_id,
                    _iso(run.started_at),
                    run.total_usd,
                    run.stop_reason.value if run.stop_reason else None,
                    run.model_dump_json(),
                ),
            )

    def get_run(self, run_id: str) -> Run:
        return Run.model_validate_json(self._by_id("runs", "run_id", run_id, "run"))

    # ---- NhtsaCache -----------------------------------------------------------

    def get(self, key: str, max_age: timedelta | None) -> Any | None:
        """The cached value, or None if absent or older than `max_age` (None = never expires)."""
        row = self._conn.execute(
            "SELECT fetched_at, json FROM nhtsa_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if max_age is not None and self._clock() - datetime.fromisoformat(row[0]) > max_age:
            return None
        return json.loads(row[1])

    def get_stale(self, key: str) -> Any | None:
        row = self._conn.execute("SELECT json FROM nhtsa_cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: Any) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO nhtsa_cache (key, fetched_at, json) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET fetched_at = excluded.fetched_at, "
                "json = excluded.json",
                (key, _iso(self._clock()), json.dumps(value)),
            )

    # ---- helpers --------------------------------------------------------------

    def _by_id(self, table: str, column: str, wanted: str, noun: str) -> str:
        """The JSON of one row, by full id or by a unique prefix of at least 6 characters.

        `table` and `column` are constants from this module, never user input.
        """
        row = self._conn.execute(
            f"SELECT json FROM {table} WHERE {column} = ?",
            (wanted,),
        ).fetchone()
        if row is not None:
            return row[0]
        if len(wanted) < MIN_PREFIX:
            raise NotFoundError(
                f"No {noun} with id {wanted!r}. A prefix needs at least {MIN_PREFIX} characters."
            )
        escaped = wanted.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._conn.execute(
            f"SELECT json FROM {table} WHERE {column} LIKE ? ESCAPE '\\' LIMIT 2",
            (escaped + "%",),
        ).fetchall()
        if not rows:
            raise NotFoundError(f"No {noun} with an id starting with {wanted!r}.")
        if len(rows) > 1:
            raise NotFoundError(
                f"Several {noun}s have an id starting with {wanted!r}; use more of it."
            )
        return rows[0][0]
