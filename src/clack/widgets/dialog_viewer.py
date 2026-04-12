"""Dialog Viewer widget — conversation explorer with expandable tool calls."""

from __future__ import annotations

import duckdb
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Static, Tree
from textual.widgets._tree import TreeNode

from clack.annotations import (
    Annotation,
    get_annotations_for_session,
    save_annotation,
)
from clack.models import DialogTurn, ToolCall


class DialogViewer(Widget):
    BINDINGS = [
        Binding("a", "annotate", "Annotate tool call"),
        Binding("h", "export_html", "HTML Export"),
        Binding("escape", "go_back", "Back"),
    ]

    _db: duckdb.DuckDBPyConnection | None = None
    _session_id: str | None = None
    _session_title: str = ""
    _dialog: list[DialogTurn] = []

    # tool_use_id → TreeNode, built during _render_dialog so we can update
    # node labels after an annotation is saved without re-rendering everything.
    _tool_nodes: dict[str, TreeNode] = {}

    # tool_use_id → Annotation, loaded fresh each time a session is opened.
    _annotations: dict[str, Annotation] = {}

    def compose(self) -> ComposeResult:
        yield Static(
            "No session selected -- press [v] on a session in Dashboard",
            id="dialog-header",
        )
        yield Tree("Dialog", id="dialog-tree")
        yield Static(
            "[a] Annotate  [h] HTML Export  [Esc] Back to Dashboard",
            id="dialog-footer",
        )

    def on_mount(self) -> None:
        tree = self.query_one("#dialog-tree", Tree)
        tree.show_root = False

    def load_session(
        self, db: duckdb.DuckDBPyConnection, session_id: str, title: str
    ) -> None:
        self._db = db
        self._session_id = session_id
        self._session_title = title
        self._tool_nodes = {}
        self._annotations = {}
        self.query_one("#dialog-header", Static).update(
            f"Loading: {title}..."
        )
        self._fetch_dialog()

    # ------------------------------------------------------------------
    # Data loading (worker thread)
    # ------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="dialog")
    def _fetch_dialog(self) -> None:
        from clack.db import get_session_dialog

        assert self._db is not None and self._session_id is not None
        self._dialog = get_session_dialog(self._db, self._session_id)
        annotations = get_annotations_for_session(self._session_id)
        self.app.call_from_thread(self._render_dialog, annotations)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_dialog(self, annotations: dict[str, Annotation]) -> None:
        self._annotations = annotations
        self._tool_nodes = {}

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
                tree.root.add_leaf(label)

            elif turn.role == "assistant":
                model_tag = f"  ({turn.model.replace('claude-', '')})" if turn.model else ""
                tokens_tag = ""
                if turn.output_tokens:
                    tokens_tag = f"  [{turn.input_tokens or 0}in/{turn.output_tokens}out]"
                dur_tag = ""
                if turn.duration_ms:
                    dur_tag = f"  {turn.duration_ms / 1000:.1f}s"

                preview = turn.content[:100].replace("\n", " ") if turn.content else "(tool calls only)"
                label = (
                    f"[bold green]ASSISTANT[/]{model_tag}{tokens_tag}{dur_tag}  "
                    f"{preview}"
                )

                if turn.tool_calls:
                    node = tree.root.add(label)
                    if turn.content:
                        node.add_leaf(f"[dim]Response:[/] {turn.content[:500]}")
                    for tc in turn.tool_calls:
                        tc_label = _format_tool_call_label(tc, annotations)
                        if tc.tool_result:
                            tc_node = node.add(tc_label, data=tc)
                            result_preview = tc.tool_result[:500].replace("\n", "\n    ")
                            tc_node.add_leaf(f"[dim]{result_preview}[/]")
                        else:
                            tc_node = node.add_leaf(tc_label, data=tc)
                        # Track by tool_use_id for later label updates
                        if tc.tool_use_id:
                            self._tool_nodes[tc.tool_use_id] = tc_node
                else:
                    tree.root.add_leaf(label)

    # ------------------------------------------------------------------
    # Annotation action
    # ------------------------------------------------------------------

    def action_annotate(self) -> None:
        if not self._session_id:
            return
        tree = self.query_one("#dialog-tree", Tree)
        node = tree.cursor_node
        if node is None or not isinstance(node.data, ToolCall):
            self.query_one("#dialog-footer", Static).update(
                "[yellow]Navigate to a tool call node first, then press [a][/]  "
                "[h] HTML Export  [Esc] Back"
            )
            return
        tc: ToolCall = node.data
        if not tc.tool_use_id:
            self.query_one("#dialog-footer", Static).update(
                "[yellow]This tool call has no ID and cannot be annotated[/]  "
                "[Esc] Back"
            )
            return

        from clack.widgets.annotation_modal import AnnotationModal

        def _on_annotation(annotation: Annotation | None) -> None:
            if annotation is None:
                return
            save_annotation(annotation)
            self._annotations[annotation.tool_use_id] = annotation
            # Update just this node's label — no full re-render needed
            if annotation.tool_use_id in self._tool_nodes:
                self._tool_nodes[annotation.tool_use_id].label = (
                    _format_tool_call_label(tc, self._annotations)
                )
            self.query_one("#dialog-footer", Static).update(
                f"[green]Annotation saved[/]  ({annotation.annotation_type})  "
                "[a] Annotate  [h] HTML Export  [Esc] Back"
            )

        self.app.push_screen(AnnotationModal(self._session_id, tc), _on_annotation)

    # ------------------------------------------------------------------
    # Other actions
    # ------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Label formatting
# ---------------------------------------------------------------------------

_ANNOTATION_MARKERS = {
    "correct":       "[bold green]✓[/] ",
    "wrong_path":    "[bold red]✗[/] ",
    "wrong_pattern": "[bold red]✗[/] ",
    "wrong_tool":    "[bold red]✗[/] ",
    "wrong_scope":   "[bold red]✗[/] ",
    "unnecessary":   "[bold red]✗[/] ",
    "other":         "[bold red]✗[/] ",
}


def _annotation_marker(tc: ToolCall, annotations: dict[str, Annotation]) -> str:
    if not tc.tool_use_id:
        return ""
    ann = annotations.get(tc.tool_use_id)
    if ann is None:
        return ""
    return _ANNOTATION_MARKERS.get(ann.annotation_type, "[bold red]✗[/] ")


def _format_tool_call_label(tc: ToolCall, annotations: dict[str, Annotation] | None = None) -> str:
    """Format a tool call into a concise tree label, with annotation marker."""
    if annotations is None:
        annotations = {}
    marker = _annotation_marker(tc, annotations)
    error_marker = " [bold red]ERROR[/]" if tc.is_error else ""
    inp = tc.tool_input

    if tc.tool_name == "Bash":
        cmd = inp.get("command", "")[:60]
        body = f"[yellow][Bash][/] {cmd}"
    elif tc.tool_name == "Read":
        body = f"[yellow][Read][/] {inp.get('file_path', '')}"
    elif tc.tool_name == "Edit":
        body = f"[yellow][Edit][/] {inp.get('file_path', '')}"
    elif tc.tool_name == "Write":
        body = f"[yellow][Write][/] {inp.get('file_path', '')}"
    elif tc.tool_name == "Grep":
        pattern = inp.get("pattern", "")
        path = inp.get("path", inp.get("glob", ""))
        body = f"[yellow][Grep][/] {pattern!r}" + (f"  {path}" if path else "")
    elif tc.tool_name == "Glob":
        body = f"[yellow][Glob][/] {inp.get('pattern', '')}"
    elif tc.tool_name == "Agent":
        desc = inp.get("description", inp.get("prompt", ""))[:50]
        body = f"[yellow][Agent][/] {desc}"
    else:
        body = f"[yellow][{tc.tool_name}][/] {str(inp)[:50]}"

    return f"{marker}{body}{error_marker}"
