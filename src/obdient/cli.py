"""Typer entry point (SPEC §13). Commands are added by later milestones."""

import logging
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Annotated

import typer

from obdient import __version__


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
