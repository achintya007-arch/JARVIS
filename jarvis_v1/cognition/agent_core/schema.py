"""
AgentCore schema — shared data types for the agentic loop.

Ported from claw-code:
  - TurnResult         → AgentResult
  - PermissionDenial   → from claw-code/src/models.py (was a stub here)
  - UsageSummary       → from claw-code/src/models.py, tracks Ollama token counts
  - stop_reason        → StopReason literal
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

StopReason = Literal["done", "max_turns", "error"]


# ── Ported from claw-code/src/models.py ──────────────────────────────────────

@dataclass(frozen=True)
class PermissionDenial:
    """
    Records a blocked tool call. Mirrors claw-code PermissionDenial.
    Collected per-run and surfaced in AgentResult.
    """
    tool_name: str
    reason: str


@dataclass(frozen=True)
class UsageSummary:
    """
    Token usage across one agentic run. Mirrors claw-code UsageSummary
    but uses real Ollama token counts (prompt_eval_count / eval_count)
    instead of word-count estimates.
    """
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, input_tokens: int, output_tokens: int) -> UsageSummary:
        return UsageSummary(
            input_tokens=self.input_tokens + input_tokens,
            output_tokens=self.output_tokens + output_tokens,
        )

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __str__(self) -> str:
        return f"in={self.input_tokens} out={self.output_tokens} total={self.total}"


# ── Core agent types ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation parsed from an LLM response."""
    tool: str
    args: dict

    def to_executor_step(self) -> dict:
        return {"tool": self.tool, "args": self.args}


@dataclass
class TurnState:
    """
    Mutable state carried across one agentic loop iteration.

    Mirrors QueryEnginePort's mutable_messages + turn tracking,
    extended to accumulate usage and permission denials per-run.
    """
    messages: list[dict]
    turn: int = 0
    tool_calls_made: list[str] = field(default_factory=list)
    last_response: str = ""
    usage: UsageSummary = field(default_factory=UsageSummary)
    permission_denials: list[PermissionDenial] = field(default_factory=list)


@dataclass(frozen=True)
class AgentResult:
    """
    Final result returned from AgentCore to Assistant.

    Extended with UsageSummary and PermissionDenial tracking
    ported from claw-code TurnResult.
    """
    response: str
    used_tools: tuple[str, ...]
    turns: int
    stop_reason: StopReason
    usage: UsageSummary = field(default_factory=UsageSummary)
    permission_denials: tuple[PermissionDenial, ...] = field(default_factory=tuple)

    @property
    def used_any_tools(self) -> bool:
        return len(self.used_tools) > 0

    @property
    def was_denied(self) -> bool:
        return len(self.permission_denials) > 0
