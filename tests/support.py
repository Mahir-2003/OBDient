"""Helpers shared by test modules (on the path through pytest's `pythonpath`)."""

from pathlib import Path
from typing import Any

from obdient.schemas.scenario import Scenario

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"


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
