"""Wal33D VW hints (SPEC §5.3): VW only, P1 only, year caution, never OTHER."""

import json
from pathlib import Path

import pytest

from obdient.knowledge.obdex import ObdexIndex
from obdient.knowledge.wal33d import (
    VW_MAKES,
    WAL33D_CAUTION,
    WAL33D_LABEL,
    WAL33D_PATH,
    lookup_hint,
)
from support import REPO

DATA = REPO / "data" / "wal33d"


def test_vendored_subset_is_vw_p1_only() -> None:
    table = json.loads(WAL33D_PATH.read_text(encoding="utf-8"))
    assert len(table) == 528
    assert (min(table), max(table)) == ("P1101", "P1866")
    assert all(code.startswith("P1") and len(code) == 5 for code in table)
    assert all(isinstance(text, str) and text for text in table.values())


def test_only_the_vw_file_its_license_and_notes_are_vendored() -> None:
    assert sorted(p.name for p in DATA.iterdir()) == [
        "LICENSE",
        "SOURCE.md",
        "volkswagen_codes.txt",
        "vw.json",
    ]
    assert "MIT License" in (DATA / "LICENSE").read_text()
    source = (DATA / "SOURCE.md").read_text()
    assert "04c43d72e7db7197658b6f72fe582c5076d9eee8" in source and "OTHER" in source


def test_vw_json_matches_the_source_file_line_for_line() -> None:
    table = json.loads(WAL33D_PATH.read_text(encoding="utf-8"))
    lines = (DATA / "volkswagen_codes.txt").read_text(encoding="ascii").splitlines()
    assert [f"{code} - {text}" for code, text in sorted(table.items())] == sorted(lines)


def test_hint_for_an_older_vw_has_no_caution() -> None:
    hint = lookup_hint("VOLKSWAGEN", 2003, "P1136")
    assert hint is not None
    assert hint.code == "P1136" and "Fuel Trim" in hint.description
    assert hint.label == WAL33D_LABEL == "Unverified community definition (Wal33D, MIT)."
    assert hint.caution is None
    assert lookup_hint("VW", 2004, "p1136") == hint.model_copy()  # make alias, lower-case code


@pytest.mark.parametrize("year", [2005, 2018, None])
def test_caution_from_2005_and_for_unknown_year(year: int | None) -> None:
    hint = lookup_hint("Volkswagen", year, "P1136")
    assert hint is not None
    assert hint.caution == WAL33D_CAUTION
    assert hint.caution.startswith("Caution: VW largely moved to standardized codes after 2004")


@pytest.mark.parametrize("make", ["AUDI", "FORD", "OTHER", "", None, "VOLKSWAGEN GROUP"])
def test_other_makes_get_nothing(make: str | None) -> None:
    assert lookup_hint(make, 2003, "P1136") is None
    assert frozenset({"VOLKSWAGEN", "VW"}) == VW_MAKES


@pytest.mark.parametrize("code", ["P0171", "P1999", "P2187", "U1101", "B1101", ""])
def test_non_p1_and_unknown_codes_get_nothing(code: str) -> None:
    assert lookup_hint("VOLKSWAGEN", 2003, code) is None


def test_no_vendored_code_is_also_an_obdex_code(obdex: ObdexIndex) -> None:
    """OBDex has no P1xxx codes, so a hint can never shadow or contradict a definition."""
    table = json.loads(WAL33D_PATH.read_text(encoding="utf-8"))
    assert [code for code in table if obdex.lookup_dtc(code) is not None] == []


def test_a_custom_table_path(tmp_path: Path) -> None:
    path = tmp_path / "vw.json"
    path.write_text(json.dumps({"P1ABC": "Test"}), encoding="utf-8")
    hint = lookup_hint("VW", 1999, "P1ABC", path=path)
    assert hint is not None and hint.description == "Test"
