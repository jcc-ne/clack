"""Main Textual application for clack."""

from __future__ import annotations

import duckdb
from textual import work
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.widgets import Footer, Header, Input, LoadingIndicator, TabbedContent, TabPane, Tree
from textual.worker import Worker, WorkerState

from clack.version import get_version
from clack.widgets.dashboard import DashboardTab
from clack.widgets.dialog_viewer import DialogViewer
from clack.widgets.query_console import QueryConsole
from clack.widgets.stats import StatsTab


class ClackApp(App):
    CSS_PATH = "css/app.tcss"
    TITLE = f"clack {get_version()}"
    BINDINGS = [
        Binding("1", "show_tab('dashboard')", "Dashboard", show=True),
        Binding("2", "show_tab('stats')", "Stats", show=True),
        Binding("3", "show_tab('dialog')", "Dialog", show=True),
        Binding("4", "show_tab('query')", "Query", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("t", "switch_theme", "Theme", show=True),
        Binding("a", "load_all_history", "Load All", show=True),
        Binding("G", "nav_end", show=False),
        Binding("ctrl+f", "nav_page_down", show=False),
        Binding("ctrl+b", "nav_page_up", show=False),
    ]

    THEMES = ("solarized-dark", "solarized-light")

    db: duckdb.DuckDBPyConnection | None = None
    _dashboard: DashboardTab | None = None
    _full_history_loaded: bool = False
    _g_pending: bool = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield LoadingIndicator(id="loading-indicator")
        with TabbedContent(id="tabs"):
            with TabPane("Dashboard", id="dashboard"):
                yield DashboardTab()
            with TabPane("Stats", id="stats"):
                yield StatsTab()
            with TabPane("Dialog", id="dialog"):
                yield DialogViewer()
            with TabPane("Query", id="query"):
                yield QueryConsole()
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "solarized-dark"
        self.query_one("#tabs").display = False
        self._load_data()
        self.set_interval(60, self._auto_refresh_dashboard)

    @work(thread=True, group="db_init")
    def _load_data(self) -> duckdb.DuckDBPyConnection:
        from clack.db import get_connection

        return get_connection()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group == "db_init" and event.state == WorkerState.SUCCESS:
            self.db = event.worker.result
            self.query_one("#loading-indicator").display = False
            self.query_one("#tabs").display = True
            assert self.db is not None
            self._dashboard = self.query_one(DashboardTab)
            self._dashboard.load_data(self.db)
            self.query_one(StatsTab).load_data(self.db)
            self.query_one(QueryConsole).set_db(self.db)

    def _auto_refresh_dashboard(self) -> None:
        """Periodic refresh for the dashboard tab."""
        if self.db is not None and self._dashboard is not None:
            try:
                self._dashboard._refresh_data()
            except Exception:
                pass

    def action_load_all_history(self) -> None:
        """Pull archived transcripts into raw_records for the Query tab.

        Sessions older than the archive cutoff are held only as aggregates, so
        SQL against raw_records omits their transcripts. This trades memory to
        make that complete; restart to get the memory back.
        """
        if self.db is None:
            return
        if self._full_history_loaded:
            self.notify("Full history is already loaded.")
            return
        from clack.db import has_archived_sessions

        if not has_archived_sessions(self.db):
            self.notify("No archived sessions — raw_records already covers all history.")
            return
        self.notify("Loading archived transcripts...")
        self._load_all_history()

    @work(thread=True, group="hydrate", exclusive=True)
    def _load_all_history(self) -> None:
        from clack.db import load_full_history

        assert self.db is not None
        try:
            sessions, records = load_full_history(self.db)
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"Load failed: {exc}", severity="error"
            )
            return
        self._full_history_loaded = True
        self.call_from_thread(self._on_full_history_loaded, sessions, records)

    def _on_full_history_loaded(self, sessions: int, records: int) -> None:
        self.query_one(QueryConsole).set_full_history_loaded()
        self.notify(
            f"Loaded {records:,} records from {sessions} archived sessions — "
            "raw_records now covers all history."
        )

    def on_key(self, event: Key) -> None:
        # Skip vim nav when an Input widget has focus
        if isinstance(self.focused, Input):
            self._g_pending = False
            return
        if event.key == "g":
            if self._g_pending:
                # gg -> go to top
                self._g_pending = False
                event.prevent_default()
                self.action_nav_home()
            else:
                self._g_pending = True
                event.prevent_default()
            return
        self._g_pending = False

    def _nav_action(self, *actions: str) -> None:
        """Try navigation actions on the focused widget, using the first one found."""
        widget = self.focused
        if widget is None:
            return
        for action in actions:
            method = getattr(widget, f"action_{action}", None)
            if method is not None:
                try:
                    method()
                except SkipAction:
                    continue
                return

    def action_nav_home(self) -> None:
        self._nav_action("scroll_top", "scroll_home")

    def action_nav_end(self) -> None:
        self._nav_action("scroll_bottom", "scroll_end")

    def action_nav_page_down(self) -> None:
        self._nav_action("page_down")

    def action_nav_page_up(self) -> None:
        self._nav_action("page_up")

    def action_switch_theme(self) -> None:
        current = self.THEMES.index(self.theme) if self.theme in self.THEMES else -1
        self.theme = self.THEMES[(current + 1) % len(self.THEMES)]

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def show_dialog(self, session_id: str, title: str) -> None:
        """Switch to dialog tab and load a session."""
        self.query_one(TabbedContent).active = "dialog"
        assert self.db is not None
        viewer = self.query_one(DialogViewer)
        viewer.load_session(self.db, session_id, title)
        viewer.query_one("#dialog-tree", Tree).focus()
