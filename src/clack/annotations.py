"""Annotation store for labeling tool calls in Claude Code sessions.

Annotations are stored in ~/.clack/annotations.db (SQLite) so the original
JSONL session files remain untouched.  Each annotation targets a specific
tool call via its tool_use_id and optionally links to the skill file whose
description should be improved.

Typical workflow
----------------
1. Browse a session in the Dialog Viewer.
2. Navigate to a bad tool call (e.g. Grep on the wrong path) and press [a].
3. Fill in the annotation type, a note, and optionally the skill path.
4. Call export_dspy_examples() to get training dicts for DSPy optimisation.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

ANNOTATION_DB_PATH = Path.home() / ".clack" / "annotations.db"

# (stored_value, human label) — order determines the Select widget display
ANNOTATION_TYPES: list[tuple[str, str]] = [
    ("wrong_path",    "Wrong path    — searched / read / wrote incorrect location"),
    ("wrong_pattern", "Wrong pattern — incorrect search pattern or regex"),
    ("wrong_tool",    "Wrong tool    — should have used a different tool"),
    ("wrong_scope",   "Wrong scope   — too broad or too narrow"),
    ("unnecessary",   "Unnecessary   — this step wasn't needed"),
    ("correct",       "Correct       — explicitly mark as good"),
    ("other",         "Other         — see note"),
]

ANNOTATION_TYPE_VALUES = [v for v, _ in ANNOTATION_TYPES]


@dataclass
class Annotation:
    session_id: str
    tool_use_id: str
    tool_name: str
    annotation_type: str
    note: str
    skill_path: str | None = None
    created_at: str | None = None
    id: int | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _open() -> sqlite3.Connection:
    ANNOTATION_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ANNOTATION_DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS annotations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT    NOT NULL,
            tool_use_id     TEXT    NOT NULL,
            tool_name       TEXT    NOT NULL,
            annotation_type TEXT    NOT NULL,
            note            TEXT    NOT NULL DEFAULT '',
            skill_path      TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    con.commit()
    return con


def _row_to_annotation(r: sqlite3.Row) -> Annotation:
    return Annotation(
        id=r["id"],
        session_id=r["session_id"],
        tool_use_id=r["tool_use_id"],
        tool_name=r["tool_name"],
        annotation_type=r["annotation_type"],
        note=r["note"],
        skill_path=r["skill_path"],
        created_at=r["created_at"],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_annotation(a: Annotation) -> None:
    """Persist an annotation.  Overwrites any existing annotation for the
    same (session_id, tool_use_id) pair."""
    con = _open()
    con.execute(
        """INSERT INTO annotations
               (session_id, tool_use_id, tool_name, annotation_type, note, skill_path)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT DO NOTHING""",
        (a.session_id, a.tool_use_id, a.tool_name, a.annotation_type, a.note, a.skill_path),
    )
    con.commit()
    con.close()


def get_annotations_for_session(session_id: str) -> dict[str, Annotation]:
    """Return annotations for a session keyed by tool_use_id."""
    con = _open()
    rows = con.execute(
        "SELECT * FROM annotations WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    con.close()
    return {r["tool_use_id"]: _row_to_annotation(r) for r in rows}


def get_all_annotations() -> list[Annotation]:
    con = _open()
    rows = con.execute(
        "SELECT * FROM annotations ORDER BY created_at DESC"
    ).fetchall()
    con.close()
    return [_row_to_annotation(r) for r in rows]


def export_dspy_examples(skill_path: str | None = None) -> list[dict]:
    """Export annotations as DSPy-compatible training example dicts.

    Each dict contains everything needed to construct a ``dspy.Example``::

        import dspy
        for ex in export_dspy_examples(".claude/skills/search.md"):
            example = dspy.Example(**ex).with_inputs("tool_name", "tool_input")

    Fields
    ------
    session_id, tool_use_id, tool_name, tool_input_json
        Identify the exact tool call.
    annotation_type, note, skill_path
        Human label and free-text correction.
    is_bad
        True for every type except "correct" — use as the DSPy metric target.
    correction
        Alias of ``note``; convenience field for DSPy signature outputs.
    """
    con = _open()
    if skill_path:
        rows = con.execute(
            "SELECT * FROM annotations WHERE skill_path = ? ORDER BY created_at",
            (skill_path,),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM annotations ORDER BY skill_path, created_at"
        ).fetchall()
    con.close()

    return [
        {
            "session_id":      r["session_id"],
            "tool_use_id":     r["tool_use_id"],
            "tool_name":       r["tool_name"],
            "annotation_type": r["annotation_type"],
            "note":            r["note"],
            "skill_path":      r["skill_path"],
            "created_at":      r["created_at"],
            # DSPy-facing fields
            "is_bad":          r["annotation_type"] != "correct",
            "correction":      r["note"],
        }
        for r in rows
    ]


def export_dspy_examples_json(skill_path: str | None = None) -> str:
    """Return export_dspy_examples() serialised as a JSON string."""
    return json.dumps(export_dspy_examples(skill_path), indent=2)
