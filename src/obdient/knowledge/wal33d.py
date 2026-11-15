"""Wal33D: unverified community definitions for Volkswagen P1xxx codes (SPEC §5.3).

Lookup is by make and code only. The `OTHER` bucket of the upstream database is never
vendored: most VW codes collide with it under a different meaning.
"""

import json
from functools import cache
from pathlib import Path

from obdient.config import REPO_ROOT
from obdient.schemas.common import Model

VW_MAKES = frozenset({"VOLKSWAGEN", "VW"})
WAL33D_PATH = REPO_ROOT / "data" / "wal33d" / "vw.json"
CAUTION_FROM_YEAR = 2005

WAL33D_LABEL = "Unverified community definition (Wal33D, MIT)."
WAL33D_CAUTION = (
    "Caution: VW largely moved to standardized codes after 2004; this definition may not "
    "apply to this model year."
)


class CommunityHint(Model):
    code: str
    description: str
    label: str  # WAL33D_LABEL
    caution: str | None  # WAL33D_CAUTION when the model year is 2005 or later, or unknown


@cache
def _table(path: Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8"))


def lookup_hint(
    make: str | None, model_year: int | None, code: str, path: Path = WAL33D_PATH
) -> CommunityHint | None:
    """A hint for a VW manufacturer code, or None. Never call this for a code OBDex defines."""
    if make is None or make.strip().upper() not in VW_MAKES:
        return None
    code = code.upper()
    if not code.startswith("P1"):
        return None
    description = _table(path).get(code)
    if description is None:
        return None
    needs_caution = model_year is None or model_year >= CAUTION_FROM_YEAR
    return CommunityHint(
        code=code,
        description=description,
        label=WAL33D_LABEL,
        caution=WAL33D_CAUTION if needs_caution else None,
    )
