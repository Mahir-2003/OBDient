"""OBDex: the generic trouble-code and PID reference (SPEC §5.1, §5.2).

The vendored snapshot lives in `data/obdex/` (see its SOURCE.md). It is compiled once into a
small SQLite index, because parsing 31 MB of YAML takes seconds.

Field mapping, OBDex YAML -> `DtcDefinition` (checked against commit 9ba1ffa):

    code                          -> code
    title.en                      -> title                 (German text is not loaded)
    description.en                -> description
    common_causes[].label.en      -> causes[].text
    common_causes[].likelihood    -> causes[].likelihood   (high | medium | low, else None)
    symptoms[].en                 -> symptoms[]
    flags.mil                     -> flags.mil
    flags.emissions_relevant      -> flags.emissions
    flags.drive_cycle_required    -> flags.drive_cycle
    flags.limp_mode_possible      -> flags.limp_mode
    related_codes                 -> related_codes
    sources                       -> source_urls           (plain URLs; there is no type field)
    reviewed                      -> review_date

Not loaded: `category`, `affected_components`, `references`, and `repair` (euros, hours,
difficulty; PRD D3). One entry, P0FFF, has no causes, symptoms or flags; its flags are None.

PID files (`pids/mode01.yaml`, `pids/mode09.yaml`) -> `PidInfo`: `mode` + `pid` -> pid,
`name.en` -> name, `formula` -> description, `unit` -> unit, `range` -> typical_min / max
(the encodable range, not a healthy range).
"""

import hashlib
import json
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import yaml

from obdient.config import OBDEX_COMMIT, Settings
from obdient.errors import IndexNotBuiltError, ObdientError
from obdient.schemas.common import Model, Tier

# Codes each pinned commit must contain; a different count means a bad or partial copy.
EXPECTED_CODES: dict[str, int] = {"9ba1ffa": 9533}
GENERIC_FILES = 7

NHTSA_HOSTS = frozenset({"static.nhtsa.gov", "nhtsa.gov", "www.nhtsa.gov"})
TSB_PATH = "/odi/tsbs/"

# Hosts whose documents count as manufacturer or supplier documents (tier 1). Seeded from the
# hosts present in the 9ba1ffa snapshot and reviewed by hand. In: vehicle and engine makers'
# own service, technical and owner-manual hosts, and automotive suppliers' own hosts. Out, on
# purpose: Wikipedia, regulators and law sites, patent offices, press and media sites, trade
# magazines, semiconductor vendors' chip datasheets, aftermarket parts sellers, and third
# parties re-hosting a maker's document (saabwisonline.com, newtonnet.co.uk).
MANUFACTURER_DOMAINS: tuple[str, ...] = (
    # vehicle and engine makers
    "gsi.ext.gm.com",
    "gm-techlink.com",
    "www.gmupfitter.com",
    "www.gmstc.com",
    "www.gmc.com",
    "www.fordservicecontent.com",
    "www.performanceparts.ford.com",
    "performanceparts.ford.com",
    "content.fordpro.com",
    "www.ford.com",
    "www.ford.ca",
    "mitsubishitechinfo.com",
    "techinfo.toyota.com",
    "vehicleinfo.mopar.com",
    "www.rambodybuilder.com",
    "www.mbusa.com",
    "www.honda.ca",
    "www.audi-technology-portal.de",
    "ask.porsche.com",
    "stpi.it.volvo.com",
    "bodybuilder.navistar.com",
    "serdia.deutz.com",
    "www.roushcleantech.com",
    # automotive suppliers
    "www.bosch-mobility.com",
    "bosch-mobility.com",
    "www.bosch-motorsport.com",
    "www.bosch-engineering.com",
    "www.boschaftermarket.com",
    "mediathek-bosch-automotive.com",
    "www.hella.com",
    "www.borgwarner.com",
    "www.ntn-snr.com",
    "www.ms-motorservice.com",
    "www.rheinmetall.com",
    "www.zf.com",
    "aftermarket.zf.com",
    "www.valeo.com",
    "www.schaeffler.com",
    "www.denso-am.eu",
    "www.mahle-aftermarket.com",
    "www.aumovio.com",
    "www.joysonsafety.com",
    "www.autoliv.com",
    "www.kautex.com",
    "www.wabco-customercentre.com",
    "truck.knorr-bremse.com",
    "www.haldex.com",
    "prod-edam.honeywell.com",
    "www.delphiautoparts.com",
    "www.hyundai-kefico.com",
    "marelli.com",
    "www.brose.com",
    "dealers.webasto.com",
    "www.jtekt.co.jp",
    "www.nsk.com",
    "emitec.com",
    "www.egelhof.com",
)
_MANUFACTURER_HOSTS = frozenset(MANUFACTURER_DOMAINS)

CauseLikelihood = Literal["high", "medium", "low"]


class Cause(Model):
    text: str
    likelihood: CauseLikelihood | None


class DtcFlags(Model):
    mil: bool | None
    emissions: bool | None
    drive_cycle: bool | None
    limp_mode: bool | None


class DtcDefinition(Model):
    code: str
    title: str
    description: str  # English only
    causes: list[Cause]
    symptoms: list[str]
    flags: DtcFlags
    related_codes: list[str]
    source_urls: list[str]
    review_date: date | None
    tier: Tier  # T1 or T2


class PidInfo(Model):
    pid: str
    name: str
    description: str
    unit: str | None
    typical_min: float | None
    typical_max: float | None


class IndexStats(Model):
    codes: int
    pids: int
    tier1: int
    tier2: int
    commit: str


# Bump when the field mapping or the index layout changes.
MAPPING_VERSION = 1


def index_fingerprint(commit: str = OBDEX_COMMIT) -> str:
    """Identifies what an index was built from: the commit, the mapping and the tier list.

    The file name embeds only the commit, so without this a changed `MANUFACTURER_DOMAINS`
    would leave stale tiers in an existing index.
    """
    parts = [commit, str(MAPPING_VERSION), *sorted(MANUFACTURER_DOMAINS), *sorted(NHTSA_HOSTS)]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def is_tier1_source(url: str) -> bool:
    """An NHTSA-hosted service bulletin, or a document on a manufacturer or supplier host."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in NHTSA_HOSTS and TSB_PATH in parts.path:
        return True
    return host in _MANUFACTURER_HOSTS


def tier_for(source_urls: list[str]) -> Tier:
    return Tier.T1 if any(is_tier1_source(url) for url in source_urls) else Tier.T2


def _load(path: Path) -> list[dict[str, Any]]:
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with path.open(encoding="utf-8") as handle:
        data = yaml.load(handle, Loader=loader)
    if not isinstance(data, list):
        raise ObdientError(f"{path}: expected a list of entries")
    return cast(list[dict[str, Any]], data)


def _english(value: object) -> str:
    if isinstance(value, dict):
        text = cast(dict[str, Any], value).get("en")
        return text.strip() if isinstance(text, str) else ""
    return value.strip() if isinstance(value, str) else ""


def _flag(flags: dict[str, Any], key: str) -> bool | None:
    value = flags.get(key)
    return value if isinstance(value, bool) else None


def _review_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _definition(entry: dict[str, Any]) -> DtcDefinition:
    flags = cast(dict[str, Any], entry.get("flags") or {})
    causes: list[Cause] = []
    for cause in cast(list[dict[str, Any]], entry.get("common_causes") or []):
        likelihood = cause.get("likelihood")
        causes.append(
            Cause(
                text=_english(cause.get("label")),
                likelihood=likelihood if likelihood in ("high", "medium", "low") else None,
            )
        )
    sources = [str(url) for url in cast(list[object], entry.get("sources") or [])]
    return DtcDefinition(
        code=str(entry["code"]),
        title=_english(entry.get("title")),
        description=_english(entry.get("description")),
        causes=causes,
        symptoms=[_english(s) for s in cast(list[object], entry.get("symptoms") or [])],
        flags=DtcFlags(
            mil=_flag(flags, "mil"),
            emissions=_flag(flags, "emissions_relevant"),
            drive_cycle=_flag(flags, "drive_cycle_required"),
            limp_mode=_flag(flags, "limp_mode_possible"),
        ),
        related_codes=[str(code) for code in cast(list[object], entry.get("related_codes") or [])],
        source_urls=sources,
        review_date=_review_date(entry.get("reviewed")),
        tier=tier_for(sources),
    )


def _pid_info(entry: dict[str, Any]) -> PidInfo:
    limits = entry.get("range")
    low: float | None = None
    high: float | None = None
    if isinstance(limits, list) and len(cast(list[object], limits)) == 2:
        pair = cast(list[float], limits)
        low, high = float(pair[0]), float(pair[1])
    formula = entry.get("formula")
    unit = entry.get("unit")
    return PidInfo(
        pid=f"{entry['mode']}{entry['pid']}".upper(),
        name=_english(entry.get("name")),
        description=f"Formula: {formula}" if formula else "",
        unit=str(unit) if unit is not None else None,
        typical_min=low,
        typical_max=high,
    )


def build_index(src_dir: Path, out_path: Path, commit: str = OBDEX_COMMIT) -> IndexStats:
    """Compile `src_dir` (the vendored snapshot) into the SQLite index at `out_path`.

    Raises ObdientError unless exactly the expected number of codes loads.
    """
    files = sorted((src_dir / "generic").glob("*.yaml"))
    if len(files) != GENERIC_FILES:
        raise ObdientError(
            f"Expected {GENERIC_FILES} OBDex family files in {src_dir / 'generic'}, "
            f"found {len(files)}. See data/obdex/SOURCE.md."
        )
    definitions: dict[str, DtcDefinition] = {}
    for path in files:
        for entry in _load(path):
            definition = _definition(entry)
            if definition.code in definitions:
                raise ObdientError(f"{path}: code {definition.code} appears twice")
            definitions[definition.code] = definition
    expected = EXPECTED_CODES.get(commit)
    if expected is None:
        raise ObdientError(f"No expected code count is recorded for OBDex commit {commit}.")
    if len(definitions) != expected:
        raise ObdientError(
            f"OBDex commit {commit} must contain {expected} codes; loaded {len(definitions)} "
            f"from {src_dir}. The snapshot is incomplete or the pin is wrong."
        )
    pids = [_pid_info(e) for path in sorted((src_dir / "pids").glob("*.yaml")) for e in _load(path)]
    tier1 = sum(1 for d in definitions.values() if d.tier is Tier.T1)
    stats = IndexStats(
        codes=len(definitions),
        pids=len(pids),
        tier1=tier1,
        tier2=len(definitions) - tier1,
        commit=commit,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_name(out_path.name + ".partial")
    partial.unlink(missing_ok=True)
    conn = sqlite3.connect(partial)
    try:
        conn.executescript(
            "CREATE TABLE dtc (code TEXT PRIMARY KEY, tier TEXT NOT NULL, json TEXT NOT NULL);"
            "CREATE TABLE pid (pid TEXT PRIMARY KEY, json TEXT NOT NULL);"
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        )
        conn.executemany(
            "INSERT INTO dtc (code, tier, json) VALUES (?, ?, ?)",
            [(d.code, d.tier.value, d.model_dump_json()) for d in definitions.values()],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO pid (pid, json) VALUES (?, ?)",
            [(p.pid, p.model_dump_json()) for p in pids],
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('stats', ?)", (stats.model_dump_json(),)
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('fingerprint', ?)", (index_fingerprint(commit),)
        )
        conn.commit()
    finally:
        conn.close()
    os.replace(partial, out_path)  # readers never see a half-written index
    return stats


def ensure_index(settings: Settings) -> Path:
    """The index path, building the index first if it is missing or out of date."""
    path = settings.obdex_index_path
    if not index_is_current(path):
        build_index(settings.data_dir / "obdex", path)
    return path


def index_is_current(path: Path, commit: str = OBDEX_COMMIT) -> bool:
    """True if `path` is an index built from this commit, mapping and tier list."""
    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'fingerprint'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return row is not None and row[0] == index_fingerprint(commit)


class ObdexIndex:
    """Read-only lookups in a built index."""

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise IndexNotBuiltError(f"OBDex index not found at {path}. Run ensure_index first.")
        self._conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def close(self) -> None:
        self._conn.close()

    def lookup_dtc(self, code: str) -> DtcDefinition | None:
        row = self._conn.execute("SELECT json FROM dtc WHERE code = ?", (code.upper(),)).fetchone()
        return DtcDefinition.model_validate_json(row[0]) if row else None

    def lookup_pid(self, pid: str) -> PidInfo | None:
        row = self._conn.execute("SELECT json FROM pid WHERE pid = ?", (pid.upper(),)).fetchone()
        return PidInfo.model_validate_json(row[0]) if row else None

    def stats(self) -> IndexStats:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'stats'").fetchone()
        return IndexStats.model_validate(json.loads(row[0]))
