"""OBDex index (SPEC §5.2): counts, field mapping, the tier rule, and a PID cross-check."""

import ast
import operator
import re
import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from obdient.config import OBDEX_COMMIT, Settings
from obdient.errors import IndexNotBuiltError, ObdientError
from obdient.knowledge import obdex as obdex_module
from obdient.knowledge.obdex import (
    MANUFACTURER_DOMAINS,
    ObdexIndex,
    build_index,
    ensure_index,
    index_is_current,
    is_tier1_source,
    tier_for,
)
from obdient.schemas.common import Tier
from obdient.vehicle.pids import PID_TABLE
from support import OBDEX_DIR


def test_the_vendored_snapshot_is_complete() -> None:
    assert len(list((OBDEX_DIR / "generic").glob("*.yaml"))) == 7
    assert sorted(p.name for p in (OBDEX_DIR / "pids").glob("*.yaml")) == [
        "mode01.yaml",
        "mode09.yaml",
    ]
    assert (OBDEX_DIR / "LICENSE-DATA").read_text().startswith("Creative Commons Legal Code")
    assert "MIT License" in (OBDEX_DIR / "LICENSE-CODE").read_text()
    assert "9ba1ffa095f3140d9e21b985b322ddea3134bb60" in (OBDEX_DIR / "SOURCE.md").read_text()


def test_stats(obdex: ObdexIndex) -> None:
    stats = obdex.stats()
    assert stats.codes == 9533
    assert stats.commit == OBDEX_COMMIT == "9ba1ffa"
    assert stats.pids == 132
    assert stats.tier1 + stats.tier2 == 9533
    assert (stats.tier1, stats.tier2) == (7911, 1622)  # changes only with MANUFACTURER_DOMAINS


def test_p0171_acceptance(obdex: ObdexIndex) -> None:
    definition = obdex.lookup_dtc("P0171")
    assert definition is not None
    assert definition.title == "System Too Lean Bank 1"
    assert len(definition.causes) == 5
    assert definition.causes[0].text.startswith("Air leak after the air flow sensor")
    assert [c.likelihood for c in definition.causes] == ["high", "high", "medium", "medium", "low"]
    assert definition.tier is Tier.T1
    assert definition.flags.model_dump() == {
        "mil": True,
        "emissions": True,
        "drive_cycle": True,
        "limp_mode": False,
    }
    assert definition.review_date == date(2026, 10, 9)
    assert "P0174" in definition.related_codes
    assert len(definition.source_urls) == 8
    assert len(definition.symptoms) == 4 and all(definition.symptoms)
    assert "Kraftstoff" not in definition.description  # English only


def test_p0420_is_tier_1_through_manufacturer_documents_alone(obdex: ObdexIndex) -> None:
    definition = obdex.lookup_dtc("P0420")
    assert definition is not None
    assert not any("/odi/tsbs/" in url for url in definition.source_urls)
    assert any("gsi.ext.gm.com" in url for url in definition.source_urls)
    assert definition.tier is Tier.T1


def test_lookup_is_case_insensitive_and_misses_return_none(obdex: ObdexIndex) -> None:
    assert obdex.lookup_dtc("p0171") == obdex.lookup_dtc("P0171")
    assert obdex.lookup_dtc("P1136") is None  # manufacturer codes are out of OBDex's scope
    assert obdex.lookup_dtc("nonsense") is None


def test_entry_without_flags_or_causes_loads_with_unknown_flags(obdex: ObdexIndex) -> None:
    definition = obdex.lookup_dtc("P0FFF")
    assert definition is not None
    assert definition.causes == [] and definition.symptoms == []
    assert set(definition.flags.model_dump().values()) == {None}


def test_pid_lookup(obdex: ObdexIndex) -> None:
    rpm = obdex.lookup_pid("010C")
    assert rpm is not None
    assert (rpm.name, rpm.unit, rpm.description) == (
        "Engine RPM",
        "rpm",
        "Formula: (256*A + B) / 4",
    )
    assert (rpm.typical_min, rpm.typical_max) == (0.0, 16383.75)
    vin = obdex.lookup_pid("0902")
    assert vin is not None and vin.typical_min is None and vin.description == ""
    assert obdex.lookup_pid("01FF") is None


# ---- tier rule ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://static.nhtsa.gov/odi/tsbs/2015/MC-10114925-9999.pdf",
        "https://nhtsa.gov/odi/tsbs/x.pdf",
        "https://gsi.ext.gm.com/gmspo/mode6/pdf/2018/18OBDG03A%20Engine%20Diagnostics.pdf",
        "https://www.fordservicecontent.com/Ford_Content/catalog/motorcraft/obd.pdf",
        "https://WWW.BOSCH-MOBILITY.COM/en/solutions/",
    ],
)
def test_tier_1_sources(url: str) -> None:
    assert is_tier1_source(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://static.nhtsa.gov/odi/inv/2019/INRD-EA19001-1234.pdf",  # NHTSA, but not a bulletin
        "https://static.nhtsa.gov/odi/rcl/2016/RCRIT-16V647-1179.pdf",
        "https://example.com/odi/tsbs/fake.pdf",  # the path alone is not enough
        "https://www.law.cornell.edu/regulations/california/13-CCR-1968.2",
        "https://en.wikipedia.org/wiki/OBD-II_PIDs",
        "https://patents.google.com/patent/US6128948A/en",
        "https://saabwisonline.com/9-3-9440/2007/dtcs/me9/p0101-00",  # third-party mirror
        "https://www.newtonnet.co.uk/fiesta/fordobd2012.pdf",
        "https://www.ti.com/lit/pdf/slva824",  # chip datasheet
        "https://media.audiusa.com/en-us/releases/1",  # press release
        "https://evil.example/?u=gsi.ext.gm.com",
        "not a url",
        "",
    ],
)
def test_tier_2_sources(url: str) -> None:
    assert not is_tier1_source(url)


def test_tier_for_needs_only_one_tier_1_source() -> None:
    wiki = "https://en.wikipedia.org/wiki/X"
    assert tier_for([wiki, "https://www.hella.com/doc.pdf"]) is Tier.T1
    assert tier_for([wiki]) is Tier.T2
    assert tier_for([]) is Tier.T2


def test_manufacturer_domains_exclude_reference_and_regulator_sites() -> None:
    assert len(set(MANUFACTURER_DOMAINS)) == len(MANUFACTURER_DOMAINS)
    banned = ("wikipedia", "cornell", "patents", "uspto", "epo.org", "europa.eu", "saabwis")
    assert not [d for d in MANUFACTURER_DOMAINS if any(b in d for b in banned)]
    assert all(d == d.lower() and "/" not in d for d in MANUFACTURER_DOMAINS)


def test_every_manufacturer_domain_is_present_in_the_snapshot(obdex_index_path: Path) -> None:
    """The list is seeded from the snapshot; an entry nothing cites is a typo."""
    import sqlite3

    with sqlite3.connect(obdex_index_path) as conn:
        blob = " ".join(row[0] for row in conn.execute("SELECT json FROM dtc"))
    assert [d for d in MANUFACTURER_DOMAINS if f"//{d}/" not in blob] == []


# ---- building ----------------------------------------------------------------


def _mini(tmp_path: Path, drop_code: bool = False, files: int = 7) -> Path:
    """A 7-file snapshot with one code per file."""
    src = tmp_path / "obdex"
    (src / "generic").mkdir(parents=True)
    (src / "pids").mkdir()
    for n in range(files):
        entry = {
            "code": f"P000{n}",
            "title": {"en": f"Title {n}", "de": "Titel"},
            "description": {"en": "Text", "de": "Text"},
            "common_causes": [{"id": "x", "likelihood": "certain", "label": {"en": "Cause"}}],
            "flags": {"limp_mode_possible": True},
            "sources": ["https://en.wikipedia.org/wiki/X"],
            "reviewed": "not a date",
        }
        entries = [] if (drop_code and n == 0) else [entry]
        (src / "generic" / f"F{n}.yaml").write_text(yaml.safe_dump(entries), encoding="utf-8")
    (src / "pids" / "mode01.yaml").write_text(
        yaml.safe_dump([{"pid": "0c", "mode": "01", "name": {"en": "RPM"}, "unit": "rpm"}])
    )
    return src


def test_wrong_code_count_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "index.sqlite"
    with pytest.raises(ObdientError, match="must contain 9533 codes; loaded 7"):
        build_index(_mini(tmp_path), out)
    assert not out.exists() and not list(tmp_path.glob("*.partial"))

    monkeypatch.setitem(obdex_module.EXPECTED_CODES, "test", 7)
    with pytest.raises(ObdientError, match="must contain 7 codes; loaded 6"):
        build_index(_mini(tmp_path / "short", drop_code=True), out, commit="test")


def test_missing_family_file_and_unknown_commit(tmp_path: Path) -> None:
    with pytest.raises(ObdientError, match="Expected 7 OBDex family files"):
        build_index(_mini(tmp_path, files=6), tmp_path / "index.sqlite")
    with pytest.raises(ObdientError, match="No expected code count"):
        build_index(_mini(tmp_path / "b"), tmp_path / "index.sqlite", commit="abc1234")


def test_small_build_maps_unknown_values_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(obdex_module.EXPECTED_CODES, "test", 7)
    out = tmp_path / "nested" / "index.sqlite"
    stats = build_index(_mini(tmp_path), out, commit="test")
    assert (stats.codes, stats.pids, stats.tier1, stats.tier2) == (7, 1, 0, 7)

    index = ObdexIndex(out)
    definition = index.lookup_dtc("P0003")
    assert definition is not None
    assert definition.causes[0].likelihood is None  # "certain" is not high / medium / low
    assert definition.review_date is None
    assert definition.flags.limp_mode is True and definition.flags.mil is None
    assert definition.tier is Tier.T2
    assert index.lookup_pid("010C") is not None  # pid upper-cased
    assert index.stats() == stats
    index.close()


def test_duplicate_code_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(obdex_module.EXPECTED_CODES, "test", 7)
    src = _mini(tmp_path)
    shutil.copy(src / "generic" / "F0.yaml", src / "generic" / "F6.yaml")
    with pytest.raises(ObdientError, match="code P0000 appears twice"):
        build_index(src, tmp_path / "index.sqlite", commit="test")


def test_missing_index_raises(tmp_path: Path) -> None:
    with pytest.raises(IndexNotBuiltError, match="OBDex index not found"):
        ObdexIndex(tmp_path / "nope.sqlite")


def test_ensure_index_builds_only_when_missing(
    settings: Settings, obdex_index_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []

    def fake_build(src: Path, out: Path) -> None:
        calls.append(src)
        shutil.copy(obdex_index_path, out)

    monkeypatch.setattr(obdex_module, "build_index", fake_build)
    assert f"obdex-{OBDEX_COMMIT}" in settings.obdex_index_path.name
    assert ensure_index(settings) == settings.obdex_index_path
    assert ensure_index(settings) == settings.obdex_index_path
    assert calls == [settings.data_dir / "obdex"]


def test_index_is_rebuilt_when_the_tier_list_changes(
    settings: Settings, obdex_index_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file name only embeds the commit; a fingerprint inside catches everything else."""
    calls: list[Path] = []

    def fake_build(src: Path, out: Path) -> None:
        calls.append(out)

    shutil.copy(obdex_index_path, settings.obdex_index_path)
    assert index_is_current(settings.obdex_index_path)
    monkeypatch.setattr(obdex_module, "build_index", fake_build)
    ensure_index(settings)
    assert calls == []

    monkeypatch.setattr(obdex_module, "MANUFACTURER_DOMAINS", ("only.example",))
    assert not index_is_current(settings.obdex_index_path)
    ensure_index(settings)
    assert calls == [settings.obdex_index_path]


def test_a_missing_or_corrupt_file_is_not_current(tmp_path: Path) -> None:
    assert not index_is_current(tmp_path / "nope.sqlite")
    junk = tmp_path / "junk.sqlite"
    junk.write_text("not a database")
    assert not index_is_current(junk)


# ---- cross-check of our own PID table against OBDex (PRD D1, SPEC V5) --------

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def _evaluate(formula: str, data: bytes) -> float:
    """Evaluate an OBDex formula such as "(256*A + B) / 4": arithmetic on A, B, C, D only."""
    names = dict(zip("ABCD", data, strict=False))

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id in names:
            return float(names[node.id])
        raise ValueError(f"unsupported formula: {formula}")

    return walk(ast.parse(formula, mode="eval").body)


# Where OBDex and SPEC §0.4 name the same unit differently.
_UNIT_NAMES = {"ratio": "λ", "° before TDC": "°"}


def _mode01() -> dict[str, dict[str, object]]:
    entries = yaml.safe_load((OBDEX_DIR / "pids" / "mode01.yaml").read_text(encoding="utf-8"))
    return {f"01{e['pid']}": e for e in entries}


def test_pid_byte_lengths_agree_with_obdex() -> None:
    """Every PID both tables know must have the same data length (V5)."""
    theirs = _mode01()
    shared = sorted(set(PID_TABLE) & set(theirs))
    assert len(shared) >= 80
    assert {
        p: (PID_TABLE[p].length, theirs[p]["bytes"])
        for p in shared
        if PID_TABLE[p].length != theirs[p]["bytes"]
    } == {}


def test_decoder_formulas_and_units_agree_with_obdex() -> None:
    theirs = _mode01()
    samples = [
        bytes([0x00] * 4),
        bytes([0x7F, 0x80, 0x01, 0xFE]),
        bytes([0xFF] * 4),
        bytes([0x14, 0x5F, 0x33, 0x99]),
    ]
    checked = 0
    for pid, spec in PID_TABLE.items():
        entry = theirs.get(pid)
        if spec.decode is None or entry is None or "formula" not in entry:
            continue
        # "A/200 (voltage); 100/128 * B - 100 (trim)" -> "A/200": the first value only
        formula = re.sub(r"\s*\([a-z][^()]*\)$", "", str(entry["formula"]).split(";")[0])
        for data in samples:
            ours = spec.decode(data[: spec.length])
            assert isinstance(ours, float | int)
            assert ours == pytest.approx(_evaluate(formula, data)), (pid, formula)
        their_unit = str(entry["unit"]).split(";")[0].strip()  # "V; %" -> the first value's
        assert spec.unit == _UNIT_NAMES.get(their_unit, their_unit), pid
        checked += 1
    assert checked == 30  # every decoder we have
