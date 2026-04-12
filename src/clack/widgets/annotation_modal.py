"""AnnotationModal — Textual ModalScreen for labelling a single tool call."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from clack.annotations import ANNOTATION_TYPES, Annotation
from clack.models import ToolCall


def _tool_call_summary(tc: ToolCall) -> str:
    """One-line human-readable summary of the tool call for the modal header."""
    inp = tc.tool_input
    if tc.tool_name == "Grep":
        pattern = inp.get("pattern", "?")
        path = inp.get("path", inp.get("glob", ""))
        return f"[Grep] pattern={pattern!r}" + (f"  path={path!r}" if path else "")
    if tc.tool_name == "Glob":
        return f"[Glob] pattern={inp.get('pattern', '?')!r}"
    if tc.tool_name in ("Read", "Edit", "Write"):
        return f"[{tc.tool_name}] {inp.get('file_path', '?')}"
    if tc.tool_name == "Bash":
        cmd = inp.get("command", "?")[:80]
        return f"[Bash] {cmd}"
    if tc.tool_name == "Agent":
        desc = inp.get("description", inp.get("prompt", "?"))[:60]
        return f"[Agent] {desc}"
    return f"[{tc.tool_name}] {str(inp)[:80]}"


class AnnotationModal(ModalScreen[Annotation | None]):
    """Modal overlay for annotating a tool call.

    Dismisses with an ``Annotation`` on save, or ``None`` on cancel.
    """

    DEFAULT_CSS = """
    AnnotationModal {
        align: center middle;
    }

    #modal-container {
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        width: 72;
        height: auto;
    }

    #modal-title {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    #error-badge {
        color: $error;
        text-style: bold;
        margin-bottom: 1;
    }

    AnnotationModal Label {
        margin-top: 1;
        color: $text-muted;
    }

    AnnotationModal Select {
        margin-top: 0;
    }

    AnnotationModal Input {
        margin-top: 0;
    }

    #modal-buttons {
        margin-top: 1;
        align: right middle;
        height: auto;
    }

    #modal-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, session_id: str, tool_call: ToolCall) -> None:
        super().__init__()
        self._session_id = session_id
        self._tool_call = tool_call

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        tc = self._tool_call
        with Vertical(id="modal-container"):
            yield Static(_tool_call_summary(tc), id="modal-title")
            if tc.is_error:
                yield Static("This tool call returned an error", id="error-badge")

            yield Label("Annotation type")
            yield Select(
                options=[(label, value) for value, label in ANNOTATION_TYPES],
                value="wrong_path",
                id="annotation-type",
            )

            yield Label("Note  —  what went wrong / what should have happened")
            yield Input(
                placeholder="e.g. Should have searched src/ not project root",
                id="annotation-note",
            )

            yield Label("Skill path  (optional)  —  skill whose description needs fixing")
            yield Input(
                placeholder="e.g. .claude/skills/search.md",
                id="skill-path",
            )

            with Horizontal(id="modal-buttons"):
                yield Button("Save  [Ctrl+S]", variant="primary", id="btn-save")
                yield Button("Cancel  [Esc]", id="btn-cancel")

    def on_mount(self) -> None:
        # Focus the note field immediately so the user can type right away
        self.query_one("#annotation-note", Input).focus()

    # ------------------------------------------------------------------
    # Actions / events
    # ------------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self._submit()
        else:
            self.dismiss(None)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _submit(self) -> None:
        type_select = self.query_one("#annotation-type", Select)
        note_input = self.query_one("#annotation-note", Input)
        skill_input = self.query_one("#skill-path", Input)

        annotation_type = (
            str(type_select.value)
            if type_select.value is not Select.BLANK
            else "other"
        )
        skill_path = skill_input.value.strip() or None

        annotation = Annotation(
            session_id=self._session_id,
            tool_use_id=self._tool_call.tool_use_id or "",
            tool_name=self._tool_call.tool_name,
            annotation_type=annotation_type,
            note=note_input.value.strip(),
            skill_path=skill_path,
        )
        self.dismiss(annotation)
