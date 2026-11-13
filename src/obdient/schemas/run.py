"""Run trace: every LLM call and tool call of one diagnosis (SPEC §3.5)."""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field

from obdient.config import Budgets, ModelRole
from obdient.schemas.common import SCHEMA_VERSION, Model, UtcDatetime
from obdient.schemas.report import Mode


class StopReason(StrEnum):
    SUBMITTED = "submitted"
    MAX_TURNS = "max_turns"
    MAX_REVISIONS = "max_revisions"
    ERROR = "error"


class Strategy(StrEnum):
    AGENT = "agent"
    BASELINE = "baseline"


class LlmStep(Model):
    type: Literal["llm"] = "llm"
    turn: int
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    usd: float
    latency_ms: int
    tool_calls: list[str]  # tool names requested in this turn
    error: str | None = None


class ToolStep(Model):
    type: Literal["tool"] = "tool"
    turn: int
    name: str
    args: dict[str, Any]
    result_summary: Annotated[str, Field(max_length=300)]
    error: str | None = None  # the exact error message returned to the model
    evidence_ids: list[str] = []  # evidence created or returned
    latency_ms: int


class RunConfig(Model):
    strategy: Strategy
    mode: Mode
    role: ModelRole
    model: str
    budgets: Budgets
    prompt_hash: str  # §7.2
    obdex_commit: str
    wal33d_commit: str


Step = Annotated[LlmStep | ToolStep, Field(discriminator="type")]


class Run(Model):
    run_id: str
    schema_version: int = SCHEMA_VERSION
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    snapshot_id: str
    scenario_id: str | None
    config: RunConfig
    steps: list[Step]
    stop_reason: StopReason | None
    error: str | None
    turns: int
    revisions: int
    total_usd: float
    report_id: str | None
