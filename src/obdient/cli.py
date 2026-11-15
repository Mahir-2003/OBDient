"""Typer entry point (SPEC §13). `scan` so far; later milestones add the rest."""

import logging
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import httpx
import typer

from obdient import __version__
from obdient.agent.safety import RECALLS_NOTE, VIN_LOOKUP_URL
from obdient.config import load_settings
from obdient.errors import (
    NhtsaUnavailable,
    ObdientError,
    UnsupportedProtocolError,
    VehicleConnectionError,
)
from obdient.knowledge.nhtsa import NhtsaClient, NhtsaResolver, NhtsaResult, VinDecode
from obdient.schemas.common import Condition, SnapshotSource
from obdient.schemas.scenario import resolve_scenario
from obdient.schemas.snapshot import DtcStatus, Snapshot, UserReport
from obdient.storage.db import Database
from obdient.userio import MAX_SYMPTOMS, CliConditionPrompter, run_intake
from obdient.vehicle.base import Vehicle
from obdient.vehicle.collector import collect, normalize_vin
from obdient.vehicle.elm327 import ELM327Vehicle
from obdient.vehicle.simulator import ScenarioSimulator
from obdient.vehicle.transports.tcp import TcpTransport


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1  # message on stderr
    USAGE = 2  # Typer
    NO_CONNECTION = 3  # could not connect to adapter or vehicle
    INCOMPLETE = 4  # report produced but incomplete (fallback)


@dataclass
class AppState:
    """Global options, stored on the Typer context for commands to read."""

    home: Path | None
    verbose: bool


app = typer.Typer(
    name="obdient",
    help="Read-only OBD-II diagnostic agent.",
    no_args_is_help=True,
    add_completion=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"obdient {__version__}")
        raise typer.Exit(ExitCode.OK)


@app.callback()
def main(
    ctx: typer.Context,
    home: Annotated[
        Path | None,
        typer.Option("--home", help="Data directory (default $OBDIENT_HOME or ~/.obdient)."),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Debug logging to stderr.")
    ] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Print version and exit."
        ),
    ] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    ctx.obj = AppState(home=home, verbose=verbose)


# ---- scan (SPEC §13.1) -------------------------------------------------------


class ScanSource(StrEnum):
    """Sources `scan` can use so far; `ble` and `replay` arrive in M8."""

    SIMULATOR = "simulator"
    EMULATOR = "emulator"


class ScanCondition(StrEnum):
    IDLE = "idle"
    HOLD_2500 = "hold_2500"
    COLD_START = "cold_start"
    WARM = "warm"


def _usage_error(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(ExitCode.USAGE)


def mask_vin(vin: str | None) -> str:
    """First 3 and last 4 characters; the rest as asterisks."""
    if vin is None:
        return "not read"
    if len(vin) <= 7:
        return "*" * len(vin)
    return vin[:3] + "*" * (len(vin) - 7) + vin[-4:]


def _summary(snapshot: Snapshot) -> str:
    codes = {
        status: sorted({d.code for d in snapshot.dtcs if d.status is status})
        for status in DtcStatus
    }
    supported = [m for m in snapshot.readiness if m.supported]
    incomplete = [m.name.value for m in supported if m.complete is False]
    mil = {True: "on", False: "off", None: "not read"}[snapshot.mil_on]
    lines = [
        f"Snapshot {snapshot.snapshot_id}",
        f"  Source:      {snapshot.source.value}",
        f"  VIN:         {mask_vin(snapshot.vin)}",
        f"  MIL:         {mil}",
        "  Codes:       "
        + "; ".join(
            f"{status.value}: {', '.join(found) if found else 'none'}"
            for status, found in codes.items()
        ),
        f"  Readiness:   {len(supported) - len(incomplete)} of {len(supported)} supported "
        "monitors complete" + (f" (incomplete: {', '.join(incomplete)})" if incomplete else ""),
        "  Conditions:  " + (", ".join(c.value for c in snapshot.conditions_captured) or "none"),
        f"  Readings:    {len(snapshot.readings)}",
        f"  Read errors: {len(snapshot.read_errors)}",
    ]
    lines += [
        f"    {e.kind.value} on {e.request}" + (f": {e.detail}" if e.detail else "")
        for e in snapshot.read_errors
    ]
    return "\n".join(lines)


@app.command()
def scan(
    ctx: typer.Context,
    source: Annotated[
        ScanSource | None,
        typer.Option("--source", help="Which vehicle to read. Inferred with --scenario."),
    ] = None,
    scenario_ref: Annotated[
        str | None,
        typer.Option("--scenario", help="Scenario path, id or id prefix (simulator)."),
    ] = None,
    scenarios_dir: Annotated[
        Path, typer.Option("--scenarios-dir", help="Where scenario ids are resolved.")
    ] = Path("scenarios"),
    host: Annotated[str, typer.Option("--host", help="Emulator TCP host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Emulator TCP port.")] = 35000,
    condition: Annotated[
        list[ScanCondition] | None,
        typer.Option("--condition", help="Repeatable; captured in the order given."),
    ] = None,
    vin: Annotated[str | None, typer.Option("--vin", help="Override the VIN.")] = None,
    symptoms: Annotated[
        str | None, typer.Option("--symptoms", help="What you noticed; skips that question.")
    ] = None,
    no_input: Annotated[
        bool, typer.Option("--no-input", help="Skip intake and condition prompts.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the Snapshot JSON instead of the summary.")
    ] = False,
) -> None:
    """Read the vehicle once and store the result as a Snapshot. Nothing is written to the car."""
    state: AppState = ctx.obj
    if source is None:
        if scenario_ref is None:
            _usage_error("Give --source, or --scenario to use the simulator.")
        source = ScanSource.SIMULATOR
    if source is ScanSource.SIMULATOR and scenario_ref is None:
        _usage_error("--source simulator requires --scenario.")
    if source is not ScanSource.SIMULATOR and scenario_ref is not None:
        _usage_error(f"--scenario requires --source simulator, not {source.value}.")
    conditions = tuple(Condition(c.value) for c in condition or ()) or (Condition.UNKNOWN,)
    if Condition.COLD_START in conditions and Condition.WARM in conditions:
        _usage_error("--condition cold_start and --condition warm cannot be combined.")

    if symptoms is not None:
        symptoms = symptoms.strip()[:MAX_SYMPTOMS]
    if vin is not None:
        try:
            vin = normalize_vin(vin)
        except ValueError as exc:
            _usage_error(str(exc))

    try:
        settings = load_settings(home=state.home)
        scenario = (
            resolve_scenario(scenario_ref, scenarios_dir) if scenario_ref is not None else None
        )
        vehicle: Vehicle
        if scenario is not None:
            vehicle = ScenarioSimulator(scenario)
            report = scenario.build_user_report()  # the scenario always answers intake
            if symptoms is not None:
                report = UserReport.model_validate(
                    {**report.model_dump(), "symptoms_text": symptoms}
                )
        else:
            vehicle = ELM327Vehicle(TcpTransport(host, port), SnapshotSource.EMULATOR)
            report = UserReport(symptoms_text=symptoms or "")
        interactive = scenario is None and not no_input
        snapshot = collect(
            vehicle,
            report,
            conditions=conditions,
            prompter=CliConditionPrompter() if interactive else None,
            vin_override=vin,
        )
        if interactive:
            # Asked after the read, so the flashing-light question knows whether the MIL is on.
            try:
                answers = run_intake(
                    lambda q: typer.prompt(q, default="", show_default=False, prompt_suffix=" "),
                    snapshot.mil_on,
                    symptoms,
                )
            except (typer.Abort, EOFError):
                # The car has been read; do not lose the scan because the questions were cut short.
                typer.echo("Intake questions skipped.", err=True)
            else:
                snapshot = snapshot.model_copy(update={"user_report": answers})
        database = Database(settings.db_path)
        try:
            database.save_snapshot(snapshot)
        finally:
            database.close()
    except (VehicleConnectionError, UnsupportedProtocolError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(ExitCode.NO_CONNECTION) from exc
    except ObdientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(ExitCode.ERROR) from exc

    typer.echo(snapshot.model_dump_json(indent=2) if as_json else _summary(snapshot))


# ---- nhtsa (SPEC §13.5) ------------------------------------------------------


def _print_nhtsa(result: NhtsaResult, decoded: VinDecode | None, complaints: int) -> None:
    names = result.candidate_names
    tried = ", ".join(names)
    if decoded is not None:
        vehicle = " ".join(
            str(part) for part in (decoded.model_year, decoded.make, decoded.model) if part
        )
        engine = f" ({decoded.engine})" if decoded.engine else ""
        typer.echo(f"Vehicle: {vehicle or 'not identified'}{engine}")
    typer.echo(f"Names tried: {tried}")
    for name in names:
        recalls_n = sum(1 for r in result.recalls if name in r.matched_names)
        complaints_n = sum(1 for c in result.complaints if name in c.matched_names)
        typer.echo(f"  {name}: {recalls_n} recalls, {complaints_n} complaints")
    typer.echo("")
    if result.recalls:
        typer.echo(f"Recalls: {len(result.recalls)}")
        for recall in result.recalls:
            typer.echo(
                f"  {recall.campaign_number}  {recall.report_date or 'date unknown'}  "
                f"{recall.component}  [found under: {', '.join(recall.matched_names)}]"
            )
    elif result.failed("recalls") >= len(names):
        typer.echo("Recalls: lookup failed; nothing is known either way.")
    else:
        typer.echo(f"Recalls: none found under names: {tried}. This is not proof that none exist.")
    typer.echo(RECALLS_NOTE)
    typer.echo(VIN_LOOKUP_URL)
    typer.echo("")
    if result.complaints:
        typer.echo(f"Owner complaints (unverified): {len(result.complaints)}")
        for complaint in result.complaints[:complaints]:
            typer.echo(
                f"  {complaint.odi_number}  {complaint.date_filed or 'date unknown'}  "
                f"{', '.join(complaint.components)}"
            )
            typer.echo(f"    {complaint.summary[:300]}")
    elif result.failed("complaints") >= len(names):
        typer.echo("Owner complaints: lookup failed; nothing is known either way.")
    else:
        typer.echo(f"Owner complaints: none found under names: {tried}.")
    for error in result.errors:
        typer.echo(f"Lookup failed: {error}", err=True)


@app.command()
def nhtsa(
    ctx: typer.Context,
    vin: Annotated[
        str | None, typer.Option("--vin", help="Decode with vPIC, then resolve.")
    ] = None,
    make: Annotated[str | None, typer.Option("--make", help="With --model and --year.")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    year: Annotated[int | None, typer.Option("--year")] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Ignore cache ages for this call.")
    ] = False,
    offline: Annotated[bool, typer.Option("--offline", help="Cache only.")] = False,
    complaints: Annotated[
        int, typer.Option("--complaints", min=0, help="Print the newest N complaint summaries.")
    ] = 0,
    as_json: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Show what NHTSA returns for a vehicle, and under which model names. No model calls."""
    state: AppState = ctx.obj
    given = (make is not None, model is not None, year is not None)
    if vin is None and not all(given):
        _usage_error("Give --vin, or --make, --model and --year together.")
    if vin is not None and any(given):
        _usage_error("Give either --vin or --make/--model/--year, not both.")
    if refresh and offline:
        _usage_error("--refresh and --offline cannot be combined.")
    if vin is not None:
        try:
            vin = normalize_vin(vin)
        except ValueError as exc:
            _usage_error(str(exc))

    settings = load_settings(home=state.home)
    database = Database(settings.db_path)
    http = None if offline else httpx.Client()
    try:
        client = NhtsaClient(
            database,
            http,
            delay_s=settings.nhtsa_delay_s,
            timeout_s=settings.nhtsa_timeout_s,
            offline=offline,
            refresh=refresh,
        )
        decoded: VinDecode | None = None
        if vin is not None:
            decoded = client.decode_vin(vin)
            make, model, year = decoded.make, decoded.model, decoded.model_year
            if make is None or model is None or year is None:
                typer.echo(
                    "vPIC could not identify the make, model and year from that VIN.", err=True
                )
                raise typer.Exit(ExitCode.ERROR)
        assert make is not None and model is not None and year is not None
        result = NhtsaResolver(client, settings.nhtsa_products_enabled).resolve(make, model, year)
    except NhtsaUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(ExitCode.ERROR) from exc
    finally:
        database.close()
        if http is not None:
            http.close()

    if as_json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _print_nhtsa(result, decoded, complaints)
    if result.errors and not result.recalls and not result.complaints:
        raise typer.Exit(ExitCode.ERROR)
