"""NHTSA client, cache behaviour and the resolver (SPEC §5.4, §5.5). No network."""

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from obdient.errors import NhtsaUnavailable
from obdient.knowledge.nhtsa import (
    API_BASE,
    USER_AGENT,
    VPIC_BASE,
    FixtureCache,
    NhtsaClient,
    NhtsaResolver,
    cache_key,
    resolve_for_scenario,
)
from obdient.schemas.scenario import load_scenario
from obdient.storage.db import Database
from support import GTI_FIXTURE, NHTSA_FIXTURES, SCENARIOS_DIR, make_scenario

RECALLS_GTI = json.loads((GTI_FIXTURE / "recalls_gti.json").read_text())
EMPTY = {"Count": 0, "Message": "Results returned successfully", "results": []}


def fixture_client() -> NhtsaClient:
    return NhtsaClient(FixtureCache(GTI_FIXTURE), http=None, offline=True)


class MemoryCache:
    def __init__(self) -> None:
        self.fresh: dict[str, Any] = {}
        self.stale: dict[str, Any] = {}
        self.asked: list[tuple[str, timedelta | None]] = []

    def get(self, key: str, max_age: timedelta | None) -> Any | None:
        self.asked.append((key, max_age))
        return self.fresh.get(key)

    def get_stale(self, key: str) -> Any | None:
        return self.stale.get(key, self.fresh.get(key))

    def put(self, key: str, value: Any) -> None:
        self.fresh[key] = value


class Web:
    """A scripted httpx transport: each request pops the next response or exception."""

    def __init__(self, *script: httpx.Response | Exception) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def client(self, cache: MemoryCache | Database | None = None, **kwargs: Any) -> NhtsaClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        return NhtsaClient(
            cache if cache is not None else MemoryCache(), http, sleep=self.sleeps.append, **kwargs
        )


def ok(data: object = EMPTY) -> httpx.Response:
    return httpx.Response(200, json=data)


# ---- the permanent GTI regression (§5.5) -------------------------------------


def test_gti_candidates_are_the_vpic_name_then_the_name_without_its_family() -> None:
    resolver = NhtsaResolver(fixture_client())
    assert resolver.candidate_names("VOLKSWAGEN", "Golf GTI", 2018) == ["Golf GTI", "GTI"]


def test_gti_has_three_recalls_all_found_under_gti_only() -> None:
    result = NhtsaResolver(fixture_client()).resolve("VOLKSWAGEN", "Golf GTI", 2018)

    assert result.candidate_names == ["Golf GTI", "GTI"]
    assert [r.campaign_number for r in result.recalls] == ["24V110000", "19V615000", "18V369000"]
    assert all(r.matched_names == ["GTI"] for r in result.recalls)
    assert [r.report_date for r in result.recalls] == ["2024-02-14", "2019-08-22", "2018-06-01"]
    assert result.matched_names["recalls"] == ["GTI"]  # a naive "Golf GTI" query finds none
    assert result.errors == []


def test_gti_complaints_are_merged_from_both_names() -> None:
    result = NhtsaResolver(fixture_client()).resolve("VOLKSWAGEN", "Golf GTI", 2018)

    assert result.matched_names["complaints"] == ["Golf GTI", "GTI"]
    assert len(result.complaints) == 50  # 33 + 17, and the two sets share no ODI number
    assert len({c.odi_number for c in result.complaints}) == 50
    by_name = {
        name: sum(1 for c in result.complaints if c.matched_names == [name])
        for name in ("Golf GTI", "GTI")
    }
    assert by_name == {"Golf GTI": 33, "GTI": 17}
    dates = [c.date_filed or "" for c in result.complaints]
    assert dates == sorted(dates, reverse=True)
    newest = result.complaints[0]
    assert newest.date_filed == "2026-09-15" and newest.components == ["ENGINE"]
    assert all(isinstance(c.odi_number, str) and c.summary for c in result.complaints)


def test_a_record_found_under_two_names_is_kept_once_with_both_names() -> None:
    web = Web(ok(RECALLS_GTI), ok(), ok(RECALLS_GTI), ok())
    result = NhtsaResolver(web.client(delay_s=0)).resolve("VOLKSWAGEN", "Golf GTI", 2018)
    assert len(result.recalls) == 3
    assert all(r.matched_names == ["Golf GTI", "GTI"] for r in result.recalls)
    assert result.matched_names == {"recalls": ["Golf GTI", "GTI"], "complaints": []}


# ---- candidate names ---------------------------------------------------------


@pytest.mark.parametrize(
    ("make", "model", "expected"),
    [
        ("VOLKSWAGEN", "Golf GTI", ["Golf GTI", "GTI"]),
        ("volkswagen", "golf r", ["golf r", "r"]),
        ("VOLKSWAGEN", "Golf", ["Golf"]),  # nothing remains after the prefix
        ("VOLKSWAGEN", "GTI", ["GTI"]),
        ("VOLKSWAGEN", "Golfer GTI", ["Golfer GTI"]),  # the prefix must be a whole word
        ("VOLKSWAGEN", "Golf  SportWagen", ["Golf  SportWagen", "SportWagen"]),
        ("JEEP", "Grand Cherokee", ["Grand Cherokee"]),  # no generic "drop the first word" rule
        ("FORD", "Golf GTI", ["Golf GTI"]),  # prefixes are per make
    ],
)
def test_candidate_names(make: str, model: str, expected: list[str]) -> None:
    assert NhtsaResolver(fixture_client()).candidate_names(make, model, 2018) == expected


# ---- parsing -----------------------------------------------------------------


def test_decode_vin_keeps_the_vin_and_its_prefix_out_of_the_result() -> None:
    decoded = fixture_client().decode_vin("WVWZZZAUZJW000000")
    assert (decoded.make, decoded.model, decoded.model_year) == ("VOLKSWAGEN", "Golf GTI", 2018)
    assert decoded.displacement_l == 2.0
    assert decoded.engine == "2.0L 4-cyl Gasoline"
    assert decoded.raw["Trim"] == "S, SE, Autobahn"
    assert "VIN" not in decoded.raw and "VehicleDescriptor" not in decoded.raw
    assert all(value for value in decoded.raw.values())  # non-empty fields only


def test_decode_vin_with_nothing_useful() -> None:
    web = Web(ok({"Results": [{"VIN": "X", "Make": "", "ModelYear": "abc", "DisplacementL": "?"}]}))
    decoded = web.client().decode_vin("WVWZZZAUZJW000000")
    assert decoded.model_dump() == {
        "make": None,
        "model": None,
        "model_year": None,
        "displacement_l": None,
        "engine": None,
        "raw": {"ModelYear": "abc", "DisplacementL": "?"},
    }


def test_models_for_make_year() -> None:
    models = fixture_client().models_for_make_year("VOLKSWAGEN", 2018)
    assert len(models) == 14 and "Golf GTI" in models and "GTI" not in models


def test_recall_fields() -> None:
    recalls = {r.campaign_number: r for r in fixture_client().recalls("VOLKSWAGEN", "GTI", 2018)}
    recall = recalls["24V110000"]
    assert recall.component == "FUEL SYSTEM, GASOLINE" and recall.report_date == "2024-02-14"
    assert recall.summary and recall.consequence and recall.remedy
    assert fixture_client().recalls("VOLKSWAGEN", "Golf GTI", 2018) == []


def test_unparsable_dates_and_malformed_rows_are_tolerated() -> None:
    rows = {"results": [{"NHTSACampaignNumber": "1", "ReportReceivedDate": "soon"}, {"x": 1}, 7]}
    (recall,) = Web(ok(rows)).client().recalls("A", "B", 2018)
    assert (recall.report_date, recall.component) == (None, "")
    odd = {"results": [{"odiNumber": 5, "components": "A, B,", "crash": "yes", "fire": True}]}
    (complaint,) = Web(ok(odd)).client().complaints("A", "B", 2018)
    assert complaint.odi_number == "5" and complaint.components == ["A", "B"]
    assert (complaint.crash, complaint.fire, complaint.date_filed) == (False, True, None)
    assert Web(ok(["not", "a", "dict"])).client().recalls("A", "B", 2018) == []


# ---- requests, cache and failure handling ------------------------------------


def test_request_shape_and_cache_key() -> None:
    web = Web(ok(), ok({"Results": []}))
    cache = MemoryCache()
    client = web.client(cache, delay_s=0)
    client.recalls("Volkswagen", "Golf GTI", 2018)
    client.decode_vin("WVWZZZAUZJW000000")

    first, second = web.requests
    assert str(first.url).startswith(f"{API_BASE}recalls/recallsByVehicle?")
    assert dict(first.url.params) == {
        "make": "Volkswagen",
        "model": "Golf GTI",
        "modelYear": "2018",
    }
    assert first.headers["User-Agent"] == USER_AGENT and USER_AGENT.startswith("obdient/")
    assert "@" not in USER_AGENT  # no contact details
    assert str(second.url) == f"{VPIC_BASE}DecodeVinValues/WVWZZZAUZJW000000?format=json"
    assert list(cache.fresh) == [
        "recalls|make=VOLKSWAGEN|model=GOLF GTI|modelYear=2018",
        "vpic_decode|vin=WVWZZZAUZJW000000",
    ]
    assert cache_key("x", {"b": "q r", "a": 1}) == "x|a=1|b=Q R"


def test_ttls_are_24_hours_except_the_vin_decode() -> None:
    web = Web(ok(), ok(), ok({"Results": []}), ok({"Results": []}))
    cache = MemoryCache()
    client = web.client(cache, delay_s=0)
    client.recalls("A", "B", 2018)
    client.complaints("A", "B", 2018)
    client.models_for_make_year("A", 2018)
    client.decode_vin("WVWZZZAUZJW000000")
    assert [age for _, age in cache.asked] == [timedelta(hours=24)] * 3 + [None]


def test_a_cache_hit_makes_no_request_and_no_delay() -> None:
    web = Web(ok(RECALLS_GTI))
    client = web.client(delay_s=5)
    first = client.recalls("VOLKSWAGEN", "GTI", 2018)
    again = client.recalls("volkswagen", "gti", 2018)  # the key ignores case
    assert [r.campaign_number for r in first] == [r.campaign_number for r in again]
    assert len(web.requests) == 1 and web.sleeps == []


def test_delay_between_network_calls_only() -> None:
    web = Web(ok(), ok(), ok())
    client = web.client(delay_s=1.5)
    for model in ("A", "B", "C"):
        client.recalls("X", model, 2018)
    assert web.sleeps == [1.5, 1.5]  # none before the first call


def test_expired_database_entries_are_refetched(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    now = [datetime(2026, 11, 14, tzinfo=UTC)]
    database = Database(tmp_path / "cache.db", clock=lambda: now[0])
    web = Web(ok(RECALLS_GTI), ok(EMPTY))
    client = web.client(database, delay_s=0)

    assert len(client.recalls("VOLKSWAGEN", "GTI", 2018)) == 3
    now[0] += timedelta(hours=23)
    assert len(client.recalls("VOLKSWAGEN", "GTI", 2018)) == 3 and len(web.requests) == 1
    now[0] += timedelta(hours=2)
    assert client.recalls("VOLKSWAGEN", "GTI", 2018) == [] and len(web.requests) == 2
    database.close()


def test_two_retries_on_timeout_and_5xx_with_backoff() -> None:
    web = Web(httpx.ReadTimeout("slow"), httpx.Response(503), ok(RECALLS_GTI))
    assert len(web.client().recalls("VOLKSWAGEN", "GTI", 2018)) == 3
    assert web.sleeps == [1.0, 3.0] and len(web.requests) == 3


def test_final_failure_without_cache_raises_with_the_reason() -> None:
    web = Web(httpx.Response(500), httpx.Response(502), httpx.ReadTimeout("slow"))
    with pytest.raises(NhtsaUnavailable, match=r"^NHTSA recalls unavailable: timed out$"):
        web.client().recalls("A", "B", 2018)
    assert len(web.requests) == 3


@pytest.mark.parametrize(
    ("step", "reason"),
    [
        (httpx.Response(404), "HTTP 404"),
        (httpx.Response(200, text="<html>"), "response was not JSON"),
        (httpx.ConnectError("refused"), "ConnectError"),
    ],
)
def test_client_errors_are_not_retried(step: httpx.Response | Exception, reason: str) -> None:
    web = Web(step)
    with pytest.raises(NhtsaUnavailable, match=f"NHTSA complaints unavailable: {reason}"):
        web.client().complaints("A", "B", 2018)
    assert len(web.requests) == 1 and web.sleeps == []


def test_http_400_with_a_zero_result_body_is_an_empty_answer_not_a_failure() -> None:
    """What NHTSA really sends when nothing matches, e.g. recalls for "Golf GTI"."""
    empty_400 = httpx.Response(400, json=EMPTY)
    lower_400 = httpx.Response(400, json={"count": 0, "message": "ok", "results": []})
    web = Web(empty_400, lower_400, ok(RECALLS_GTI), lower_400)
    cache = MemoryCache()
    result = NhtsaResolver(web.client(cache, delay_s=0)).resolve("VOLKSWAGEN", "Golf GTI", 2018)

    assert result.errors == [] and len(result.recalls) == 3
    assert result.matched_names == {"recalls": ["GTI"], "complaints": []}
    assert len(web.requests) == 4 and web.sleeps == [0, 0, 0]  # no retries
    assert cache.fresh["recalls|make=VOLKSWAGEN|model=GOLF GTI|modelYear=2018"] == EMPTY  # cached


@pytest.mark.parametrize(
    "body",
    [
        {"Count": 3, "results": []},
        {"Count": 0, "results": [{"x": 1}]},
        {"Count": 0},
        {"message": "Bad Request"},
        ["results"],
    ],
)
def test_other_http_400_bodies_are_still_failures(body: object) -> None:
    web = Web(httpx.Response(400, json=body))
    with pytest.raises(NhtsaUnavailable, match="HTTP 400"):
        web.client().recalls("A", "B", 2018)


def test_stale_cache_is_used_when_the_network_fails(caplog: pytest.LogCaptureFixture) -> None:
    cache = MemoryCache()
    cache.stale["recalls|make=VOLKSWAGEN|model=GTI|modelYear=2018"] = RECALLS_GTI
    web = Web(httpx.Response(404))
    with caplog.at_level(logging.WARNING, logger="obdient.nhtsa"):
        recalls = web.client(cache).recalls("VOLKSWAGEN", "GTI", 2018)
    assert len(recalls) == 3
    assert "NHTSA recalls unavailable (HTTP 404); using cached data." in caplog.text


def test_offline_uses_the_cache_and_a_miss_raises() -> None:
    web = Web()
    cache = MemoryCache()
    cache.fresh["recalls|make=VOLKSWAGEN|model=GTI|modelYear=2018"] = RECALLS_GTI
    client = web.client(cache, offline=True)
    assert len(client.recalls("VOLKSWAGEN", "GTI", 2018)) == 3
    with pytest.raises(NhtsaUnavailable, match=r"^offline and not cached$"):
        client.recalls("VOLKSWAGEN", "Golf GTI", 2018)
    assert web.requests == []


def test_offline_accepts_an_expired_cache_row(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    now = [datetime(2026, 11, 1, tzinfo=UTC)]
    database = Database(tmp_path / "cache.db", clock=lambda: now[0])
    database.put("recalls|make=VOLKSWAGEN|model=GTI|modelYear=2018", RECALLS_GTI)
    now[0] += timedelta(days=30)
    client = NhtsaClient(database, http=None, offline=True)
    assert len(client.recalls("VOLKSWAGEN", "GTI", 2018)) == 3
    database.close()


def test_suggested_vin_and_other_vin_fields_never_leave_the_decoder() -> None:
    row = {
        "VIN": "1HGCM82633A004352",
        "SuggestedVIN": "1HGCM82633A00435!",
        "VehicleDescriptor": "1HGCM826*3A",
        "PossibleValues": "x",
        "Make": "HONDA",
    }
    decoded = Web(ok({"Results": [row]})).client().decode_vin("1HGCM82633A004352")
    assert decoded.raw == {"PossibleValues": "x", "Make": "HONDA"}
    assert "1HGCM826" not in decoded.model_dump_json()


def test_vw_make_alias_gets_the_family_prefix_too() -> None:
    resolver = NhtsaResolver(fixture_client())
    assert resolver.candidate_names("VW", "Golf GTI", 2018) == ["Golf GTI", "GTI"]


def test_refresh_skips_fresh_cache_but_falls_back_to_it() -> None:
    cache = MemoryCache()
    cache.fresh["recalls|make=A|model=B|modelYear=2018"] = RECALLS_GTI
    web = Web(ok(EMPTY), httpx.Response(404))
    assert web.client(cache, refresh=True).recalls("A", "B", 2018) == []
    cache.fresh["recalls|make=A|model=B|modelYear=2018"] = RECALLS_GTI
    assert len(web.client(cache, refresh=True).recalls("A", "B", 2018)) == 3  # network failed
    assert len(web.requests) == 2


def test_only_the_two_nhtsa_hosts_can_be_contacted() -> None:
    web = Web()
    client = web.client()
    for url in (
        "https://example.com/api",
        "https://api.nhtsa.gov.evil.example/x",
        "http://api.nhtsa.gov/",
    ):
        with pytest.raises(ValueError, match="not an NHTSA endpoint"):
            client._fetch("recalls", url, {}, max_age=None)  # pyright: ignore[reportPrivateUsage]
    assert web.requests == []


def test_a_failed_query_is_recorded_and_the_others_still_run() -> None:
    #           recalls Golf GTI     complaints Golf GTI   recalls GTI       complaints GTI
    web = Web(httpx.Response(404), ok(), ok(RECALLS_GTI), httpx.Response(404))
    result = NhtsaResolver(web.client(delay_s=0)).resolve("VOLKSWAGEN", "Golf GTI", 2018)
    assert len(result.recalls) == 3 and result.complaints == []
    assert result.errors == [
        "recalls for 'Golf GTI': NHTSA recalls unavailable: HTTP 404",
        "complaints for 'GTI': NHTSA complaints unavailable: HTTP 404",
    ]
    assert (result.failed("recalls"), result.failed("complaints")) == (1, 1)


# ---- FixtureCache and scenarios ----------------------------------------------


def test_fixture_cache_maps_keys_to_files_and_never_writes(tmp_path: Path) -> None:
    (tmp_path / "recalls_golf_gti.json").write_text('{"results": []}')
    cache = FixtureCache(tmp_path)
    assert cache.get("recalls|make=VOLKSWAGEN|model=GOLF GTI|modelYear=2018", None) == {
        "results": []
    }
    assert cache.get_stale("recalls|make=VW|model=Golf-GTI|modelYear=1999") == {"results": []}
    assert cache.get("recalls|make=VOLKSWAGEN|model=GTI|modelYear=2018", None) is None
    assert cache.get("vpic_decode|vin=X", None) is None
    cache.put("complaints|model=GTI", {"results": [1]})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["recalls_golf_gti.json"]


def test_the_gti_fixture_directory_holds_the_six_responses() -> None:
    assert sorted(p.name for p in GTI_FIXTURE.glob("*.json")) == [
        "complaints_golf_gti.json",
        "complaints_gti.json",
        "recalls_golf_gti.json",
        "recalls_gti.json",
        "vpic_decode.json",
        "vpic_models.json",
    ]
    decode = (GTI_FIXTURE / "vpic_decode.json").read_text()
    assert "WVWZZZAUZJW000000" in decode  # the placeholder, not a real VIN


def test_scenario_with_a_fixture_resolves_offline() -> None:
    result = resolve_for_scenario(
        load_scenario(SCENARIOS_DIR / "s01_pcv_lean.yaml"), NHTSA_FIXTURES
    )
    assert len(result.recalls) == 3 and len(result.complaints) == 50 and result.errors == []


def test_scenario_with_neither_sees_nothing_under_its_candidate_names() -> None:
    result = resolve_for_scenario(make_scenario(), NHTSA_FIXTURES)
    assert result.candidate_names == ["Golf GTI", "GTI"]
    assert (result.recalls, result.complaints, result.errors) == ([], [], [])
    assert result.matched_names == {"recalls": [], "complaints": []}


def test_scenario_with_inline_records_is_sorted_and_named_in_candidate_order() -> None:
    older = {
        "campaign_number": "18V123000",
        "component": "FUEL SYSTEM, GASOLINE",
        "summary": "s",
        "consequence": "c",
        "remedy": "r",
        "report_date": "2018-02-01",
    }
    newer = {
        **older,
        "campaign_number": "24V110000",
        "report_date": "2024-02-14",
        "matched_names": ["GTI", "Somewhere Else"],
    }
    scenario = make_scenario(nhtsa={"recalls": [older, newer]})
    result = resolve_for_scenario(scenario, NHTSA_FIXTURES)
    assert [r.campaign_number for r in result.recalls] == ["24V110000", "18V123000"]
    assert result.matched_names == {"recalls": ["Golf GTI", "GTI"], "complaints": []}


def test_scenario_with_a_missing_fixture_reports_errors_not_emptiness() -> None:
    result = resolve_for_scenario(make_scenario(nhtsa={"fixture": "nope"}), NHTSA_FIXTURES)
    assert result.recalls == [] and len(result.errors) == 4
    assert result.failed("recalls") == 2
