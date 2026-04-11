"""DuckDB data layer for clack.

Reads Claude Code session JSONL files directly via read_json_auto.
All queries run against an in-memory temp table built on startup.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from clack.models import (
    DayStats,
    DialogTurn,
    ModelStats,
    SessionSummary,
    ToolCall,
)

SESSIONS_DIR = Path.home() / ".claude/projects"
SESSIONS_GLOB = str(SESSIONS_DIR / "*/*.jsonl")

# Tracks file mtimes for incremental refresh
_file_mtimes: dict[str, float] = {}


def get_connection() -> duckdb.DuckDBPyConnection:
    """Create a DuckDB connection and load session data."""
    con = duckdb.connect(":memory:")
    _load_raw_records(con, SESSIONS_GLOB)
    _snapshot_mtimes()
    _create_views(con)
    return con


def refresh(con: duckdb.DuckDBPyConnection) -> None:
    """Incrementally reload only changed session files."""
    changed = _get_changed_files()
    if not changed:
        return

    # Reload changed files one at a time to isolate bad files
    for filepath in changed:
        con.execute("DELETE FROM raw_records WHERE filename = ?", [filepath])
        try:
            _load_raw_records(con, [filepath])
        except Exception:
            pass  # skip malformed files
    _snapshot_mtimes()


def _snapshot_mtimes() -> None:
    """Record current mtimes of all session JSONL files."""
    global _file_mtimes
    _file_mtimes = {}
    for p in SESSIONS_DIR.glob("*/*.jsonl"):
        try:
            _file_mtimes[str(p)] = p.stat().st_mtime
        except OSError:
            pass


def _get_changed_files() -> list[str]:
    """Return paths of files that are new or modified since last snapshot."""
    changed = []
    for p in SESSIONS_DIR.glob("*/*.jsonl"):
        path_str = str(p)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if path_str not in _file_mtimes or mtime > _file_mtimes[path_str]:
            changed.append(path_str)
    return changed


def _load_raw_records(
    con: duckdb.DuckDBPyConnection, source: str | list[str],
) -> None:
    if isinstance(source, list):
        if not source:
            return
        file_list = ", ".join(f"'{f}'" for f in source)
        glob_expr = f"[{file_list}]"
    else:
        glob_expr = f"'{source}'"

    select_sql = f"""
        SELECT *,
            regexp_extract(filename, '.*/([^/]+)/[^/]+\\.jsonl$', 1) AS project_slug,
            regexp_extract(filename, '.*/([^/]+)\\.jsonl$', 1) AS file_session_id
        FROM read_json_auto(
            {glob_expr},
            format='newline_delimited',
            union_by_name=true,
            maximum_object_size=10485760,
            filename=true,
            ignore_errors=true
        )
    """

    # First call creates the table; subsequent calls insert into it
    try:
        con.execute("SELECT 1 FROM raw_records LIMIT 0")
        # Use INSERT BY NAME so columns are matched, missing ones get NULL
        con.execute(f"INSERT INTO raw_records BY NAME {select_sql}")
    except duckdb.CatalogException:
        con.execute(f"CREATE TEMP TABLE raw_records AS {select_sql}")


def _create_views(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE VIEW v_sessions AS
        WITH first_user_msg AS (
            SELECT
                sessionId,
                cwd,
                gitBranch,
                version,
                message.content::VARCHAR AS first_message,
                timestamp,
                ROW_NUMBER() OVER (PARTITION BY sessionId ORDER BY timestamp) AS rn
            FROM raw_records
            WHERE type = 'user'
              AND json_type(message.content) = 'VARCHAR'
        ),
        custom_titles AS (
            SELECT sessionId, customTitle
            FROM raw_records
            WHERE type = 'custom-title'
        ),
        slugs AS (
            SELECT sessionId, slug
            FROM raw_records
            WHERE slug IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY sessionId ORDER BY timestamp) = 1
        ),
        models_used AS (
            SELECT
                sessionId,
                message.model AS model,
                COUNT(DISTINCT message.id) AS turn_count
            FROM raw_records
            WHERE type = 'assistant'
              AND message.model IS NOT NULL
              AND message.model != '<synthetic>'
            GROUP BY sessionId, message.model
        ),
        session_times AS (
            SELECT
                sessionId,
                MIN(timestamp) AS started_at,
                MAX(timestamp) AS last_active
            FROM raw_records
            WHERE type IN ('user', 'assistant')
            GROUP BY sessionId
        )
        SELECT
            st.sessionId,
            st.started_at,
            st.last_active,
            fu.cwd,
            fu.gitBranch,
            fu.version,
            COALESCE(ct.customTitle, s.slug) AS title,
            CASE
                WHEN fu.first_message IS NOT NULL
                    THEN LEFT(TRIM('"' FROM fu.first_message), 120)
                ELSE '[no prompt]'
            END AS summary,
            m.model AS primary_model,
            COALESCE(m.turn_count, 0) AS turn_count
        FROM session_times st
        LEFT JOIN first_user_msg fu ON fu.sessionId = st.sessionId AND fu.rn = 1
        LEFT JOIN custom_titles ct ON ct.sessionId = st.sessionId
        LEFT JOIN slugs s ON s.sessionId = st.sessionId
        LEFT JOIN (
            SELECT sessionId, model, turn_count,
                   ROW_NUMBER() OVER (PARTITION BY sessionId ORDER BY turn_count DESC) AS rn
            FROM models_used
        ) m ON m.sessionId = st.sessionId AND m.rn = 1
        ORDER BY st.last_active DESC
    """)

    con.execute("""
        CREATE VIEW v_assistant_turns AS
        WITH ranked AS (
            SELECT
                sessionId,
                message.id AS msg_id,
                message.model AS model,
                message.usage.input_tokens AS input_tokens,
                message.usage.output_tokens AS output_tokens,
                message.usage.cache_creation_input_tokens AS cache_creation_tokens,
                message.usage.cache_read_input_tokens AS cache_read_tokens,
                timestamp,
                ROW_NUMBER() OVER (
                    PARTITION BY sessionId, message.id
                    ORDER BY message.usage.output_tokens DESC NULLS LAST
                ) AS rn
            FROM raw_records
            WHERE type = 'assistant'
              AND message.model IS NOT NULL
              AND message.model != '<synthetic>'
        )
        SELECT * EXCLUDE(rn) FROM ranked WHERE rn = 1
    """)

    con.execute("""
        CREATE VIEW v_stats AS
        SELECT
            model,
            COUNT(DISTINCT sessionId) AS session_count,
            COUNT(*) AS turn_count,
            SUM(input_tokens) AS total_input_tokens,
            SUM(output_tokens) AS total_output_tokens,
            SUM(cache_creation_tokens) AS total_cache_creation,
            SUM(cache_read_tokens) AS total_cache_read
        FROM v_assistant_turns
        GROUP BY model
        ORDER BY total_output_tokens DESC
    """)

    con.execute("""
        CREATE VIEW v_sessions_by_day AS
        SELECT
            timestamp::DATE AS day,
            COUNT(DISTINCT sessionId) AS sessions,
            COUNT(DISTINCT msg_id) AS turns,
            SUM(output_tokens) AS output_tokens
        FROM v_assistant_turns
        GROUP BY 1
        ORDER BY 1
    """)


# --- Query functions ---


def get_sessions(con: duckdb.DuckDBPyConnection) -> list[SessionSummary]:
    rows = con.execute("SELECT * FROM v_sessions").fetchall()
    return [
        SessionSummary(
            session_id=str(r[0]),
            started_at=str(r[1]),
            last_active=str(r[2]),
            cwd=r[3],
            git_branch=r[4],
            version=r[5],
            title=r[6],
            summary=r[7],
            primary_model=r[8],
            turn_count=r[9],
        )
        for r in rows
    ]


def get_model_stats(con: duckdb.DuckDBPyConnection) -> list[ModelStats]:
    rows = con.execute("SELECT * FROM v_stats").fetchall()
    return [
        ModelStats(
            model=r[0],
            session_count=r[1],
            turn_count=r[2],
            total_input_tokens=r[3] or 0,
            total_output_tokens=r[4] or 0,
            total_cache_creation=r[5] or 0,
            total_cache_read=r[6] or 0,
        )
        for r in rows
    ]


def get_daily_stats(con: duckdb.DuckDBPyConnection) -> list[DayStats]:
    rows = con.execute("SELECT * FROM v_sessions_by_day").fetchall()
    return [
        DayStats(
            day=str(r[0]),
            sessions=r[1],
            turns=r[2],
            output_tokens=r[3] or 0,
        )
        for r in rows
    ]


def get_session_dialog(
    con: duckdb.DuckDBPyConnection, session_id: str
) -> list[DialogTurn]:
    """Reconstruct the conversation for a single session."""
    rows = con.execute(
        """
        SELECT
            type,
            uuid,
            parentUuid,
            timestamp,
            message,
            toolUseResult,
            durationMs,
            subtype,
            sourceToolAssistantUUID
        FROM raw_records
        WHERE sessionId = $1::UUID
          AND type IN ('user', 'assistant', 'system')
          AND isSidechain = false
        ORDER BY timestamp, uuid
        """,
        [session_id],
    ).fetchall()

    return _build_dialog_turns(rows)


def _build_dialog_turns(rows: list) -> list[DialogTurn]:
    """Parse raw records into a list of DialogTurn objects."""
    # Collect assistant chunks grouped by message.id
    assistant_chunks: dict[str, list] = {}
    # Collect tool results keyed by sourceToolAssistantUUID
    tool_results: dict[str, list] = {}
    # Collect turn durations
    turn_durations: list[tuple[str, int]] = []

    for row in rows:
        (rec_type, uuid, parent_uuid, ts, message,
         tool_use_result, duration_ms, subtype, source_tool_uuid) = row
        msg = _parse_msg(message)

        if rec_type == "user":
            content = _get_content(msg)
            if source_tool_uuid is not None:
                result_text, is_error = _extract_tool_result(content, tool_use_result)
                tool_results.setdefault(str(source_tool_uuid), []).append(
                    {"text": result_text, "is_error": is_error}
                )

        elif rec_type == "assistant":
            msg_id = msg.get("id", "") if msg else ""
            if msg_id:
                assistant_chunks.setdefault(msg_id, []).append(
                    {"timestamp": str(ts), "msg": msg, "uuid": str(uuid)}
                )

        elif rec_type == "system" and subtype == "turn_duration" and duration_ms:
            turn_durations.append((str(parent_uuid), int(duration_ms)))

    # Build ordered dialog turns
    duration_map = dict(turn_durations)
    turns: list[DialogTurn] = []
    seen_msg_ids: set[str] = set()

    for row in rows:
        (rec_type, uuid, parent_uuid, ts, message,
         tool_use_result, duration_ms, subtype, source_tool_uuid) = row
        msg = _parse_msg(message)

        if rec_type == "user" and source_tool_uuid is None:
            text = _extract_user_text(msg)
            if text:
                turns.append(DialogTurn(role="user", timestamp=str(ts), content=text))

        elif rec_type == "assistant":
            msg_id = msg.get("id", "") if msg else ""
            if not msg_id or msg_id in seen_msg_ids:
                continue
            seen_msg_ids.add(msg_id)

            chunks = assistant_chunks.get(msg_id, [])
            turn = _build_assistant_turn(chunks, tool_results, duration_map, str(ts))
            turns.append(turn)

    return turns


def _parse_msg(message) -> dict:
    """Parse message from DuckDB — it's a struct/dict with JSON content field."""
    if message is None:
        return {}
    if isinstance(message, dict):
        return message
    return _parse_json_field(message) or {}


def _get_content(msg: dict):
    """Get parsed content from a message dict."""
    content = msg.get("content", "")
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            return parsed
        except (json.JSONDecodeError, TypeError):
            return content
    return content


def _extract_user_text(msg: dict) -> str:
    """Extract human-readable text from a user message."""
    content = _get_content(msg)
    if isinstance(content, str):
        return content.strip().strip('"')
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content) if content else ""


def _extract_tool_result(content, tool_use_result) -> tuple[str, bool]:
    """Extract tool result text and error status."""
    result_text = ""
    is_error = False

    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                result_text = str(block.get("content", ""))
                is_error = block.get("is_error", False)
    elif isinstance(content, str):
        result_text = content

    tur = _parse_json_field(tool_use_result)
    if isinstance(tur, dict):
        for key_name in ("stdout", "content", "filenames"):
            if key_name in tur:
                val = tur[key_name]
                if isinstance(val, str):
                    result_text = val[:2000]
                elif isinstance(val, list):
                    result_text = "\n".join(str(v) for v in val[:50])
                break
        if tur.get("stderr"):
            result_text += f"\nSTDERR: {tur['stderr']}"
    elif isinstance(tur, str):
        result_text = tur[:2000]

    return result_text, is_error


def _build_assistant_turn(
    chunks: list[dict],
    tool_results: dict[str, list],
    duration_map: dict[str, int],
    fallback_ts: str,
) -> DialogTurn:
    """Build a single assistant DialogTurn from merged chunks."""
    text_parts = []
    tool_calls = []
    model = None
    input_tokens = None
    output_tokens = None

    for chunk in chunks:
        m = chunk["msg"]
        if m.get("model") and m["model"] != "<synthetic>":
            model = m["model"]
        usage = m.get("usage") or {}
        if isinstance(usage, dict) and usage.get("output_tokens"):
            ot = usage["output_tokens"]
            if output_tokens is None or ot > output_tokens:
                output_tokens = ot
                input_tokens = usage.get("input_tokens")

        content = _get_content(m)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    text_parts.append(text)
            elif block.get("type") == "tool_use":
                tc = _build_tool_call(block, chunk["uuid"], tool_results)
                tool_calls.append(tc)

    last_uuid = chunks[-1]["uuid"] if chunks else None
    dur = duration_map.get(last_uuid) if last_uuid else None

    return DialogTurn(
        role="assistant",
        timestamp=chunks[0]["timestamp"] if chunks else fallback_ts,
        content="\n\n".join(text_parts),
        tool_calls=tool_calls,
        duration_ms=dur,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _build_tool_call(
    block: dict, chunk_uuid: str, tool_results: dict[str, list]
) -> ToolCall:
    """Build a ToolCall from a tool_use content block."""
    tool_input = block.get("input", {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
            tool_input = {"raw": tool_input}

    result_text = None
    is_error = False
    results = tool_results.get(chunk_uuid, [])
    if results:
        r = results.pop(0)
        result_text = r["text"]
        is_error = r["is_error"]

    return ToolCall(
        tool_name=block.get("name", "?"),
        tool_input=tool_input if isinstance(tool_input, dict) else {},
        tool_result=result_text,
        is_error=is_error,
        tool_use_id=block.get("id", ""),
    )


def _parse_json_field(val):
    """Parse a JSON field that may be a string, dict, or DuckDB struct."""
    if val is None:
        return None
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val
