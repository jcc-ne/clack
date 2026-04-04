"""Query console widget — interactive DuckDB SQL REPL."""

from __future__ import annotations

import duckdb
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import DataTable, Input, Static


class QueryConsole(Widget):
    BINDINGS = [
        Binding("ctrl+j", "execute", "Execute", show=True),
    ]

    _db: duckdb.DuckDBPyConnection | None = None
    _history: list[str] = []
    _history_idx: int = -1

    def compose(self) -> ComposeResult:
        yield Static(
            "Views: v_sessions, v_assistant_turns, v_stats, v_sessions_by_day, raw_records",
            id="views-help",
        )
        yield DataTable(id="query-results")
        yield Input(
            placeholder="SQL> SELECT * FROM v_sessions LIMIT 10  (Enter to execute)",
            id="query-input",
        )
        yield Static("Ready", id="query-status")

    def on_mount(self) -> None:
        table = self.query_one("#query-results", DataTable)
        table.cursor_type = "row"

    def set_db(self, db: duckdb.DuckDBPyConnection) -> None:
        self._db = db

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "query-input":
            sql = event.value.strip()
            if sql:
                self._history.append(sql)
                self._history_idx = len(self._history)
                self._run_query(sql)

    def action_execute(self) -> None:
        inp = self.query_one("#query-input", Input)
        sql = inp.value.strip()
        if sql:
            self._history.append(sql)
            self._history_idx = len(self._history)
            self._run_query(sql)

    @work(thread=True, exclusive=True, group="query")
    def _run_query(self, sql: str) -> None:
        status = self.query_one("#query-status", Static)
        self.app.call_from_thread(status.update, f"Running: {sql[:60]}...")

        try:
            assert self._db is not None
            result = self._db.execute(sql)
            columns = [desc[0] for desc in result.description]
            rows = result.fetchmany(500)
            self.app.call_from_thread(self._show_results, columns, rows, len(rows))
        except Exception as e:
            self.app.call_from_thread(self._show_error, str(e))

    def _show_results(self, columns: list[str], rows: list, count: int) -> None:
        table = self.query_one("#query-results", DataTable)
        table.clear(columns=True)
        for col in columns:
            table.add_column(col, key=col)
        for row in rows:
            table.add_row(*[str(v)[:200] if v is not None else "" for v in row])

        status = self.query_one("#query-status", Static)
        suffix = " (showing first 500)" if count >= 500 else ""
        status.update(f"{count} rows returned{suffix}")

    def _show_error(self, error: str) -> None:
        status = self.query_one("#query-status", Static)
        status.update(f"Error: {error}")
