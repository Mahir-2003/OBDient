"""Helpers shared by test modules (on the path through pytest's `pythonpath`)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from obdient.agent import safety
from obdient.knowledge.evidence import ComplaintCorpus, EvidenceStore, prefetch
from obdient.knowledge.nhtsa import NhtsaResult, resolve_for_scenario
from obdient.knowledge.obdex import DtcDefinition, ObdexIndex
from obdient.schemas.report import Escalation
from obdient.schemas.scenario import Scenario
from obdient.schemas.snapshot import Snapshot, VehicleProfile
from obdient.vehicle.collector import collect
from obdient.vehicle.simulator import ScenarioSimulator

REPO = Path(__file__).resolve().parents[1]
SCENARIOS_DIR = REPO / "scenarios"
OBDEX_DIR = REPO / "data" / "obdex"
NHTSA_FIXTURES = REPO / "tests" / "fixtures" / "nhtsa"
GTI_FIXTURE = NHTSA_FIXTURES / "gti_2018"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def scenario_dict(**overrides: Any) -> dict[str, Any]:
    """A small valid scenario as plain data; tests override the fields they care about."""
    base: dict[str, Any] = {
        "id": "s99_test",
        "title": "Test scenario",
        "source": ["https://example.org/source"],
        "vehicle": {"vin": "TESTVIN", "make": "VOLKSWAGEN", "model": "Golf GTI", "year": 2018},
        "dtcs": {"stored": ["P0171"], "pending": ["P0300"], "permanent": ["P0171"]},
        "freeze_frame": {"trigger": "P0171", "rpm": 780, "ltft_b1": 19.5},
        "readiness": {"catalyst": "complete", "evap": "incomplete"},
        "since_clear": {"warmups": 45, "distance_km": 820},
        "conditions": {
            "idle": {"rpm": 780, "ltft_b1": 19.5, "ect_c": 90, "speed_kmh": 0},
            "hold_2500": {"rpm": 2500, "ltft_b1": 8.0, "ect_c": 91, "speed_kmh": 0},
        },
        "expected": {"top_finding_any": ["vacuum_leak"]},
    }
    return {**base, **overrides}


def make_scenario(**overrides: Any) -> Scenario:
    return Scenario.model_validate(scenario_dict(**overrides))


def profile_for(scenario: Scenario, snapshot: Snapshot, nhtsa: NhtsaResult) -> VehicleProfile:
    """What runner.build_profile (M5) will produce for a scenario run."""
    vehicle = scenario.vehicle
    return VehicleProfile(
        vin=snapshot.vin,
        make=vehicle.make,
        model=vehicle.model,
        model_year=vehicle.year,
        engine=vehicle.engine,
        displacement_l=vehicle.displacement_l,
        vpic_raw={},
        candidate_names=nhtsa.candidate_names,
        matched_names=nhtsa.matched_names,
    )


class Prefetched:
    """A scenario taken from YAML through collect() and prefetch()."""

    def __init__(self, scenario: Scenario, obdex: ObdexIndex) -> None:
        self.scenario = scenario
        self.snapshot = collect(ScenarioSimulator(scenario), scenario.build_user_report())
        self.nhtsa = resolve_for_scenario(scenario, NHTSA_FIXTURES)
        self.profile = profile_for(scenario, self.snapshot, self.nhtsa)
        self.store = EvidenceStore()
        self.corpus: ComplaintCorpus = prefetch(
            self.snapshot, self.profile, self.nhtsa, obdex, self.store, NOW
        )
        self.definitions: dict[str, DtcDefinition] = {}
        for code in {d.code for d in self.snapshot.dtcs}:
            definition = obdex.lookup_dtc(code)
            if definition is not None:
                self.definitions[code] = definition

    def escalations(self) -> list[Escalation]:
        inputs = safety.SafetyInputs(
            snapshot=self.snapshot,
            user_texts=safety.user_texts(self.snapshot.user_report),
            recalls=self.nhtsa.recalls,
        )
        return safety.evaluate(inputs, self.store)

    def summaries(self) -> list[str]:
        return [f"{e.id} {e.kind.value}: {e.summary}" for e in self.store.all()]
