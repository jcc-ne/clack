"""Main Textual application for clack."""

from __future__ import annotations

import duckdb
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, LoadingIndicator, TabbedContent, TabPane, Tree
from textual.worker import Worker, WorkerState

from clack.widgets.dashboard import DashboardTab
from clack.widgets.dialog_viewer import DialogViewer
from clack.widgets.query_console import QueryConsole
from clack.widgets.stats import StatsTab


class ClackApp(App):
    CSS_PATH = "css/app.tcss"
    TITLE = "clack"
    BINDINGS = [
        Binding("1", "show_tab('dashboard')", "Dashboard", show=True),
        Binding("2", "show_tab('stats')", "Stats", show=True),
        Binding("3", "show_tab('query')", "Query", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("t", "switch_theme", "Theme", show=True),
    ]

    THEMES = ("solarized-dark", "solarized-light")

    db: duckdb.DuckDBPyConnection | None = None

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
            self.query_one(DashboardTab).load_data(self.db)
            self.query_one(StatsTab).load_data(self.db)
            self.query_one(QueryConsole).set_db(self.db)

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
