"""Data models for clack."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SessionSummary:
    session_id: str
    started_at: str
    last_active: str
    cwd: str | None
    git_branch: str | None
    version: str | None
    title: str | None
    summary: str
    primary_model: str | None
    turn_count: int


@dataclass
class ModelStats:
    model: str
    session_count: int
    turn_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_cache_creation: int
    total_cache_read: int


@dataclass
class DayStats:
    day: str
    sessions: int
    turns: int
    output_tokens: int


@dataclass
class ToolCall:
    tool_name: str
    tool_input: dict
    tool_result: str | None
    is_error: bool
    tool_use_id: str | None = None


@dataclass
class DialogTurn:
    role: str  # "user", "assistant", "system"
    timestamp: str
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    duration_ms: int | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
