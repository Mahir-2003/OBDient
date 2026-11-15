"""Escalation rules, fixed report strings and banned phrases (SPEC §3.4, §6).

Escalations are computed here, by rules, from the scan and from what the user said. The
model never decides them and cannot remove one.
"""

import re
from collections.abc import Sequence

from obdient.knowledge.evidence import EvidenceStore
from obdient.knowledge.obdex import DtcDefinition
from obdient.knowledge.wal33d import WAL33D_CAUTION, WAL33D_LABEL
from obdient.schemas.common import Condition, Model
from obdient.schemas.evidence import EvidenceKind
from obdient.schemas.nhtsa import Recall
from obdient.schemas.report import Escalation, EscalationLevel
from obdient.schemas.snapshot import Reading, Snapshot, UserReport, WarningLight

__all__ = [
    "BANNED_PHRASES",
    "COMPLAINT_LABEL",
    "DISCLAIMER",
    "LIMP_MODE_LABEL",
    "RECALLS_NOTE",
    "VIN_LOOKUP_URL",
    "WAL33D_CAUTION",
    "WAL33D_LABEL",
    "SafetyInputs",
    "definition_labels",
    "evaluate",
    "find_banned",
    "match_user_text",
    "merge",
    "user_texts",
]

# ---- fixed strings (§3.4) ----------------------------------------------------

DISCLAIMER = (
    "OBDient is a read-only informational tool, not a mechanic. This report is built from "
    "generic OBD-II data and public sources and may be incomplete or wrong. It does not tell "
    "you whether the vehicle can be driven. Have a qualified technician confirm any diagnosis "
    "before paying for repairs."
)
RECALLS_NOTE = (
    "Recalls listed here are model-level, not specific to your VIN. Check your VIN at the "
    "NHTSA lookup linked below."
)
VIN_LOOKUP_URL = "https://www.nhtsa.gov/recalls"
COMPLAINT_LABEL = "Unverified owner reports"
# Shown beside a code in the codes section. It is information about the code, not an
# escalation: OBDex sets this flag on about half of all codes (SPEC X13).
LIMP_MODE_LABEL = "OBDex: can trigger reduced-power (limp) mode"

BANNERS: dict[EscalationLevel, str] = {
    EscalationLevel.STOP: "Stop and have it checked before driving further.",
    EscalationLevel.PROMPT: "Get it inspected promptly.",
    EscalationLevel.OUT_OF_SCOPE: "OBDient can't read those systems; have them inspected.",
    EscalationLevel.RECALL: "Open-recall check recommended: look up your VIN at nhtsa.gov/recalls.",
}
_LEVEL_ORDER = list(BANNERS)

# ---- thresholds (tunable) ----------------------------------------------------

COOLANT_STOP_C = 115.0
LOW_VOLTAGE_V = 12.0
ENGINE_RUNNING_RPM = 400.0

_ECT, _RPM, _MODULE_V = "0105", "010C", "0142"
_SINGLE_CYLINDER_MISFIRES = frozenset(f"P030{n}" for n in range(1, 7))
RECALL_COMPONENTS = ("BRAKE", "STEERING", "AIR BAG", "FUEL SYSTEM", "POWER TRAIN", "ENGINE")

# ---- user-text triggers (§6.3) -----------------------------------------------

_FUEL = r"\b(fuel|gas|gasoline|petrol)\b"
_SMELL = r"\b(smell\w*|odou?r|fumes?)\b"
_FLASH = r"\b(flash\w*|blink\w*)\b"
_MIL = r"\b(check[- ]engine|engine light|mil|cel)\b"
_GAP = r"[^.!?;\n]"

TRIGGERS: dict[str, re.Pattern[str]] = {
    "fuel_smell": re.compile(
        rf"{_FUEL}{_GAP}{{0,25}}{_SMELL}|{_SMELL}{_GAP}{{0,15}}{_FUEL}", re.IGNORECASE
    ),
    "smoke_or_steam": re.compile(r"\b(smok(e|es|ing)|steam(s|ing)?)\b", re.IGNORECASE),
    "brake": re.compile(r"\bbrak(e|es|ing)\b", re.IGNORECASE),
    "steering": re.compile(r"\bsteering\b", re.IGNORECASE),
    "oil_pressure_light": re.compile(r"\boil( pressure)? (light|lamp|warning)\b", re.IGNORECASE),
    "flashing_mil": re.compile(
        rf"{_FLASH}{_GAP}{{0,40}}{_MIL}|{_MIL}{_GAP}{{0,40}}{_FLASH}", re.IGNORECASE
    ),
    "abs": re.compile(r"\babs\b", re.IGNORECASE),
    "airbag": re.compile(r"\bair ?bags?\b|\bsrs\b", re.IGNORECASE),
}
MAX_QUOTE_CHARS = 60
MAX_QUOTES = 3

# ---- banned phrases (§6.4) ---------------------------------------------------

BANNED_PHRASES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bsafe to drive\b",
        r"\bfine to drive\b",
        r"\bok(ay)? to drive\b",
        r"\bgood to drive\b",
        r"\b(you can|it'?s fine to) (keep|continue) driving\b",
        r"\bnothing to worry about\b",
        r"\bno need to (see|visit) a mechanic\b",
        r"\bguaranteed?\b",
        r"\bwill (definitely )?fix\b",
        r"\bdefinitely (is|caused|the cause)\b",
        r"\b100 ?%",
    )
)


def definition_labels(definition: DtcDefinition | None) -> list[str]:
    """The labels a code's OBDex definition adds to its `CodeEntry`."""
    if definition is not None and definition.flags.limp_mode is True:
        return [LIMP_MODE_LABEL]
    return []


def _straighten(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'")


def find_banned(text: str) -> list[str]:
    """The banned substrings found in `text`, in pattern order."""
    text = _straighten(text)
    return [m.group(0) for pattern in BANNED_PHRASES for m in pattern.finditer(text)]


def match_user_text(texts: Sequence[str]) -> dict[str, list[str]]:
    """Trigger name -> what the user wrote that matched it, in the order found.

    There is no negation handling: "no smoke" fires `smoke_or_steam`. Over-firing is the
    accepted failure direction, and the reason quotes the match so the reader can see why.
    """
    found: dict[str, list[str]] = {}
    for name, pattern in TRIGGERS.items():
        quotes: list[str] = []
        for text in texts:
            for match in pattern.finditer(_straighten(text)):
                quote = " ".join(match.group(0).split()).replace('"', "")[:MAX_QUOTE_CHARS]
                if quote and quote.lower() not in {q.lower() for q in quotes}:
                    quotes.append(quote)
        if quotes:
            found[name] = quotes[:MAX_QUOTES]
    return found


def user_texts(report: UserReport, answers: Sequence[str] = ()) -> list[str]:
    """The free text the keyword backstop reads: symptoms, start, and every ask_user answer."""
    return [t for t in (report.symptoms_text, report.started_when or "", *answers) if t]


class SafetyInputs(Model):
    snapshot: Snapshot
    # From read_live_data during the run: one list per call, in call order, so that batch n
    # here is the evidence store's live batch n (the scan is batch 0).
    extra_readings: list[list[Reading]] = []
    user_texts: list[str] = []
    recalls: list[Recall] = []


def _quoted(quotes: Sequence[str]) -> str:
    return "You mentioned " + ", ".join(f'"{q}"' for q in quotes) + "."


def evaluate(inputs: SafetyInputs, store: EvidenceStore) -> list[Escalation]:
    """Every rule that fires, at most once each: STOP, PROMPT, OUT_OF_SCOPE, RECALL order."""
    snapshot = inputs.snapshot
    report = snapshot.user_report
    lights = set(report.warning_lights_seen)
    checklist = report.checklist
    matched = match_user_text(inputs.user_texts)
    user_ids = [e.id for e in store.by_kind(EvidenceKind.USER_REPORT)]
    codes = sorted({d.code for d in snapshot.dtcs})
    found: list[Escalation] = []

    def fire(
        rule_id: str, level: EscalationLevel, reason: str, evidence_ids: Sequence[str]
    ) -> None:
        found.append(
            Escalation(
                rule_id=rule_id,
                level=level,
                banner=BANNERS[level],
                reason=reason,
                evidence_ids=list(dict.fromkeys(evidence_ids)),
            )
        )

    def code_ids(wanted: Sequence[str]) -> list[str]:
        items = (store.find(f"dtc:{code}") for code in wanted)
        return [item.id for item in items if item is not None]

    def live_id(pid: str, condition: Condition, batch: int) -> list[str]:
        item = store.find(f"live:{pid}:{condition.value}:{batch}")
        return [item.id] if item is not None else []

    def user_rule(
        rule_id: str, structured: bool, trigger: str, fixed: str, level: EscalationLevel
    ) -> None:
        if structured:
            fire(rule_id, level, fixed, user_ids)
        elif trigger in matched:
            fire(rule_id, level, _quoted(matched[trigger]), user_ids)

    # Every live phase: the scan's conditions are batch 0, each read_live_data call is next.
    phases: list[tuple[Condition, int, list[Reading]]] = [
        (condition, 0, [r for r in snapshot.readings if r.condition is condition])
        for condition in snapshot.conditions_captured
    ]
    for number, batch in enumerate(inputs.extra_readings, start=1):
        for condition in dict.fromkeys(r.condition for r in batch):
            phases.append((condition, number, [r for r in batch if r.condition is condition]))

    def values(group: Sequence[Reading], pid: str) -> list[float]:
        return [r.value for r in group if r.pid == pid and isinstance(r.value, float)]

    # S1: a live coolant reading at or above the limit. Freeze-frame values do not count.
    hot = [(max(values(g, _ECT)), c, b) for c, b, g in phases if values(g, _ECT)]
    hot = [entry for entry in hot if entry[0] >= COOLANT_STOP_C]
    if hot:
        fire(
            "S1_coolant_overtemp",
            EscalationLevel.STOP,
            f"Coolant temperature read {max(v for v, _, _ in hot):.0f} °C "
            f"(limit {COOLANT_STOP_C:.0f} °C).",
            [i for _, condition, batch in hot for i in live_id(_ECT, condition, batch)],
        )
    if "P0217" in codes:
        fire(
            "S2_p0217",
            EscalationLevel.STOP,
            "Code P0217 (engine over-temperature) is present.",
            code_ids(["P0217"]),
        )
    stop = EscalationLevel.STOP
    user_rule(
        "S3_fuel_smell",
        checklist.fuel_smell is True,
        "fuel_smell",
        "You reported a fuel smell.",
        stop,
    )
    user_rule(
        "S4_smoke_or_steam",
        checklist.smoke_or_steam is True,
        "smoke_or_steam",
        "You reported smoke or steam.",
        stop,
    )
    user_rule(
        "S5_brake_problem",
        checklist.brake_problem is True,
        "brake",
        "You reported a brake problem.",
        stop,
    )
    user_rule(
        "S6_steering_problem",
        checklist.steering_problem is True,
        "steering",
        "You reported a steering problem.",
        stop,
    )
    user_rule(
        "S7_oil_pressure_light",
        WarningLight.OIL_PRESSURE in lights,
        "oil_pressure_light",
        "You reported an oil pressure warning light.",
        stop,
    )
    user_rule(
        "S8_flashing_mil",
        WarningLight.FLASHING_MIL in lights,
        "flashing_mil",
        "You reported a flashing check-engine light.",
        stop,
    )

    # P2: a random misfire, or misfires on two or more cylinders.
    single = [c for c in codes if c in _SINGLE_CYLINDER_MISFIRES]
    if "P0300" in codes or len(single) >= 2:
        misfires = [c for c in codes if c == "P0300" or c in _SINGLE_CYLINDER_MISFIRES]
        fire(
            "P2_misfire",
            EscalationLevel.PROMPT,
            f"Misfire code(s) {', '.join(misfires)} are present.",
            code_ids(misfires),
        )
    # P3: low system voltage while the engine is running, judged within one phase or batch.
    has_module_v = _MODULE_V in snapshot.supported_pids
    adapter_v = snapshot.adapter.voltage
    low: list[tuple[float, list[str]]] = []
    for condition, batch, group in phases:
        if not any(rpm >= ENGINE_RUNNING_RPM for rpm in values(group, _RPM)):
            continue
        rpm_ids = live_id(_RPM, condition, batch)
        if has_module_v:
            volts = [v for v in values(group, _MODULE_V) if v < LOW_VOLTAGE_V]
            if volts:
                low.append((min(volts), [*live_id(_MODULE_V, condition, batch), *rpm_ids]))
        elif adapter_v is not None and adapter_v < LOW_VOLTAGE_V:
            low.append((adapter_v, rpm_ids))
    if low:
        lowest = min(value for value, _ in low)
        # Two decimals, as the reading itself is shown, so 11.96 V never prints as "12.0 V".
        fire(
            "P3_low_voltage",
            EscalationLevel.PROMPT,
            f"System voltage read {lowest:.2f} V with the engine running "
            f"(limit {LOW_VOLTAGE_V:.1f} V).",
            [i for _, ids in low for i in ids],
        )

    # O1: systems a generic OBD scan cannot read.
    structured: list[str] = []
    if checklist.brake_problem is True:
        structured.append("brakes")
    if checklist.steering_problem is True:
        structured.append("steering")
    if WarningLight.ABS in lights:
        structured.append("ABS")
    if WarningLight.AIRBAG in lights:
        structured.append("airbags")
    quotes = [q for name in ("brake", "steering", "abs", "airbag") for q in matched.get(name, [])]
    if structured:
        fire(
            "O1_unreadable_system",
            EscalationLevel.OUT_OF_SCOPE,
            f"You reported a problem with {_join(structured)}.",
            user_ids,
        )
    elif quotes:
        unique = list(dict.fromkeys(quotes))[:MAX_QUOTES]
        fire("O1_unreadable_system", EscalationLevel.OUT_OF_SCOPE, _quoted(unique), user_ids)

    # R1: model-level recalls on safety components. Lowest level; never graded.
    safety_recalls = [
        r for r in inputs.recalls if any(part in r.component.upper() for part in RECALL_COMPONENTS)
    ]
    if safety_recalls:
        components = list(dict.fromkeys(r.component for r in safety_recalls))
        campaigns = [r.campaign_number for r in safety_recalls]
        recall_items = (store.find(f"recall:{number}") for number in campaigns)
        fire(
            "R1_safety_recall",
            EscalationLevel.RECALL,
            f"{len(safety_recalls)} model-level recall(s) touch {'; '.join(components)}: "
            f"{', '.join(campaigns)}.",
            [item.id for item in recall_items if item is not None],
        )

    found.sort(key=lambda e: (_LEVEL_ORDER.index(e.level), e.rule_id))
    return found


def _join(words: Sequence[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def merge(existing: Sequence[Escalation], new: Sequence[Escalation]) -> list[Escalation]:
    """Keep every existing escalation unchanged and append rules not seen before (X8)."""
    known = {e.rule_id for e in existing}
    return [*existing, *(e for e in new if e.rule_id not in known)]
