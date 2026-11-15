"""The evidence store, prefetch and derived metrics (SPEC §3.3, §5.6).

Everything the model may cite is an `Evidence` item with an ID (`E1`, `E2`, ...). Prefetch
adds items in a fixed order, so the same snapshot always gets the same IDs.
"""

import math
import re
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from statistics import fmean
from typing import Any, cast

from obdient.config import OBDEX_COMMIT, WAL33D_COMMIT
from obdient.errors import UnknownEvidenceError
from obdient.knowledge import wal33d
from obdient.knowledge.nhtsa import NhtsaResult
from obdient.knowledge.obdex import ObdexIndex
from obdient.schemas.common import CANONICAL_UNITS, Condition, Tier
from obdient.schemas.evidence import Evidence, EvidenceKind, EvidenceSource, GapKind, SourceType
from obdient.schemas.nhtsa import Complaint
from obdient.schemas.snapshot import (
    CONTINUOUS_MONITORS,
    DtcStatus,
    ReadError,
    ReadErrorKind,
    Reading,
    Snapshot,
    UserReport,
    VehicleProfile,
)
from obdient.vehicle.pids import NAME_TO_PID, PID_TABLE

MAX_SUMMARY = 300
RECALL_SUMMARY_CHARS = 200
COMPLAINT_SUMMARY_CHARS = 220
CAUSE_SUMMARY_CHARS = 60
MAX_READ_ERROR_GAPS = 10
CORE_PIDS = ("0105", "0106", "0107", "010C")  # coolant, STFT, LTFT, rpm

# Derived-metric thresholds (tunable).
MAF_BAND_LOW_PER_L = 0.9
MAF_BAND_HIGH_PER_L = 1.6
WARM_COOLANT_C = 70.0
WARMUP_MIN_RUNTIME_S = 600.0
WARMUP_RATE_MIN_SPAN_S = 60.0
RECENT_CLEAR_MAX_WARMUPS = 5
RECENT_CLEAR_MAX_KM = 80.0

SEARCH_STOP_WORDS = frozenset(
    {"the", "a", "an", "of", "and", "or", "to", "in", "on", "at", "is", "it", "my", "when"}
    | {"with", "for"}
)
SEARCH_TOP_K = 5

# How a PID is named in a summary sentence.
LABELS: dict[str, str] = {
    "load_pct": "engine load",
    "ect_c": "coolant",
    "stft_b1": "STFT bank 1",
    "ltft_b1": "LTFT bank 1",
    "stft_b2": "STFT bank 2",
    "ltft_b2": "LTFT bank 2",
    "fuel_pressure_kpa": "fuel pressure",
    "map_kpa": "manifold pressure",
    "rpm": "engine speed",
    "speed_kmh": "speed",
    "timing_deg": "ignition timing",
    "iat_c": "intake air",
    "maf_gs": "MAF",
    "throttle_pct": "throttle",
    "o2_b1s1_v": "O2 sensor B1S1",
    "o2_b1s2_v": "O2 sensor B1S2",
    "runtime_s": "run time",
    "distance_mil_on_km": "distance with MIL on",
    "fuel_level_pct": "fuel level",
    "warmups_since_clear": "warm-ups since clear",
    "distance_since_clear_km": "distance since clear",
    "baro_kpa": "barometric pressure",
    "lambda_b1s1": "lambda B1S1",
    "module_v": "module voltage",
    "abs_load_pct": "absolute load",
    "lambda_cmd": "commanded lambda",
    "ambient_c": "ambient air",
    "minutes_mil_on": "time with MIL on",
    "minutes_since_clear": "time since clear",
    "oil_temp_c": "oil temperature",
}
_TRIM_NAMES = frozenset({"stft_b1", "ltft_b1", "stft_b2", "ltft_b2"})
_TRIM_PAIRS = ((1, "0106", "0107"), (2, "0108", "0109"))  # bank, STFT pid, LTFT pid


def quantity(value: float, unit: str, decimals: int = 0) -> dict[str, Any]:
    """A Quantity dict, with the value already rounded to its display precision."""
    rounded = round(float(value), decimals)
    return {"value": int(rounded) if decimals == 0 else rounded, "unit": unit}


def fmt(value: float, unit: str | None, decimals: int = 0, signed: bool = False) -> str:
    """A number as the summaries print it: fixed decimals, a space, the canonical unit."""
    rounded = round(float(value), decimals)
    if rounded == 0:
        rounded = 0.0  # no "-0.0"
    text = f"{rounded:+.{decimals}f}" if signed else f"{rounded:.{decimals}f}"
    return f"{text} {unit}" if unit else text


def _fmt_pid(pid: str, value: float) -> str:
    spec = PID_TABLE[pid]
    return fmt(value, spec.unit, spec.decimals, signed=spec.name in _TRIM_NAMES)


def _label(pid: str) -> str:
    return LABELS.get(PID_TABLE[pid].name, pid) if pid in PID_TABLE else pid


def _cut(text: str, limit: int = MAX_SUMMARY) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fit(parts: Sequence[str], room: int) -> str:
    """As many whole parts as fit, so a summary is never cut in the middle of a number."""
    if not parts:
        return "no readable values"
    shown: list[str] = []
    for index, part in enumerate(parts):
        rest = len(parts) - index - 1
        tail = f", and {rest} more values" if rest else ""
        if len(", ".join([*shown, part])) + len(tail) + 1 > room and shown:
            return ", ".join(shown) + f", and {len(parts) - index} more values"
        shown.append(part)
    return ", ".join(shown)


def _detail_is_shareable(error: ReadError) -> bool:
    """Whether a read error's detail may be shown to the model.

    Parse errors carry the raw reply, and a mode 09 reply is the VIN in hex: neither may
    ever reach an evidence item.
    """
    if not error.detail:
        return False
    return error.kind is not ReadErrorKind.PARSE_ERROR and not error.request.startswith("09")


def _check_payload(value: object, path: str) -> None:
    """Payload rule 1 of §3.3: no bare numbers; every number is a Quantity dict."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int | float):
        raise ValueError(f"Bare number at {path}; wrap it as a Quantity {{value, unit}}.")
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        if set(mapping) == {"value", "unit"}:
            number, unit = mapping["value"], mapping["unit"]
            if isinstance(number, bool) or not isinstance(number, int | float):
                raise ValueError(f"Quantity at {path} has a non-numeric value.")
            if not math.isfinite(number):
                raise ValueError(f"Quantity at {path} is not a finite number.")
            if unit not in CANONICAL_UNITS:
                raise ValueError(f"Quantity at {path} has a non-canonical unit {unit!r}.")
            return
        for key, item in mapping.items():
            _check_payload(item, f"{path}.{key}")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(cast(list[object], value)):
            _check_payload(item, f"{path}[{index}]")
        return
    raise ValueError(f"Unsupported payload value at {path}: {type(value).__name__}.")


class EvidenceStore:
    """Evidence for one run, numbered in the order it was added."""

    def __init__(self) -> None:
        self._items: list[Evidence] = []
        self._by_key: dict[str, Evidence] = {}
        self._keys: dict[str, str] = {}  # evidence id -> dedupe key
        self._live_batches = 0

    def add(
        self,
        kind: EvidenceKind,
        tier: Tier,
        source: EvidenceSource,
        summary: str,
        payload: dict[str, Any],
        matched_name: str | None = None,
        dedupe_key: str | None = None,
    ) -> Evidence:
        """Add an item, or return the existing one with the same `dedupe_key` (no new ID).

        Raises ValueError if the payload contains a bare number.
        """
        if dedupe_key is not None and dedupe_key in self._by_key:
            return self._by_key[dedupe_key]
        _check_payload(payload, "payload")
        item = Evidence(
            id=f"E{len(self._items) + 1}",
            kind=kind,
            tier=tier,
            source=source,
            summary=_cut(summary),
            payload=payload,
            matched_name=matched_name,
        )
        self._items.append(item)
        if dedupe_key is not None:
            self._by_key[dedupe_key] = item
            self._keys[item.id] = dedupe_key
        return item

    def get(self, evidence_id: str) -> Evidence:
        match = re.fullmatch(r"E([1-9][0-9]*)", evidence_id)
        if match is None or int(match.group(1)) > len(self._items):
            raise UnknownEvidenceError(evidence_id)
        return self._items[int(match.group(1)) - 1]

    def has(self, evidence_id: str) -> bool:
        try:
            self.get(evidence_id)
        except UnknownEvidenceError:
            return False
        return True

    def all(self) -> list[Evidence]:
        return list(self._items)

    def by_kind(self, kind: EvidenceKind) -> list[Evidence]:
        return [item for item in self._items if item.kind is kind]

    def find(self, dedupe_key: str) -> Evidence | None:
        return self._by_key.get(dedupe_key)

    def key_of(self, evidence_id: str) -> str | None:
        return self._keys.get(evidence_id)

    def next_live_batch(self) -> int:
        """Batch 0 is the scan; each `read_live_data` call takes the next number."""
        self._live_batches += 1
        return self._live_batches


class ComplaintCorpus:
    """The complaints retrieved for this vehicle. Per run; not evidence until searched."""

    def __init__(self, complaints: Sequence[Complaint], names_tried: Sequence[str]) -> None:
        self.complaints = list(complaints)
        self.names_tried = list(names_tried)

    def __len__(self) -> int:
        return len(self.complaints)

    def search(self, query: str, component: str | None, k: int = SEARCH_TOP_K) -> list[Complaint]:
        """Deterministic keyword ranking (SPEC §7.5.5)."""
        tokens = {t for t in re.findall(r"[a-z0-9]+", query.lower()) if t not in SEARCH_STOP_WORDS}
        wanted = component.strip().lower() if component and component.strip() else None
        scored: list[tuple[int, Complaint]] = []
        for complaint in self.complaints:
            if wanted is not None and not any(wanted in c.lower() for c in complaint.components):
                continue
            words = Counter(
                re.findall(
                    r"[a-z0-9]+", f"{complaint.summary} {' '.join(complaint.components)}".lower()
                )
            )
            present = [t for t in tokens if words[t]]
            score = 10 * len(present) + min(sum(words[t] for t in present), 10)
            if score:
                scored.append((score, complaint))
        # Sort by score desc, date_filed desc, odi_number asc (stable sorts, last key first).
        scored.sort(key=lambda pair: pair[1].odi_number)
        scored.sort(key=lambda pair: pair[1].date_filed or "", reverse=True)
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [complaint for _, complaint in scored[:k]]


# ---- sources ---------------------------------------------------------------


def _obd(now: datetime) -> EvidenceSource:
    return EvidenceSource(type=SourceType.OBD, retrieved_at=now)


def _computed(now: datetime) -> EvidenceSource:
    return EvidenceSource(type=SourceType.COMPUTED, retrieved_at=now)


def _gap(
    store: EvidenceStore,
    now: datetime,
    gap: GapKind,
    subject: str,
    summary: str,
    source: SourceType = SourceType.COMPUTED,
    **extra: Any,
) -> Evidence:
    return store.add(
        EvidenceKind.EVIDENCE_GAP,
        Tier.T3,
        EvidenceSource(type=source, retrieved_at=now),
        summary,
        {"gap": gap.value, "detail": summary, **extra},
        dedupe_key=f"gap:{gap.value}:{subject}",
    )


# ---- prefetch --------------------------------------------------------------


def user_report_is_default(report: UserReport) -> bool:
    return report == UserReport()


def _user_report_summary(report: UserReport) -> str:
    parts: list[str] = []
    if report.symptoms_text:
        parts.append(f'The user reports: "{_cut(report.symptoms_text, 160)}"')
    if report.started_when:
        parts.append(f'Started: "{_cut(report.started_when, 40)}"')
    if report.conditions:
        parts.append("Happens when: " + ", ".join(report.conditions))
    if report.warning_lights_seen:
        parts.append(
            "Warning lights seen: " + ", ".join(w.value for w in report.warning_lights_seen)
        )
    answered = {k: v for k, v in report.checklist.model_dump().items() if v is not None}
    if answered:
        parts.append(
            "Checklist: " + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in answered.items())
        )
    return ". ".join(parts) + "." if parts else "The user gave an odometer reading only."


def _cylinder(code: str) -> int | None:
    match = re.fullmatch(r"P03(0[1-9]|1[0-2])", code)
    return int(match.group(1)) if match else None


def prefetch(
    snapshot: Snapshot,
    profile: VehicleProfile,
    nhtsa: NhtsaResult | None,
    obdex: ObdexIndex,
    store: EvidenceStore,
    now: datetime,
) -> ComplaintCorpus:
    """Fill `store` from the snapshot and the reference data, in the fixed order of §5.6."""
    # 1. user report
    report = snapshot.user_report
    if not user_report_is_default(report):
        store.add(
            EvidenceKind.USER_REPORT,
            Tier.USER,
            EvidenceSource(type=SourceType.USER, retrieved_at=now),
            _user_report_summary(report),
            {
                "symptoms_text": report.symptoms_text,
                "started_when": report.started_when,
                "conditions": list(report.conditions),
                "warning_lights_seen": [w.value for w in report.warning_lights_seen],
                "checklist": report.checklist.model_dump(),
            },
            dedupe_key="user_report",
        )

    # 2. one dtc item per distinct code
    codes = sorted({d.code for d in snapshot.dtcs})
    for code in codes:
        entries = [d for d in snapshot.dtcs if d.code == code]
        statuses = [s.value for s in DtcStatus if any(d.status is s for d in entries)]
        ecus = sorted({d.ecu for d in entries})
        payload: dict[str, Any] = {"code": code, "statuses": statuses, "ecus": ecus}
        cylinder = _cylinder(code)
        if cylinder is not None:
            payload["cylinder"] = quantity(cylinder, "cylinder")
        store.add(
            EvidenceKind.DTC,
            Tier.T1,
            _obd(now),
            f"Code {code} is present ({', '.join(statuses)}) on ECU {', '.join(ecus)}.",
            payload,
            dedupe_key=f"dtc:{code}",
        )

    # 3. per code: OBDex definition, else Wal33D hint, else a gap
    for code in codes:
        definition = obdex.lookup_dtc(code)
        if definition is not None:
            causes = "; ".join(_cut(c.text, CAUSE_SUMMARY_CHARS) for c in definition.causes[:3])
            summary = f"OBDex definition of {code}: {definition.title}."
            if causes:
                summary += f" Common causes: {causes}."
            store.add(
                EvidenceKind.DTC_DEFINITION,
                definition.tier,
                EvidenceSource(
                    type=SourceType.OBDEX,
                    url=definition.source_urls[0] if definition.source_urls else None,
                    retrieved_at=now,
                    dataset_version=OBDEX_COMMIT,
                ),
                summary,
                {
                    "code": code,
                    "title": definition.title,
                    "description": definition.description,
                    "causes": [c.model_dump() for c in definition.causes],
                    "symptoms": definition.symptoms,
                    "flags": definition.flags.model_dump(),
                    "related_codes": definition.related_codes,
                    "source_urls": definition.source_urls,
                },
                dedupe_key=f"def:{code}",
            )
            continue
        hint = wal33d.lookup_hint(profile.make, profile.model_year, code)
        if hint is not None:
            summary = f"{hint.label} {code}: {hint.description}."
            if hint.caution:
                summary += f" {hint.caution}"
            store.add(
                EvidenceKind.COMMUNITY_HINT,
                Tier.T3,
                EvidenceSource(
                    type=SourceType.WAL33D, retrieved_at=now, dataset_version=WAL33D_COMMIT
                ),
                summary,
                hint.model_dump(),
                dedupe_key=f"def:{code}",
            )
            continue
        if code.startswith("P1"):
            _gap(
                store,
                now,
                GapKind.P1XXX_NO_DEFINITION,
                code,
                f"{code} is a manufacturer-specific code and no definition is available for it. "
                "Its meaning is unknown here.",
                code=code,
            )
        else:
            _gap(
                store,
                now,
                GapKind.NO_DEFINITION,
                code,
                f"No definition is available for {code}. Its meaning is unknown here.",
                code=code,
            )

    # 4. freeze frame
    frame = snapshot.freeze_frame
    if frame is not None:
        values = [r for r in frame.readings if isinstance(r.value, float) and r.pid in PID_TABLE]
        head = f"Freeze frame for {frame.trigger_code}: "
        parts = [f"{_label(r.pid)} {_fmt_pid(r.pid, cast(float, r.value))}" for r in values]
        store.add(
            EvidenceKind.FREEZE_FRAME,
            Tier.T1,
            _obd(now),
            head + _fit(parts, MAX_SUMMARY - len(head)) + ".",
            {
                "trigger_code": frame.trigger_code,
                "readings": {
                    r.name: quantity(
                        cast(float, r.value), r.unit or "count", PID_TABLE[r.pid].decimals
                    )
                    for r in values
                },
            },
            dedupe_key="freeze_frame",
        )
    elif any(d.status is DtcStatus.STORED for d in snapshot.dtcs):
        _gap(
            store,
            now,
            GapKind.NO_FREEZE_FRAME,
            "freeze_frame",
            "Codes are stored but the vehicle returned no freeze frame, so the conditions when "
            "the code set are unknown.",
            source=SourceType.OBD,
        )

    # 5. readiness
    if snapshot.readiness:
        supported = [m for m in snapshot.readiness if m.supported]
        incomplete = [m.name.value for m in supported if m.complete is False]
        if incomplete:
            summary = (
                f"{len(incomplete)} of {len(supported)} supported monitors incomplete: "
                f"{', '.join(incomplete)}."
            )
        else:
            summary = f"All {len(supported)} supported monitors are complete."
        store.add(
            EvidenceKind.READINESS,
            Tier.T1,
            _obd(now),
            summary,
            {
                "monitors": {
                    m.name.value: (
                        "unsupported"
                        if not m.supported
                        else "complete"
                        if m.complete
                        else "incomplete"
                    )
                    for m in snapshot.readiness
                },
                "supported": quantity(len(supported), "count"),
                "incomplete": quantity(len(incomplete), "count"),
            },
            dedupe_key="readiness",
        )
    else:
        _gap(
            store,
            now,
            GapKind.READ_ERROR,
            "readiness",
            "The vehicle did not answer the status request, so the MIL state, code count and "
            "readiness monitors are unknown.",
            source=SourceType.OBD,
            pid="0101",
        )

    # 6. since clear
    since = snapshot.since_clear
    since_parts: list[tuple[str, str, float | None, str]] = [  # payload key, words, value, unit
        ("warmups", "warm-ups", since.warmups, "count"),
        ("distance_km", "", since.distance_km, "km"),
        ("minutes_since_clear", "of running", since.minutes_since_clear, "min"),
        ("minutes_mil_on", "with the MIL on", since.minutes_mil_on, "min"),
        ("distance_mil_on_km", "with the MIL on", since.distance_mil_on_km, "km"),
    ]
    known = [
        (key, words, value, unit) for key, words, value, unit in since_parts if value is not None
    ]
    if known:
        phrases = [
            f"{fmt(value, None if unit == 'count' else unit)} {words}".strip()
            for _, words, value, unit in known
        ]
        store.add(
            EvidenceKind.SINCE_CLEAR,
            Tier.T1,
            _obd(now),
            f"Since codes were last cleared: {', '.join(phrases)}.",
            {key: quantity(value, unit) for key, _, value, unit in known},
            dedupe_key="since_clear",
        )

    # 7. live readings: per condition in capture order, per PID in PID order
    _add_live(store, snapshot.readings, snapshot.conditions_captured, batch=0, now=now)

    # 8. derived metrics
    derive(store, snapshot, profile, now)

    # 9-10. recalls and the complaint summary
    corpus = ComplaintCorpus([], profile.candidate_names)
    if nhtsa is not None:
        names = nhtsa.candidate_names
        tried = ", ".join(names)
        corpus = ComplaintCorpus(nhtsa.complaints, names)
        for recall in nhtsa.recalls:
            store.add(
                EvidenceKind.RECALL,
                Tier.T1,
                EvidenceSource(
                    type=SourceType.NHTSA_RECALLS,
                    url=f"https://www.nhtsa.gov/recalls?nhtsaId={recall.campaign_number}",
                    retrieved_at=now,
                ),
                f"NHTSA recall {recall.campaign_number} ({recall.component}): "
                f"{_cut(recall.summary, RECALL_SUMMARY_CHARS)}",
                {
                    "campaign_number": recall.campaign_number,
                    "component": recall.component,
                    "summary": recall.summary,
                    "consequence": recall.consequence,
                    "remedy": recall.remedy,
                    "report_date": recall.report_date,
                },
                matched_name=", ".join(recall.matched_names) or None,
                dedupe_key=f"recall:{recall.campaign_number}",
            )
        if not nhtsa.recalls and nhtsa.failed("recalls") < len(names):
            _gap(
                store,
                now,
                GapKind.NHTSA_NONE_FOUND,
                "recalls",
                f"No recalls found under names: {tried}. This is not proof that none exist.",
                source=SourceType.NHTSA_RECALLS,
                names_tried=list(names),
            )
        if nhtsa.complaints:
            by_component = Counter(
                c for complaint in nhtsa.complaints for c in complaint.components
            )
            ranked = sorted(by_component.items(), key=lambda item: (-item[1], item[0]))
            top = ", ".join(f"{name} ({count})" for name, count in ranked[:5])
            store.add(
                EvidenceKind.COMPLAINT,
                Tier.T3,
                EvidenceSource(type=SourceType.NHTSA_COMPLAINTS, retrieved_at=now),
                f"{len(nhtsa.complaints)} owner complaints on file under names: {tried} "
                f"(unverified). Top components: {top}.",
                {
                    "total": quantity(len(nhtsa.complaints), "count"),
                    "by_component": {name: quantity(count, "count") for name, count in ranked},
                    "names_tried": list(names),
                },
                matched_name=", ".join(nhtsa.matched_names.get("complaints", [])) or None,
                dedupe_key="complaint:summary",
            )
        elif nhtsa.failed("complaints") < len(names):
            _gap(
                store,
                now,
                GapKind.NHTSA_NONE_FOUND,
                "complaints",
                f"No owner complaints found under names: {tried}. "
                "This is not proof that none exist.",
                source=SourceType.NHTSA_COMPLAINTS,
                names_tried=list(names),
            )

    # 11. remaining gaps
    if snapshot.vin is None:
        _gap(
            store,
            now,
            GapKind.NO_VIN,
            "vin",
            "The vehicle did not report a VIN, so it could not be identified automatically.",
            source=SourceType.OBD,
        )
    if nhtsa is None:
        _gap(
            store,
            now,
            GapKind.NHTSA_UNAVAILABLE,
            "all",
            "NHTSA recalls and complaints were not looked up for this vehicle. "
            "This is not evidence that none exist.",
        )
    else:
        for index, error in enumerate(nhtsa.errors):
            _gap(
                store,
                now,
                GapKind.NHTSA_UNAVAILABLE,
                str(index),
                f"An NHTSA lookup failed ({_cut(error, 160)}). This is not evidence of absence.",
            )
    for pid in CORE_PIDS:
        if pid not in snapshot.supported_pids:
            _gap(
                store,
                now,
                GapKind.PID_UNSUPPORTED,
                pid,
                f"The vehicle does not report {_label(pid)} (PID {pid}).",
                source=SourceType.OBD,
                pid=pid,
            )
    seen: list[tuple[str, str]] = []
    for error in snapshot.read_errors:
        subject = (error.kind.value, error.request)
        if subject in seen:
            continue
        if error.request == "0101" and not snapshot.readiness:
            continue  # already reported in step 5
        seen.append(subject)
        if len(seen) > MAX_READ_ERROR_GAPS:
            break
        detail = f" ({_cut(error.detail or '', 120)})" if _detail_is_shareable(error) else ""
        _gap(
            store,
            now,
            GapKind.READ_ERROR,
            f"{error.kind.value}:{error.request}",
            f"Reading {error.request} failed: {error.kind.value}{detail}.",
            source=SourceType.OBD,
        )
    return corpus


# ---- live readings ---------------------------------------------------------


def _add_live(
    store: EvidenceStore,
    readings: Sequence[Reading],
    conditions: Sequence[Condition],
    batch: int,
    now: datetime,
) -> list[Evidence]:
    """One item per (condition, PID), aggregating that phase's samples."""
    added: list[Evidence] = []
    for condition in conditions:
        phase = [r for r in readings if r.condition is condition]
        for pid in sorted({r.pid for r in phase}):
            samples = [r for r in phase if r.pid == pid]
            key = f"live:{pid}:{condition.value}:{batch}"
            if store.find(key) is not None:
                continue
            name = samples[0].name
            numbers = [r.value for r in samples if isinstance(r.value, float)]
            base = {"pid": pid, "name": name, "condition": condition.value}
            if numbers and pid in PID_TABLE:
                spec = PID_TABLE[pid]
                unit, decimals = spec.unit or "count", spec.decimals
                mean = round(fmean(numbers), decimals)
                added.append(
                    store.add(
                        EvidenceKind.LIVE_READING,
                        Tier.T1,
                        _obd(now),
                        f"{_label(pid)[:1].upper()}{_label(pid)[1:]} at {condition.value}: "
                        f"{_fmt_pid(pid, mean)} (n={len(numbers)}, "
                        f"min {_fmt_pid(pid, min(numbers))}, max {_fmt_pid(pid, max(numbers))}).",
                        {
                            **base,
                            "n": quantity(len(numbers), "count"),
                            "mean": quantity(mean, unit, decimals),
                            "min": quantity(min(numbers), unit, decimals),
                            "max": quantity(max(numbers), unit, decimals),
                        },
                        dedupe_key=key,
                    )
                )
                continue
            texts = [r.value for r in samples if isinstance(r.value, str)]
            if texts:
                added.append(
                    store.add(
                        EvidenceKind.LIVE_READING,
                        Tier.T1,
                        _obd(now),
                        f"{name} at {condition.value}: {texts[-1]}.",
                        {**base, "value": texts[-1]},
                        dedupe_key=key,
                    )
                )
    return added


def add_live_readings(
    store: EvidenceStore, readings: Sequence[Reading], now: datetime
) -> list[Evidence]:
    """Evidence for one `read_live_data` call; each call is a new batch."""
    batch = store.next_live_batch()
    conditions: list[Condition] = []
    for reading in readings:
        if reading.condition not in conditions:
            conditions.append(reading.condition)
    return _add_live(store, readings, conditions, batch, now)


# ---- derived metrics -------------------------------------------------------


def _derived(
    store: EvidenceStore,
    now: datetime,
    metric: str,
    subject: str,
    summary: str,
    inputs: Sequence[Evidence],
    **quantities: Any,
) -> Evidence:
    tiers = [i.tier for i in inputs]
    tier = Tier.T3 if Tier.T3 in tiers else Tier.T2 if Tier.T2 in tiers else Tier.T1
    return store.add(
        EvidenceKind.DERIVED,
        tier,
        _computed(now),
        summary,
        {"metric": metric, "inputs": [i.id for i in inputs], **quantities},
        dedupe_key=f"derived:{metric}:{subject}",
    )


def _mean_of(item: Evidence | None) -> float | None:
    if item is None:
        return None
    mean = item.payload.get("mean")
    return float(cast(dict[str, Any], mean)["value"]) if isinstance(mean, dict) else None


def _total_trims(store: EvidenceStore, condition: str, batch: int, now: datetime) -> list[Evidence]:
    made: list[Evidence] = []
    for bank, stft_pid, ltft_pid in _TRIM_PAIRS:
        stft_item = store.find(f"live:{stft_pid}:{condition}:{batch}")
        ltft_item = store.find(f"live:{ltft_pid}:{condition}:{batch}")
        stft, ltft = _mean_of(stft_item), _mean_of(ltft_item)
        if stft is None or ltft is None or stft_item is None or ltft_item is None:
            continue
        total = round(stft + ltft, 1)
        made.append(
            _derived(
                store,
                now,
                "total_fuel_trim",
                f"b{bank}:{condition}:{batch}",
                f"Total fuel trim bank {bank} at {condition}: {fmt(total, '%', 1, True)} "
                f"(STFT {fmt(stft, '%', 1, True)} plus LTFT {fmt(ltft, '%', 1, True)}).",
                [stft_item, ltft_item],
                total=quantity(total, "%", 1),
                stft=quantity(stft, "%", 1),
                ltft=quantity(ltft, "%", 1),
                bank=quantity(bank, "bank"),
                condition=condition,
            )
        )
    return made


def _trim_delta(store: EvidenceStore, now: datetime) -> list[Evidence]:
    """Total trim at hold_2500 minus total trim at idle, using the newest of each."""
    made: list[Evidence] = []
    for bank in (1, 2):
        latest: dict[str, Evidence] = {}
        for item in store.by_kind(EvidenceKind.DERIVED):
            payload = item.payload
            if payload.get("metric") == "total_fuel_trim" and payload["bank"]["value"] == bank:
                latest[str(payload.get("condition"))] = item
        idle, hold = latest.get(Condition.IDLE.value), latest.get(Condition.HOLD_2500.value)
        if idle is None or hold is None:
            continue
        idle_total = float(idle.payload["total"]["value"])
        hold_total = float(hold.payload["total"]["value"])
        delta = round(hold_total - idle_total, 1)
        made.append(
            _derived(
                store,
                now,
                "fuel_trim_delta",
                f"b{bank}:{idle.id}:{hold.id}",
                f"Total fuel trim bank {bank} changes by {fmt(delta, '%', 1, True)} from idle to "
                f"hold_2500 (idle {fmt(idle_total, '%', 1, True)}, hold_2500 "
                f"{fmt(hold_total, '%', 1, True)}).",
                [idle, hold],
                delta=quantity(delta, "%", 1),
                idle_total=quantity(idle_total, "%", 1),
                hold_total=quantity(hold_total, "%", 1),
                bank=quantity(bank, "bank"),
            )
        )
    return made


def derive(
    store: EvidenceStore, snapshot: Snapshot, profile: VehicleProfile, now: datetime
) -> list[Evidence]:
    """Derived metrics for the scan (batch 0). Returns the new items."""
    before = len(store.all())
    live = {c.value for c in snapshot.conditions_captured}

    # total_fuel_trim: per bank, per live phase and for the freeze frame
    for condition in (c.value for c in snapshot.conditions_captured):
        _total_trims(store, condition, 0, now)
    frame_item = store.find("freeze_frame")
    if frame_item is not None:
        frozen = cast(dict[str, dict[str, Any]], frame_item.payload["readings"])
        for bank in (1, 2):
            stft, ltft = frozen.get(f"stft_b{bank}"), frozen.get(f"ltft_b{bank}")
            if stft is None or ltft is None:
                continue
            total = round(float(stft["value"]) + float(ltft["value"]), 1)
            _derived(
                store,
                now,
                "total_fuel_trim",
                f"b{bank}:freeze_frame",
                f"Total fuel trim bank {bank} in the freeze frame: {fmt(total, '%', 1, True)} "
                f"(STFT {fmt(stft['value'], '%', 1, True)} plus "
                f"LTFT {fmt(ltft['value'], '%', 1, True)}).",
                [frame_item],
                total=quantity(total, "%", 1),
                stft=quantity(stft["value"], "%", 1),
                ltft=quantity(ltft["value"], "%", 1),
                bank=quantity(bank, "bank"),
                condition="freeze_frame",
            )

    # fuel_trim_delta
    _trim_delta(store, now)

    # maf_vs_expected: MAF at warm idle against a band scaled by displacement
    maf_item = store.find(f"live:{NAME_TO_PID['maf_gs']}:idle:0")
    coolant_item = store.find(f"live:{NAME_TO_PID['ect_c']}:idle:0")
    maf, coolant = _mean_of(maf_item), _mean_of(coolant_item)
    litres = profile.displacement_l
    if (
        maf_item is not None
        and coolant_item is not None
        and maf is not None
        and coolant is not None
        and coolant >= WARM_COOLANT_C
        and litres
    ):
        low, high = round(MAF_BAND_LOW_PER_L * litres, 1), round(MAF_BAND_HIGH_PER_L * litres, 1)
        position = "below" if maf < low else "above" if maf > high else "within"
        _derived(
            store,
            now,
            "maf_vs_expected",
            "idle",
            f"MAF at warm idle is {fmt(maf, 'g/s', 2)}, {position} the rough expected band of "
            f"{fmt(low, 'g/s', 1)} to {fmt(high, 'g/s', 1)} for a {fmt(litres, 'L', 1)} engine.",
            [maf_item, coolant_item],
            maf=quantity(maf, "g/s", 2),
            expected_low=quantity(low, "g/s", 1),
            expected_high=quantity(high, "g/s", 1),
            displacement=quantity(litres, "L", 1),
            position=position,
        )

    # warmup_after_runtime: still cool after ten minutes or more of running
    for condition in (c.value for c in snapshot.conditions_captured):
        runtime_item = store.find(f"live:{NAME_TO_PID['runtime_s']}:{condition}:0")
        ect_item = store.find(f"live:{NAME_TO_PID['ect_c']}:{condition}:0")
        runtime, ect = _mean_of(runtime_item), _mean_of(ect_item)
        if runtime_item is None or ect_item is None or runtime is None or ect is None:
            continue
        if runtime < WARMUP_MIN_RUNTIME_S:
            continue
        status = "below_normal" if ect < WARM_COOLANT_C else "normal"
        words = "below normal operating temperature" if status == "below_normal" else "normal"
        _derived(
            store,
            now,
            "warmup_after_runtime",
            condition,
            f"After {fmt(runtime / 60, 'min')} of running, coolant is {fmt(ect, '°C')} "
            f"at {condition}: {words}.",
            [runtime_item, ect_item],
            runtime=quantity(runtime / 60, "min"),
            coolant=quantity(ect, "°C"),
            status=status,
        )

    # warmup_rate: coolant rise during cold-start readings at least a minute apart
    if Condition.COLD_START.value in live:
        cold = sorted(
            (
                r
                for r in snapshot.readings
                if r.condition is Condition.COLD_START
                and r.pid == NAME_TO_PID["ect_c"]
                and isinstance(r.value, float)
            ),
            key=lambda r: r.ts,
        )
        cold_item = store.find(f"live:{NAME_TO_PID['ect_c']}:cold_start:0")
        if len(cold) >= 2 and cold_item is not None:
            span_s = (cold[-1].ts - cold[0].ts).total_seconds()
            if span_s >= WARMUP_RATE_MIN_SPAN_S:
                first, last = cast(float, cold[0].value), cast(float, cold[-1].value)
                minutes = round(span_s / 60, 1)
                rate = round((last - first) / (span_s / 60), 1)
                _derived(
                    store,
                    now,
                    "warmup_rate",
                    "cold_start",
                    f"Coolant rose from {fmt(first, '°C')} to {fmt(last, '°C')} in "
                    f"{fmt(minutes, 'min', 1)}: {fmt(rate, '°C/min', 1)}.",
                    [cold_item],
                    rate=quantity(rate, "°C/min", 1),
                    first=quantity(first, "°C"),
                    last=quantity(last, "°C"),
                    minutes=quantity(minutes, "min", 1),
                )

    # readiness_summary and recently_cleared
    readiness_item = store.find("readiness")
    if readiness_item is not None:
        non_continuous = [
            m for m in snapshot.readiness if m.supported and m.name not in CONTINUOUS_MONITORS
        ]
        incomplete = sum(1 for m in non_continuous if m.complete is False)
        _derived(
            store,
            now,
            "readiness_summary",
            "scan",
            f"{incomplete} of {len(non_continuous)} supported non-continuous monitors are "
            "incomplete.",
            [readiness_item],
            supported=quantity(len(non_continuous), "count"),
            incomplete=quantity(incomplete, "count"),
        )
        since_item = store.find("since_clear")
        since = snapshot.since_clear
        if since_item is not None and since.warmups is not None and since.distance_km is not None:
            flag = (
                since.warmups <= RECENT_CLEAR_MAX_WARMUPS
                and since.distance_km <= RECENT_CLEAR_MAX_KM
                and incomplete >= 1
            )
            facts = (
                f"{since.warmups} warm-ups and {fmt(since.distance_km, 'km')} since codes were "
                f"cleared, with {incomplete} monitors incomplete"
            )
            _derived(
                store,
                now,
                "recently_cleared",
                "scan",
                f"Codes were probably cleared recently: {facts}."
                if flag
                else f"No sign of a recent code clear: {facts}.",
                [since_item, readiness_item],
                warmups=quantity(since.warmups, "count"),
                distance=quantity(since.distance_km, "km"),
                incomplete=quantity(incomplete, "count"),
                flag=flag,
            )
    return store.all()[before:]


def derive_live(
    store: EvidenceStore, new_items: Sequence[Evidence], now: datetime
) -> list[Evidence]:
    """Total fuel trim (and the idle-to-hold delta) for readings from `read_live_data`."""
    before = len(store.all())
    phases: list[tuple[str, int]] = []
    for item in new_items:
        key = store.key_of(item.id)
        if item.kind is not EvidenceKind.LIVE_READING or key is None:
            continue
        _, _, condition, batch = key.split(":")
        if (condition, int(batch)) not in phases:
            phases.append((condition, int(batch)))
    for condition, batch in phases:
        _total_trims(store, condition, batch, now)
    if phases:
        _trim_delta(store, now)
    return store.all()[before:]
