"""NHTSA recall and complaint records (SPEC §5.4).

They live here, not in `knowledge/nhtsa.py`, because `Scenario` embeds them and `schemas`
may not import `knowledge`. `knowledge/nhtsa.py` re-exports both.
"""

from obdient.schemas.common import Model


class Recall(Model):
    campaign_number: str
    component: str
    summary: str
    consequence: str
    remedy: str
    report_date: str | None
    matched_names: list[str]  # the model names this recall was found under


class Complaint(Model):
    odi_number: str
    date_filed: str | None
    date_of_incident: str | None
    components: list[str]
    summary: str
    crash: bool
    fire: bool
    matched_names: list[str]
