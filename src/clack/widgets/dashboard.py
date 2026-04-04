"""Dashboard widget — session list with search and detail bar."""

from __future__ import annotations

import duckdb
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import DataTable, Input, Static

from clack.models import SessionSummary


class DashboardTab(Widget):
    can_focus = False

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("v", "view_dialog", "View Dialog"),
        Binding("slash", "focus_search", "Search"),
        Binding("escape", "clear_search", "Clear Search"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[SessionSummary] = []
        self.filtered: list[SessionSummary] = []
        self._db: duckdb.DuckDBPyConnection | None = None

    def compose(self) -> ComposeResult:
        yield Input(placeholder="/ Search sessions...", id="search-input")
        yield DataTable(id="session-table")
        yield Static("Select a session to see details", id="detail-bar")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_column("Date", width=10)
        table.add_column("Project", width=18)
        table.add_column("Summary", width=45)
        table.add_column("Model", width=14)
        table.add_column("Turns", width=5)

    def load_data(self, db: duckdb.DuckDBPyConnection) -> None:
        """Called from app after DB is ready. Runs on main thread."""
        self._db = db
        self._fetch_and_populate()

    def _fetch_and_populate(self) -> None:
        from clack.db import get_sessions

        assert self._db is not None
        self.sessions = get_sessions(self._db)
        self.filtered = list(self.sessions)
        self._populate_table()

    def _populate_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        seen_ids: set[str] = set()
        for s in self.filtered:
            if s.session_id in seen_ids:
                continue
            seen_ids.add(s.session_id)
            short_project = s.cwd.rstrip("/").split("/")[-1] if s.cwd else "?"
            short_model = (
                (s.primary_model or "?")
                .replace("claude-", "")
                .replace("-20250929", "")
            )
            date = s.started_at[:10] if s.started_at else "?"
            summary = s.title or s.summary[:80]
            table.add_row(
                date, short_project, summary, short_model,
                str(s.turn_count), key=s.session_id,
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        sid = event.row_key.value if event.row_key else None
        if sid is not None:
            session = self._find_session(str(sid))
            if session:
                detail = self.query_one("#detail-bar", Static)
                branch = (
                    f"  branch: {session.git_branch}"
                    if session.git_branch else ""
                )
                detail.update(
                    f"cwd: {session.cwd or '?'}{branch}  "
                    f"turns: {session.turn_count}  "
                    f"ver: {session.version or '?'}  "
                    f"id: {session.session_id[:8]}"
                )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter pressed — resume session in tmux."""
        sid = event.row_key.value if event.row_key else None
        if sid is not None:
            session = self._find_session(str(sid))
            if session:
                from clack.tmux import resume_session

                resume_session(self.app, session.session_id, session.cwd or ".")

    def action_view_dialog(self) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is not None:
            cursor_row = table.cursor_row
            keys = list(table.rows.keys())
            if cursor_row < len(keys):
                sid = keys[cursor_row].value
                session = self._find_session(sid)
                if session:
                    self.app.show_dialog(  # type: ignore[attr-defined]
                        sid, session.title or session.summary[:40],
                    )

    def action_refresh(self) -> None:
        if self._db:
            self._fetch_and_populate()

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_clear_search(self) -> None:
        inp = self.query_one("#search-input", Input)
        inp.value = ""
        self.filtered = list(self.sessions)
        self._populate_table()
        self.query_one(DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.lower().strip()
        if not query:
            self.filtered = list(self.sessions)
        else:
            self.filtered = [
                s for s in self.sessions
                if query in (s.summary or "").lower()
                or query in (s.title or "").lower()
                or query in (s.cwd or "").lower()
                or query in (s.primary_model or "").lower()
            ]
        self._populate_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one(DataTable).focus()

    def _find_session(self, session_id: str) -> SessionSummary | None:
        for s in self.sessions:
            if s.session_id == session_id:
                return s
        return None
