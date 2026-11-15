"""NHTSA: VIN decoding, recalls and owner complaints (SPEC §5.4, §5.5).

Only two hosts are ever contacted, and only through `NhtsaClient`. `NhtsaResolver` exists
because NHTSA files one car under several model names: vPIC calls the 2018 GTI "Golf GTI",
while its recalls are filed under "GTI".
"""

import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import httpx

from obdient import __version__
from obdient.errors import NhtsaUnavailable
from obdient.schemas.common import Model
from obdient.schemas.nhtsa import Complaint, Recall
from obdient.schemas.scenario import Scenario

__all__ = [
    "Complaint",
    "FixtureCache",
    "NhtsaCache",
    "NhtsaClient",
    "NhtsaResolver",
    "NhtsaResult",
    "Recall",
    "VinDecode",
    "resolve_for_scenario",
]

logger = logging.getLogger("obdient.nhtsa")

VPIC_BASE = "https://vpic.nhtsa.dot.gov/api/vehicles/"
API_BASE = "https://api.nhtsa.gov/"
ALLOWED_BASES = (VPIC_BASE, API_BASE)
USER_AGENT = f"obdient/{__version__}"

TTL_DAY = timedelta(hours=24)
RETRY_BACKOFF_S = (1.0, 3.0)  # two retries

# A model name is only a candidate with its family prefix removed for these makes. A generic
# "drop the first word" rule would turn "Grand Cherokee" into "Cherokee".
FAMILY_PREFIXES: dict[str, tuple[str, ...]] = {"VOLKSWAGEN": ("Golf",), "VW": ("Golf",)}

Dataset = Literal["recalls", "complaints"]

# vPIC fields never copied out of a decode: the VIN, its prefix, and status text.
_VPIC_DROP = frozenset(
    {"VIN", "VehicleDescriptor", "ErrorCode", "ErrorText", "AdditionalErrorText"}
)


class VinDecode(Model):
    make: str | None
    model: str | None
    model_year: int | None
    displacement_l: float | None
    engine: str | None
    raw: dict[str, str]  # non-empty vPIC fields; every field that holds a VIN is left out


class NhtsaCache(Protocol):
    def get(self, key: str, max_age: timedelta | None) -> Any | None:
        """None max_age = never expires."""
        ...

    def get_stale(self, key: str) -> Any | None: ...

    def put(self, key: str, value: Any) -> None: ...


def cache_key(endpoint: str, params: Mapping[str, object]) -> str:
    pairs = "|".join(f"{k}={str(v).upper()}" for k, v in sorted(params.items()))
    return f"{endpoint}|{pairs}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


class FixtureCache:
    """A read-only `NhtsaCache` over one directory of saved responses.

    Files: `vpic_decode.json`, `vpic_models.json`, `recalls_<model>.json` and
    `complaints_<model>.json`, where `<model>` is the model name in lowercase with
    underscores. Make and year are not part of the file name: one directory is one vehicle.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, key: str) -> Path:
        endpoint, _, rest = key.partition("|")
        params = dict(pair.split("=", 1) for pair in rest.split("|") if "=" in pair)
        if endpoint in ("recalls", "complaints"):
            return self._dir / f"{endpoint}_{_slug(params.get('model', ''))}.json"
        return self._dir / f"{_slug(endpoint)}.json"

    def get(self, key: str, max_age: timedelta | None) -> Any | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def get_stale(self, key: str) -> Any | None:
        return self.get(key, None)

    def put(self, key: str, value: Any) -> None:
        return None  # fixtures are never written by a run


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


def _iso_date(value: object, day_first: bool) -> str | None:
    """NHTSA dates as ISO `YYYY-MM-DD`. Recalls use DD/MM/YYYY; complaints use MM/DD/YYYY."""
    text = _text(value)
    try:
        return datetime.strptime(text, "%d/%m/%Y" if day_first else "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _results(data: object, key: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    items = cast(dict[str, Any], data).get(key)
    if not isinstance(items, list):
        return []
    return [cast(dict[str, Any], i) for i in cast(list[object], items) if isinstance(i, dict)]


def _is_empty_answer(status: int, data: object) -> bool:
    """NHTSA answers "nothing matches" with HTTP 400 and a normal zero-result body.

    Seen live on both the recalls and the complaints endpoint (2026-11-14). Treating it as a
    failure would hide exactly the case the resolver exists for: 0 recalls under "Golf GTI".
    """
    if status != 400 or not isinstance(data, dict):
        return False
    body = cast(dict[str, Any], data)
    count = body.get("Count", body.get("count"))
    return count == 0 and body.get("results") == []


class NhtsaClient:
    """vPIC and the recalls / complaints API, behind a cache."""

    def __init__(
        self,
        cache: NhtsaCache,
        http: httpx.Client | None,
        delay_s: float = 1.0,
        timeout_s: float = 10.0,
        offline: bool = False,
        refresh: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cache = cache
        self._http = http
        self._delay_s = delay_s
        self._timeout_s = timeout_s
        self._offline = offline or http is None
        self._refresh = refresh  # ignore cache TTLs (the cache is still a fallback)
        self._sleep = sleep
        self._network_calls = 0

    # ---- endpoints -----------------------------------------------------------

    def decode_vin(self, vin: str) -> VinDecode:
        data = self._fetch(
            "vpic_decode",
            f"{VPIC_BASE}DecodeVinValues/{vin}",
            {"format": "json"},
            key_params={"vin": vin},
            max_age=None,
        )
        rows = _results(data, "Results")
        fields = {
            key: _text(value)
            for key, value in (rows[0] if rows else {}).items()
            if _text(value) and key not in _VPIC_DROP and "vin" not in key.lower()
        }
        year = fields.get("ModelYear", "")
        try:
            displacement = round(float(fields["DisplacementL"]), 1)
        except (KeyError, ValueError):
            displacement = None
        return VinDecode(
            make=fields["Make"].upper() if "Make" in fields else None,
            model=fields.get("Model"),
            model_year=int(year) if year.isascii() and year.isdigit() else None,
            displacement_l=displacement,
            engine=_engine_text(fields, displacement),
            raw=fields,
        )

    def models_for_make_year(self, make: str, year: int) -> list[str]:
        data = self._fetch(
            "vpic_models",
            f"{VPIC_BASE}GetModelsForMakeYear/make/{make}/modelyear/{year}",
            {"format": "json"},
            key_params={"make": make, "modelYear": year},
            max_age=TTL_DAY,
        )
        return [
            _text(r.get("Model_Name")) for r in _results(data, "Results") if r.get("Model_Name")
        ]

    def recalls(self, make: str, model: str, year: int) -> list[Recall]:
        params = {"make": make, "model": model, "modelYear": year}
        data = self._fetch(
            "recalls", f"{API_BASE}recalls/recallsByVehicle", params, max_age=TTL_DAY
        )
        return [
            Recall(
                campaign_number=_text(r.get("NHTSACampaignNumber")),
                component=_text(r.get("Component")),
                summary=_text(r.get("Summary")),
                consequence=_text(r.get("Consequence")),
                remedy=_text(r.get("Remedy")),
                report_date=_iso_date(r.get("ReportReceivedDate"), day_first=True),
                matched_names=[model],
            )
            for r in _results(data, "results")
            if r.get("NHTSACampaignNumber")
        ]

    def complaints(self, make: str, model: str, year: int) -> list[Complaint]:
        params = {"make": make, "model": model, "modelYear": year}
        data = self._fetch(
            "complaints", f"{API_BASE}complaints/complaintsByVehicle", params, max_age=TTL_DAY
        )
        return [
            Complaint(
                odi_number=_text(r.get("odiNumber")),
                date_filed=_iso_date(r.get("dateComplaintFiled"), day_first=False),
                date_of_incident=_iso_date(r.get("dateOfIncident"), day_first=False),
                components=[c.strip() for c in _text(r.get("components")).split(",") if c.strip()],
                summary=_text(r.get("summary")),
                crash=r.get("crash") is True,
                fire=r.get("fire") is True,
                matched_names=[model],
            )
            for r in _results(data, "results")
            if r.get("odiNumber") is not None
        ]

    # ---- transport -----------------------------------------------------------

    def _fetch(
        self,
        endpoint: str,
        url: str,
        params: Mapping[str, object],
        max_age: timedelta | None,
        key_params: Mapping[str, object] | None = None,
    ) -> Any:
        if not url.startswith(ALLOWED_BASES):
            raise ValueError(f"Refusing to contact {url!r}: not an NHTSA endpoint.")
        key = cache_key(endpoint, key_params if key_params is not None else params)
        if self._offline or self._http is None:
            # Cache only, whatever its age: an old answer beats none when we cannot ask again.
            cached = self._cache.get_stale(key)
            if cached is None:
                raise NhtsaUnavailable("offline and not cached")
            return cached
        if not self._refresh:
            cached = self._cache.get(key, max_age)
            if cached is not None:
                return cached

        reason = "no attempt made"
        for attempt in range(len(RETRY_BACKOFF_S) + 1):
            if attempt:
                self._sleep(RETRY_BACKOFF_S[attempt - 1])
            elif self._network_calls:
                self._sleep(self._delay_s)  # be polite between calls, not after cache hits
            self._network_calls += 1
            try:
                response = self._http.get(
                    url,
                    params={k: str(v) for k, v in params.items()},
                    headers={"User-Agent": USER_AGENT},
                    timeout=self._timeout_s,
                )
            except httpx.TimeoutException:
                reason = "timed out"
                continue
            except httpx.HTTPError as exc:
                reason = type(exc).__name__
                break
            if response.status_code >= 500:
                reason = f"HTTP {response.status_code}"
                continue
            try:
                data = response.json()
            except ValueError:
                data = None
            if response.status_code != 200 and not _is_empty_answer(response.status_code, data):
                reason = f"HTTP {response.status_code}"
                break
            if data is None:
                reason = "response was not JSON"
                break
            self._cache.put(key, data)
            return data

        stale = self._cache.get_stale(key)
        if stale is not None:
            logger.warning("NHTSA %s unavailable (%s); using cached data.", endpoint, reason)
            return stale
        raise NhtsaUnavailable(f"NHTSA {endpoint} unavailable: {reason}")


def _engine_text(fields: Mapping[str, str], displacement: float | None) -> str | None:
    parts: list[str] = []
    if displacement is not None:
        parts.append(f"{displacement:.1f}L")
    if fields.get("EngineCylinders"):
        parts.append(f"{fields['EngineCylinders']}-cyl")
    for key in ("EngineModel", "FuelTypePrimary"):
        if fields.get(key):
            parts.append(fields[key])
    if fields.get("Turbo", "").lower() == "yes":
        parts.append("turbo")
    return " ".join(parts) or None


class NhtsaResult(Model):
    candidate_names: list[str]
    recalls: list[Recall]
    complaints: list[Complaint]
    matched_names: dict[Dataset, list[str]]  # names that returned at least one record
    errors: list[str]  # one entry per failed (dataset, name) query: "<dataset> for '<name>': ..."

    def failed(self, dataset: Dataset) -> int:
        """How many of this dataset's queries failed."""
        return sum(1 for error in self.errors if error.startswith(f"{dataset} for "))


class NhtsaResolver:
    def __init__(self, client: NhtsaClient, products_enabled: bool = False) -> None:
        self._client = client
        # [VERIFY M0] The products endpoint is unverified; candidate 3 of §5.5 is not built.
        self._products_enabled = products_enabled

    def candidate_names(self, make: str, model: str, year: int) -> list[str]:
        """Names to query, in order: the model as given, then without its family prefix."""
        names = [model.strip()]
        for prefix in FAMILY_PREFIXES.get(make.strip().upper(), ()):
            stem = prefix.lower() + " "
            rest = model.strip()[len(stem) :].strip()
            if model.strip().lower().startswith(stem) and rest:
                names.append(rest)
        unique: list[str] = []
        for name in names:
            if name and name.lower() not in {u.lower() for u in unique}:
                unique.append(name)
        return unique

    def resolve(self, make: str, model: str, year: int) -> NhtsaResult:
        names = self.candidate_names(make, model, year)
        recalls: dict[str, Recall] = {}
        complaints: dict[str, Complaint] = {}
        matched: dict[Dataset, list[str]] = {"recalls": [], "complaints": []}
        errors: list[str] = []
        for name in names:
            try:
                found = self._client.recalls(make, name, year)
            except NhtsaUnavailable as exc:
                errors.append(f"recalls for {name!r}: {exc}")
            else:
                if found:
                    matched["recalls"].append(name)
                for recall in found:
                    known = recalls.get(recall.campaign_number)
                    recalls[recall.campaign_number] = (
                        recall
                        if known is None
                        else known.model_copy(
                            update={"matched_names": [*known.matched_names, name]}
                        )
                    )
            try:
                reports = self._client.complaints(make, name, year)
            except NhtsaUnavailable as exc:
                errors.append(f"complaints for {name!r}: {exc}")
            else:
                if reports:
                    matched["complaints"].append(name)
                for complaint in reports:
                    seen = complaints.get(complaint.odi_number)
                    complaints[complaint.odi_number] = (
                        complaint
                        if seen is None
                        else seen.model_copy(update={"matched_names": [*seen.matched_names, name]})
                    )
        return NhtsaResult(
            candidate_names=names,
            recalls=sorted(
                recalls.values(),
                key=lambda r: (r.report_date or "", r.campaign_number),
                reverse=True,
            ),
            complaints=sorted(
                complaints.values(), key=lambda c: (c.date_filed or "", c.odi_number), reverse=True
            ),
            matched_names=matched,
            errors=errors,
        )


def resolve_for_scenario(scenario: Scenario, fixtures_dir: Path) -> NhtsaResult:
    """What a scenario run sees from NHTSA. Never touches the network (SPEC §11)."""
    vehicle = scenario.vehicle
    if scenario.nhtsa.fixture is not None:
        cache = FixtureCache(fixtures_dir / scenario.nhtsa.fixture)
        client = NhtsaClient(cache, http=None, offline=True)
        return NhtsaResolver(client).resolve(vehicle.make, vehicle.model, vehicle.year)
    names = NhtsaResolver(NhtsaClient(FixtureCache(fixtures_dir), None)).candidate_names(
        vehicle.make, vehicle.model, vehicle.year
    )
    recalls = sorted(
        scenario.nhtsa.recalls or [],
        key=lambda r: (r.report_date or "", r.campaign_number),
        reverse=True,
    )
    complaints = sorted(
        scenario.nhtsa.complaints or [],
        key=lambda c: (c.date_filed or "", c.odi_number),
        reverse=True,
    )
    return NhtsaResult(
        candidate_names=names,
        recalls=recalls,
        complaints=complaints,
        matched_names={
            "recalls": [n for n in names if any(n in r.matched_names for r in recalls)],
            "complaints": [n for n in names if any(n in c.matched_names for c in complaints)],
        },
        errors=[],
    )
