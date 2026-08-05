"""Dashboard widget — session list with search and detail bar."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import DataTable, Input, Static
from textual.widgets.data_table import CellDoesNotExist

from clack.github import PRInfo
from clack.models import SessionSummary

PROJECTS_DIR = Path.home() / ".claude/projects"
PR_COLUMN_KEY = "pr"


class DashboardTab(Widget):
    can_focus = False

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("v", "view_dialog", "View Dialog"),
        Binding("p", "open_pr", "Open PR"),
        Binding("slash", "focus_search", "Search"),
        Binding("escape", "clear_search", "Clear Search"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[SessionSummary] = []
        self.filtered: list[SessionSummary] = []
        self._db: duckdb.DuckDBPyConnection | None = None
        self._active_panes: dict[str, str] = {}  # session_id -> pane label
        self._session_states: dict[str, str] = {}  # session_id -> "working" | "waiting"
        # session_id -> PRInfo. Unlike the dicts above this is never cleared —
        # PR lookups are slow, so we keep showing the last known value while a
        # refresh is in flight.
        self._pr_info: dict[str, PRInfo] = {}
        self._data_loaded: bool = False

    def compose(self) -> ComposeResult:
        yield Input(placeholder="/ Search sessions (FTS)...", id="search-input")
        yield DataTable(id="session-table")
        yield Static("Select a session to see details", id="detail-bar")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.cursor_foreground_priority = "renderable"
        table.add_column("Live", width=14)
        table.add_column("Date", width=10)
        table.add_column("Updated", width=12)
        table.add_column("Project", width=18)
        table.add_column("Summary", width=35)
        table.add_column("PR", width=20, key=PR_COLUMN_KEY)
        table.add_column("Model", width=14)
        table.add_column("Turns", width=5)

    def load_data(self, db: duckdb.DuckDBPyConnection) -> None:
        """Called from app after DB is ready. Runs on main thread."""
        self._db = db
        self._fetch_and_populate()
        self._data_loaded = True

    def _fetch_and_populate(self, incremental: bool = False) -> None:
        from clack.db import get_sessions, refresh

        assert self._db is not None
        if incremental:
            refresh(self._db)
        self.sessions = get_sessions(self._db)
        self.filtered = list(self.sessions)
        self._refresh_active_panes()
        self._populate_table()
        self._fetch_pr_status()

    @work(thread=True, exclusive=True, group="pr")
    def _fetch_pr_status(self) -> None:
        """Look up PRs for every session's branch off the main thread.

        Results are applied per repo as they arrive, so the column fills in
        progressively rather than waiting on the slowest repo.
        """
        from clack.github import build_pr_map

        def on_partial(partial: dict[str, PRInfo]) -> None:
            self.app.call_from_thread(self._apply_pr_map, partial)

        try:
            build_pr_map(self.sessions, on_partial=on_partial)
        except Exception:
            pass

    def _apply_pr_map(self, mapping: dict[str, PRInfo]) -> None:
        """Patch PR cells in place — this lands while the user is scrolling."""
        self._pr_info.update(mapping)
        table = self.query_one(DataTable)
        for sid, pr in mapping.items():
            try:
                table.update_cell(sid, PR_COLUMN_KEY, _pr_cell(pr))
            except CellDoesNotExist:
                pass  # filtered out by search, or gone since the fetch started

    def _refresh_active_panes(self) -> None:
        from clack.tmux import get_active_claude_panes

        self._active_panes = {}
        self._session_states = {}
        for pane in get_active_claude_panes():
            if pane.session_id:
                self._active_panes[pane.session_id] = pane.label
                self._session_states[pane.session_id] = _detect_session_state(
                    pane.session_id
                )

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
            updated = _relative_time(s.last_active) if s.last_active else "?"
            summary = s.title or s.summary[:80]
            label = self._active_panes.get(s.session_id)
            if label:
                state = self._session_states.get(s.session_id, "working")
                if state == "waiting":
                    live = Text(f"● {label}", style="red")
                elif state == "done" and _is_recent(s.last_active):
                    live = Text(f"● {label}", style="yellow")
                elif state == "done":
                    live = Text(f"● {label}")
                else:
                    live = Text(f"● {label}", style="green")
            else:
                live = Text("")
            pr_cell = _pr_cell(self._pr_info.get(s.session_id))
            table.add_row(
                live, date, updated, short_project, summary, pr_cell, short_model,
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
                pr = self._pr_info.get(session.session_id)
                if pr:
                    icon, label, _ = pr.display
                    pr_part = f"  PR: {icon} {label} {pr.state.replace('_', ' ')}"
                else:
                    pr_part = ""
                detail.update(
                    f"cwd: {session.cwd or '?'}{branch}{pr_part}  "
                    f"turns: {session.turn_count}  "
                    f"ver: {session.version or '?'}  "
                    f"id: {session.session_id[:8]}"
                )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter pressed — jump to active pane or resume session."""
        sid = event.row_key.value if event.row_key else None
        if sid is None:
            return
        session = self._find_session(str(sid))
        if not session:
            return

        from clack.tmux import resume_session

        resume_session(self.app, session.session_id, session.cwd or ".")

    def action_view_dialog(self) -> None:
        sid = self._cursor_session_id()
        if sid is None:
            return
        session = self._find_session(sid)
        if session:
            self.app.show_dialog(  # type: ignore[attr-defined]
                sid, session.title or session.summary[:40],
            )

    def action_open_pr(self) -> None:
        """Open the highlighted session's PR in a browser (`gh pr view -w`)."""
        sid = self._cursor_session_id()
        if sid is None:
            return
        pr = self._pr_info.get(sid)
        if pr is None:
            self.notify("No PR found for this session's branch", severity="warning")
            return

        from clack.github import open_pr_in_browser

        if not open_pr_in_browser(pr):
            self.notify(f"Could not open {pr.label}", severity="error")

    def _cursor_session_id(self) -> str | None:
        table = self.query_one(DataTable)
        cursor_row = table.cursor_row
        if cursor_row is None:
            return None
        keys = list(table.rows.keys())
        if cursor_row >= len(keys):
            return None
        return keys[cursor_row].value

    def _refresh_data(self) -> None:
        if self._db and self._data_loaded:
            try:
                self._fetch_and_populate(incremental=True)
            except Exception:
                pass

    def action_refresh(self) -> None:
        if self._db:
            self._fetch_and_populate(incremental=True)

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_clear_search(self) -> None:
        inp = self.query_one("#search-input", Input)
        inp.value = ""
        self.filtered = list(self.sessions)
        self._populate_table()
        self.query_one(DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        if not query:
            self.filtered = list(self.sessions)
            self._populate_table()
            return

        from clack.db import search_sessions_fts

        matching_ids = search_sessions_fts(self._db, query) if self._db else None
        if matching_ids is not None:
            id_order = {sid: i for i, sid in enumerate(matching_ids)}
            self.filtered = sorted(
                [s for s in self.sessions if s.session_id in id_order],
                key=lambda s: id_order[s.session_id],
            )
        else:
            q = query.lower()
            self.filtered = [
                s for s in self.sessions
                if q in (s.summary or "").lower()
                or q in (s.title or "").lower()
                or q in (s.cwd or "").lower()
                or q in (s.primary_model or "").lower()
            ]
        self._populate_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one(DataTable).focus()

    def _find_session(self, session_id: str) -> SessionSummary | None:
        for s in self.sessions:
            if s.session_id == session_id:
                return s
        return None


def _pr_cell(pr: PRInfo | None) -> Text:
    """Render a PR as `<icon> repo#number`, or blank when there's no PR."""
    if pr is None:
        return Text("")
    icon, label, style = pr.display
    return Text(f"{icon} {label}", style=style)


def _relative_time(timestamp: str) -> str:
    """Convert a timestamp to a human-readable relative time string."""
    try:
        dt = datetime.fromisoformat(timestamp).astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        if days < 30:
            return f"{days}d ago"
        return timestamp[:10]
    except (ValueError, TypeError):
        return timestamp[:10] if timestamp else "?"


def _is_recent(timestamp: str, hours: int = 24) -> bool:
    """Return True if the timestamp is within the last N hours."""
    try:
        dt = datetime.fromisoformat(timestamp).astimezone(timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() < hours * 3600
    except (ValueError, TypeError):
        return False


def _detect_session_state(session_id: str) -> str:
    """Detect the state of a live session.

    Returns:
        "working" — actively generating or executing tools (green)
        "waiting" — blocked on tool permission approval (red)
        "done"    — finished work, stop_reason=end_turn (yellow)
    """
    for jsonl in PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        try:
            size = jsonl.stat().st_size
            with open(jsonl, "rb") as f:
                if size > 4096:
                    f.seek(size - 4096)
                    f.readline()  # skip partial first line
                data = f.read().decode("utf-8", errors="replace")

            for line in reversed(data.strip().splitlines()):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")
                if etype == "system":
                    continue  # skip turn_duration etc, look deeper
                if etype == "assistant":
                    msg = event.get("message", {})
                    stop = msg.get("stop_reason") if isinstance(msg, dict) else None
                    if stop == "end_turn":
                        return "done"
                    if stop == "tool_use":
                        return "waiting"  # blocked on tool permission
                    return "working"  # still streaming
                if etype == "user":
                    if event.get("isSidechain"):
                        continue  # tool result, keep looking
                    return "working"  # real user input → assistant processing
                # skip attachment, custom-title, etc.
        except OSError:
            pass
    return "working"
