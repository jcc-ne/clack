"""Stats panel widget — model breakdown and daily trends."""

from __future__ import annotations

import duckdb
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import DataTable, Static

from clack.models import DayStats, ModelStats


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


class StatsTab(Widget):
    _db: duckdb.DuckDBPyConnection | None = None

    def compose(self) -> ComposeResult:
        yield DataTable(id="model-table")
        yield Static("Loading stats...", id="stats-summary")

    def on_mount(self) -> None:
        table = self.query_one("#model-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Model", "Sessions", "Turns", "Input Tokens",
            "Output Tokens", "Cache Create", "Cache Read",
        )

    def load_data(self, db: duckdb.DuckDBPyConnection) -> None:
        """Called from app after DB is ready. Runs on main thread."""
        self._db = db
        from clack.db import get_daily_stats, get_model_stats

        model_stats = get_model_stats(db)
        daily_stats = get_daily_stats(db)
        self._populate(model_stats, daily_stats)

    def _populate(self, model_stats: list[ModelStats], daily_stats: list[DayStats]) -> None:
        table = self.query_one("#model-table", DataTable)
        table.clear()

        total_sessions = 0
        total_turns = 0
        total_input = 0
        total_output = 0

        for ms in model_stats:
            short_model = ms.model.replace("claude-", "").replace("-20250929", "")
            table.add_row(
                short_model,
                str(ms.session_count),
                str(ms.turn_count),
                _fmt_tokens(ms.total_input_tokens),
                _fmt_tokens(ms.total_output_tokens),
                _fmt_tokens(ms.total_cache_creation),
                _fmt_tokens(ms.total_cache_read),
            )
            total_sessions += ms.session_count
            total_turns += ms.turn_count
            total_input += ms.total_input_tokens
            total_output += ms.total_output_tokens

        # Build sparkline from daily stats (last 30 days)
        recent = daily_stats[-30:] if len(daily_stats) > 30 else daily_stats
        spark_chars = " ▁▂▃▄▅▆▇█"
        if recent:
            max_sessions = max(d.sessions for d in recent)
            if max_sessions > 0:
                sparkline = "".join(
                    spark_chars[min(int(d.sessions / max_sessions * 8), 8)]
                    for d in recent
                )
            else:
                sparkline = "▁" * len(recent)

            max_tokens = max(d.output_tokens for d in recent) if recent else 1
            if max_tokens > 0:
                token_sparkline = "".join(
                    spark_chars[min(int(d.output_tokens / max_tokens * 8), 8)]
                    for d in recent
                )
            else:
                token_sparkline = "▁" * len(recent)

            date_range = f"{recent[0].day} — {recent[-1].day}"
        else:
            sparkline = ""
            token_sparkline = ""
            date_range = ""

        summary = self.query_one("#stats-summary", Static)
        summary.update(
            f"Totals: {total_sessions} sessions, {total_turns:,} turns, "
            f"{_fmt_tokens(total_input)} input, {_fmt_tokens(total_output)} output\n\n"
            f"Sessions/day (last 30d): {sparkline}\n"
            f"Tokens/day  (last 30d): {token_sparkline}\n"
            f"{date_range}"
        )
