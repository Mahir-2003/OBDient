"""The real NHTSA endpoints. Run with: uv run pytest -m network

Checks that the live API still has the shape the committed fixtures have, and that the
naming mismatch the resolver exists for is still there.
"""

import httpx
import pytest

from obdient.knowledge.nhtsa import NhtsaClient, NhtsaResolver

pytestmark = pytest.mark.network


class NoCache:
    def get(self, key: str, max_age: object) -> None:
        return None

    def get_stale(self, key: str) -> None:
        return None

    def put(self, key: str, value: object) -> None:
        return None


def test_live_gti_lookup_matches_the_fixture_shape() -> None:
    with httpx.Client() as http:
        client = NhtsaClient(NoCache(), http, delay_s=1.0)
        models = client.models_for_make_year("VOLKSWAGEN", 2018)
        result = NhtsaResolver(client).resolve("VOLKSWAGEN", "Golf GTI", 2018)

    assert "Golf GTI" in models
    assert result.errors == []
    assert result.candidate_names == ["Golf GTI", "GTI"]
    assert len(result.recalls) >= 3
    assert {"18V369000", "19V615000", "24V110000"} <= {r.campaign_number for r in result.recalls}
    assert all(r.report_date and r.component and r.summary for r in result.recalls)
    assert "GTI" in result.matched_names["recalls"]
    assert len(result.complaints) >= 50
    assert all(c.odi_number and c.date_filed for c in result.complaints)
