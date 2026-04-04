"""Dialog Viewer widget — conversation explorer with expandable tool calls."""

from __future__ import annotations

import duckdb
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Static, Tree

from clack.models import DialogTurn, ToolCall


class DialogViewer(Widget):
    BINDINGS = [
        Binding("h", "export_html", "HTML Export"),
        Binding("escape", "go_back", "Back"),
    ]

    _db: duckdb.DuckDBPyConnection | None = None
    _session_id: str | None = None
    _session_title: str = ""
    _dialog: list[DialogTurn] = []

    def compose(self) -> ComposeResult:
        yield Static(
            "No session selected -- press [v] on a session in Dashboard",
            id="dialog-header",
        )
        yield Tree("Dialog", id="dialog-tree")
        yield Static("[h] HTML Export  [Esc] Back to Dashboard", id="dialog-footer")

    def on_mount(self) -> None:
        tree = self.query_one("#dialog-tree", Tree)
        tree.show_root = False

    def load_session(
        self, db: duckdb.DuckDBPyConnection, session_id: str, title: str
    ) -> None:
        self._db = db
        self._session_id = session_id
        self._session_title = title
        self.query_one("#dialog-header", Static).update(
            f"Loading: {title}..."
        )
        self._fetch_dialog()

    @work(thread=True, exclusive=True, group="dialog")
    def _fetch_dialog(self) -> None:
        from clack.db import get_session_dialog

        assert self._db is not None and self._session_id is not None
        self._dialog = get_session_dialog(self._db, self._session_id)
        self.app.call_from_thread(self._render_dialog)

    def _render_dialog(self) -> None:
        header = self.query_one("#dialog-header", Static)
        header.update(
            f"Session: {self._session_title}  |  "
            f"{len(self._dialog)} turns  |  "
            f"id: {self._session_id[:8] if self._session_id else '?'}"
        )

        tree = self.query_one("#dialog-tree", Tree)
        tree.clear()

        for turn in self._dialog:
            if turn.role == "user":
                label = f"[bold cyan]USER[/]  {turn.content[:120]}"
                node = tree.root.add_leaf(label)

            elif turn.role == "assistant":
                model_tag = f"  ({turn.model.replace('claude-', '')})" if turn.model else ""
                tokens_tag = ""
                if turn.output_tokens:
                    tokens_tag = f"  [{turn.input_tokens or 0}in/{turn.output_tokens}out]"
                dur_tag = ""
                if turn.duration_ms:
                    dur_tag = f"  {turn.duration_ms / 1000:.1f}s"

                if turn.content:
                    preview = turn.content[:100].replace("\n", " ")
                else:
                    preview = "(tool calls only)"

                label = (
                    f"[bold green]ASSISTANT[/]{model_tag}{tokens_tag}{dur_tag}  "
                    f"{preview}"
                )

                if turn.tool_calls:
                    node = tree.root.add(label)
                    # Add full text as first child if there's content
                    if turn.content:
                        node.add_leaf(
                            f"[dim]Response:[/] {turn.content[:500]}"
                        )
                    # Add tool calls
                    for tc in turn.tool_calls:
                        tc_label = _format_tool_call_label(tc)
                        if tc.tool_result:
                            tc_node = node.add(tc_label)
                            # Truncate result for display
                            result_preview = tc.tool_result[:500].replace("\n", "\n    ")
                            tc_node.add_leaf(f"[dim]{result_preview}[/]")
                        else:
                            node.add_leaf(tc_label)
                else:
                    tree.root.add_leaf(label)

    def action_export_html(self) -> None:
        if not self._dialog or not self._session_id:
            return
        from clack.html_export import export_dialog_html

        assert self._session_id is not None
        path = export_dialog_html(self._session_id, self._session_title, self._dialog)
        self.query_one("#dialog-footer", Static).update(
            f"Exported to {path} — opening in browser...  [Esc] Back"
        )

    def action_go_back(self) -> None:
        self.app.action_show_tab("dashboard")  # type: ignore[attr-defined]


def _format_tool_call_label(tc: ToolCall) -> str:
    """Format a tool call into a concise label."""
    error_marker = " [bold red]ERROR[/]" if tc.is_error else ""
    inp = tc.tool_input

    if tc.tool_name == "Bash":
        cmd = inp.get("command", "")[:60]
        return f"[yellow][Bash][/] {cmd}{error_marker}"
    elif tc.tool_name == "Read":
        path = inp.get("file_path", "")
        return f"[yellow][Read][/] {path}{error_marker}"
    elif tc.tool_name == "Edit":
        path = inp.get("file_path", "")
        return f"[yellow][Edit][/] {path}{error_marker}"
    elif tc.tool_name == "Write":
        path = inp.get("file_path", "")
        return f"[yellow][Write][/] {path}{error_marker}"
    elif tc.tool_name == "Grep":
        pattern = inp.get("pattern", "")
        return f"[yellow][Grep][/] {pattern}{error_marker}"
    elif tc.tool_name == "Glob":
        pattern = inp.get("pattern", "")
        return f"[yellow][Glob][/] {pattern}{error_marker}"
    elif tc.tool_name == "Agent":
        desc = inp.get("description", inp.get("prompt", ""))[:50]
        return f"[yellow][Agent][/] {desc}{error_marker}"
    else:
        detail = str(inp)[:50]
        return f"[yellow][{tc.tool_name}][/] {detail}{error_marker}"
