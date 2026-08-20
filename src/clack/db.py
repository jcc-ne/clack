"""DuckDB data layer for clack.

Reads Claude Code session JSONL files via read_json with an explicit schema.
All queries run against an in-memory temp table built on startup.
"""

from __future__ import annotations

import json
import time
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

# Sessions untouched for longer than this are not held in raw_records. Only
# their aggregates (summary row, token totals, search text) stay resident; the
# full transcript is re-read from disk if the user opens the dialog view.
ARCHIVE_AFTER_DAYS = 14

# Archived files are aggregated in batches so peak memory stays bounded by the
# batch rather than by the whole history.
_ARCHIVE_BATCH = 25

# Tracks file mtimes for incremental refresh
_file_mtimes: dict[str, float] = {}


def get_connection() -> duckdb.DuckDBPyConnection:
    """Create a DuckDB connection and load session data."""
    con = duckdb.connect(":memory:")
    recent, archived = _split_files()
    _load_raw_records(con, recent)
    _ensure_raw_records(con)
    _build_archive(con, archived)
    _snapshot_mtimes()
    _create_views(con)
    _create_fts_index(con)
    return con


def refresh(con: duckdb.DuckDBPyConnection) -> None:
    """Incrementally load records appended to changed session files.

    Session JSONL files are append-only, so a changed file is ingested from its
    last-seen timestamp onward instead of being deleted and re-read in full.
    That matters because DuckDB tombstones deleted rows rather than reclaiming
    them, so re-reading whole files on a timer grew the table without bound.
    """
    changed = _get_changed_files()
    if not changed:
        return

    _unarchive(con, changed)
    watermarks = _watermarks(con, changed)

    for filepath in changed:
        try:
            watermark = watermarks.get(filepath)
            # Records with no timestamp (e.g. custom-title) carry no ordering,
            # so they are replaced wholesale — only a handful exist per file.
            if watermark is not None:
                con.execute(
                    'DELETE FROM raw_records WHERE filename = ? '
                    'AND ("timestamp" IS NULL OR "timestamp" >= ?)',
                    [filepath, watermark],
                )
            _load_raw_records(con, [filepath], since=watermark)
        except Exception:
            pass  # skip malformed files
    _snapshot_mtimes()


def _watermarks(
    con: duckdb.DuckDBPyConnection, files: list[str]
) -> dict[str, str]:
    """Last-ingested timestamp per file, in a single pass over raw_records."""
    placeholders = ", ".join("?" for _ in files)
    rows = con.execute(
        f'SELECT filename, max("timestamp") FROM raw_records '
        f"WHERE filename IN ({placeholders}) GROUP BY filename",
        files,
    ).fetchall()
    return {r[0]: r[1] for r in rows if r[1] is not None}


def _split_files() -> tuple[list[str], list[str]]:
    """Partition session files into recently-active and archivable."""
    cutoff = time.time() - ARCHIVE_AFTER_DAYS * 86400
    recent: list[str] = []
    archived: list[str] = []
    for p in SESSIONS_DIR.glob("*/*.jsonl"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        (recent if mtime >= cutoff else archived).append(str(p))
    return recent, archived


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


_READ_JSON_SCHEMA = """{
    type: 'VARCHAR',
    sessionId: 'VARCHAR',
    "timestamp": 'VARCHAR',
    uuid: 'VARCHAR',
    parentUuid: 'VARCHAR',
    isSidechain: 'BOOLEAN',
    message: 'STRUCT(
        role VARCHAR,
        content JSON,
        model VARCHAR,
        id VARCHAR,
        type VARCHAR,
        stop_reason VARCHAR,
        stop_sequence VARCHAR,
        usage STRUCT(
            input_tokens BIGINT,
            cache_creation_input_tokens BIGINT,
            cache_read_input_tokens BIGINT,
            output_tokens BIGINT
        )
    )',
    cwd: 'VARCHAR',
    gitBranch: 'VARCHAR',
    version: 'VARCHAR',
    customTitle: 'VARCHAR',
    slug: 'VARCHAR',
    toolUseResult: 'JSON',
    durationMs: 'BIGINT',
    subtype: 'VARCHAR',
    sourceToolAssistantUUID: 'VARCHAR',
    userType: 'VARCHAR'
}"""


def _glob_expr(source: str | list[str]) -> str:
    if isinstance(source, list):
        file_list = ", ".join(f"'{f}'" for f in source)
        return f"[{file_list}]"
    return f"'{source}'"


def _records_select(source: str | list[str], since: str | None = None) -> str:
    """SELECT over read_json producing the raw_records column layout."""
    where = ""
    if since is not None:
        where = f"""WHERE "timestamp" IS NULL OR "timestamp" >= '{since}'"""
    return f"""
        SELECT
            type,
            sessionId::UUID AS sessionId,
            "timestamp",
            uuid::UUID AS uuid,
            parentUuid::UUID AS parentUuid,
            COALESCE(isSidechain, false) AS isSidechain,
            message,
            cwd,
            gitBranch,
            version,
            customTitle,
            slug,
            toolUseResult,
            durationMs,
            subtype,
            sourceToolAssistantUUID::UUID AS sourceToolAssistantUUID,
            userType,
            regexp_extract(filename, '.*/([^/]+)/[^/]+\\.jsonl$', 1) AS project_slug,
            regexp_extract(filename, '.*/([^/]+)\\.jsonl$', 1) AS file_session_id,
            filename
        FROM read_json(
            {_glob_expr(source)},
            format='newline_delimited',
            columns={_READ_JSON_SCHEMA},
            filename=true,
            ignore_errors=true
        )
        {where}
    """


def _load_raw_records(
    con: duckdb.DuckDBPyConnection,
    source: str | list[str],
    since: str | None = None,
) -> None:
    if isinstance(source, list) and not source:
        return
    select_sql = _records_select(source, since)

    # First call creates the table; subsequent calls insert into it
    try:
        con.execute("SELECT 1 FROM raw_records LIMIT 0")
        con.execute(f"INSERT INTO raw_records {select_sql}")
    except duckdb.CatalogException:
        con.execute(f"CREATE TEMP TABLE raw_records AS {select_sql}")


def _ensure_raw_records(con: duckdb.DuckDBPyConnection) -> None:
    """Create an empty raw_records if no recent files existed to define it."""
    try:
        con.execute("SELECT 1 FROM raw_records LIMIT 0")
    except duckdb.CatalogException:
        con.execute(
            f"CREATE TEMP TABLE raw_records AS "
            f"{_records_select(SESSIONS_GLOB)} LIMIT 0"
        )


# --- Derivations, expressed over a swappable source relation ---
#
# The same SQL runs twice: as a view over raw_records for recent sessions, and
# eagerly over a staging table to materialise aggregates for archived ones.


def _sessions_sql(src: str) -> str:
    return f"""
        WITH first_user_msg AS (
            SELECT
                sessionId,
                cwd,
                gitBranch,
                version,
                message.content::VARCHAR AS first_message,
                timestamp,
                ROW_NUMBER() OVER (PARTITION BY sessionId ORDER BY timestamp) AS rn
            FROM {src}
            WHERE type = 'user'
              AND json_type(message.content) = 'VARCHAR'
        ),
        custom_titles AS (
            -- One row per session: a session's title record is rewritten on
            -- every turn, so without collapsing these the joins below fan a
            -- single session out into one dashboard row per rewrite.
            SELECT sessionId, MAX(customTitle) AS customTitle
            FROM {src}
            WHERE type = 'custom-title'
            GROUP BY sessionId
        ),
        slugs AS (
            SELECT sessionId, slug
            FROM {src}
            WHERE slug IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY sessionId ORDER BY timestamp) = 1
        ),
        models_used AS (
            SELECT
                sessionId,
                message.model AS model,
                COUNT(DISTINCT message.id) AS turn_count
            FROM {src}
            WHERE type = 'assistant'
              AND message.model IS NOT NULL
              AND message.model != '<synthetic>'
            GROUP BY sessionId, message.model
        ),
        plans AS ({_plans_sql(src)}),
        session_times AS (
            SELECT
                sessionId,
                MIN(timestamp) AS started_at,
                MAX(timestamp) AS last_active
            FROM {src}
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
            COALESCE(m.turn_count, 0) AS turn_count,
            p.plan_path
        FROM session_times st
        LEFT JOIN first_user_msg fu ON fu.sessionId = st.sessionId AND fu.rn = 1
        LEFT JOIN custom_titles ct ON ct.sessionId = st.sessionId
        LEFT JOIN slugs s ON s.sessionId = st.sessionId
        LEFT JOIN plans p ON p.sessionId = st.sessionId
        LEFT JOIN (
            SELECT sessionId, model, turn_count,
                   ROW_NUMBER() OVER (PARTITION BY sessionId ORDER BY turn_count DESC) AS rn
            FROM models_used
        ) m ON m.sessionId = st.sessionId AND m.rn = 1
    """


def _turns_sql(src: str) -> str:
    return f"""
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
            FROM {src}
            WHERE type = 'assistant'
              AND message.model IS NOT NULL
              AND message.model != '<synthetic>'
        )
        SELECT * EXCLUDE(rn) FROM ranked WHERE rn = 1
    """


def _plans_sql(src: str) -> str:
    """Plan doc per session, from the ExitPlanMode tool call that wrote it.

    The call carries both the plan markdown and the path it was written to, so
    the body is searchable even when the file has since been deleted. A session
    can re-plan; the last call wins.
    """
    return f"""
        WITH blocks AS (
            SELECT
                sessionId,
                timestamp,
                unnest(json_extract(message.content, '$[*]')) AS b
            FROM {src}
            WHERE type = 'assistant'
              AND json_type(message.content) = 'ARRAY'
        )
        SELECT
            sessionId,
            json_extract_string(b, '$.input.planFilePath') AS plan_path,
            json_extract_string(b, '$.input.plan') AS plan_text
        FROM blocks
        WHERE json_extract_string(b, '$.name') = 'ExitPlanMode'
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY sessionId ORDER BY timestamp DESC
        ) = 1
    """


_FTS_MAX_TURNS = 30


def _session_text_sql(src: str, sessions_src: str) -> str:
    """Per-session searchable text: early user messages, summary fields, plan.

    Driven off the session ids present in `src` rather than off the user
    messages, so a session whose prompts are all content-block arrays — and so
    contributes no user text — still gets a row and stays findable by its plan
    or title.
    """
    return f"""
        WITH session_ids AS (
            SELECT DISTINCT sessionId::VARCHAR AS session_id
            FROM {src}
            WHERE sessionId IS NOT NULL
        ),
        plans AS ({_plans_sql(src)}),
        user_texts AS (
            SELECT
                sessionId::VARCHAR AS session_id,
                STRING_AGG(TRIM('"' FROM message.content::VARCHAR), ' ') AS user_messages
            FROM (
                SELECT
                    sessionId,
                    message,
                    timestamp,
                    ROW_NUMBER() OVER (
                        PARTITION BY sessionId
                        ORDER BY timestamp
                    ) AS turn_rn
                FROM {src}
                WHERE type = 'user'
                  AND json_type(message.content) = 'VARCHAR'
            ) ranked
            WHERE turn_rn <= {_FTS_MAX_TURNS}
            GROUP BY sessionId
        )
        SELECT
            si.session_id,
            CONCAT_WS(
                ' ',
                ut.user_messages, vs.title, vs.summary, vs.cwd, p.plan_text
            ) AS full_text
        FROM session_ids si
        LEFT JOIN user_texts ut ON ut.session_id = si.session_id
        LEFT JOIN plans p ON p.sessionId::VARCHAR = si.session_id
        LEFT JOIN {sessions_src} vs ON vs.sessionId::VARCHAR = si.session_id
    """


# --- Archive: aggregates for sessions whose transcripts are not held ---


def _build_archive(con: duckdb.DuckDBPyConnection, files: list[str]) -> None:
    """Aggregate old session files without keeping their records resident.

    Each batch is staged in a temp table, reduced to aggregates, then dropped.
    DuckDB does release a dropped table's blocks, so peak memory tracks the
    batch size rather than the full archive.
    """
    con.execute(
        f"CREATE TEMP TABLE archive_sessions AS "
        f"SELECT * FROM ({_sessions_sql('raw_records')}) LIMIT 0"
    )
    con.execute(
        f"CREATE TEMP TABLE archive_turns AS "
        f"SELECT * FROM ({_turns_sql('raw_records')}) LIMIT 0"
    )
    con.execute(
        "CREATE TEMP TABLE archive_text (session_id VARCHAR, full_text VARCHAR)"
    )
    con.execute(
        "CREATE TEMP TABLE archive_files (session_id VARCHAR, filename VARCHAR)"
    )

    for i in range(0, len(files), _ARCHIVE_BATCH):
        batch = files[i : i + _ARCHIVE_BATCH]
        try:
            con.execute(f"CREATE TEMP TABLE _stage AS {_records_select(batch)}")
            con.execute(
                f"INSERT INTO archive_sessions {_sessions_sql('_stage')}"
            )
            con.execute(f"INSERT INTO archive_turns {_turns_sql('_stage')}")
            con.execute(
                f"INSERT INTO archive_text "
                f"{_session_text_sql('_stage', 'archive_sessions')}"
            )
            con.execute("""
                INSERT INTO archive_files
                SELECT DISTINCT sessionId::VARCHAR, filename FROM _stage
                WHERE sessionId IS NOT NULL
            """)
        except Exception:
            pass  # skip malformed batch
        finally:
            con.execute("DROP TABLE IF EXISTS _stage")


def _unarchive(con: duckdb.DuckDBPyConnection, files: list[str]) -> None:
    """Drop archived aggregates for files that changed, so they can be reloaded.

    Reached when a session older than the cutoff is resumed: without this the
    session would appear both in the archive and in raw_records.
    """
    placeholders = ", ".join("?" for _ in files)
    ids = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT session_id FROM archive_files "
            f"WHERE filename IN ({placeholders})",
            files,
        ).fetchall()
    ]
    if not ids:
        return
    id_ph = ", ".join("?" for _ in ids)
    for table, col in (
        ("archive_sessions", "sessionId::VARCHAR"),
        ("archive_turns", "sessionId::VARCHAR"),
        ("archive_text", "session_id"),
        ("archive_files", "session_id"),
    ):
        con.execute(f"DELETE FROM {table} WHERE {col} IN ({id_ph})", ids)


def load_full_history(con: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """Load archived transcripts into raw_records so SQL sees all history.

    The dashboard, stats and search already cover archived sessions via their
    aggregates; this exists for the Query tab, where raw_records would
    otherwise silently omit older transcripts. Costs whatever the archived
    files weigh, and is undone only by restarting.

    Returns (sessions_loaded, records_added).
    """
    files = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT filename FROM archive_files"
        ).fetchall()
    ]
    if not files:
        return (0, 0)

    before = con.execute("SELECT count(*) FROM raw_records").fetchone()[0]
    sessions = con.execute("SELECT count(*) FROM archive_files").fetchone()[0]
    _unarchive(con, files)
    for i in range(0, len(files), _ARCHIVE_BATCH):
        try:
            _load_raw_records(con, files[i : i + _ARCHIVE_BATCH])
        except Exception:
            pass  # skip malformed batch
    after = con.execute("SELECT count(*) FROM raw_records").fetchone()[0]
    _create_fts_index(con)
    return (sessions, after - before)


def has_archived_sessions(con: duckdb.DuckDBPyConnection) -> bool:
    try:
        return bool(
            con.execute("SELECT count(*) FROM archive_files").fetchone()[0]
        )
    except Exception:
        return False


def _create_views(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE VIEW v_sessions AS
        SELECT * FROM ({_sessions_sql('raw_records')})
        UNION ALL
        SELECT * FROM archive_sessions
        ORDER BY last_active DESC
    """)

    con.execute(f"""
        CREATE VIEW v_assistant_turns AS
        SELECT * FROM ({_turns_sql('raw_records')})
        UNION ALL
        SELECT * FROM archive_turns
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


def _create_fts_index(con: duckdb.DuckDBPyConnection) -> None:
    """Build a full-text index over early user messages for session search."""
    try:
        try:
            con.execute("LOAD fts")
        except duckdb.Error:
            # Not bundled with the wheel on every platform; needs a one-off
            # download. Without it search silently degrades to substring match.
            con.execute("INSTALL fts")
            con.execute("LOAD fts")
        con.execute("DROP TABLE IF EXISTS session_full_text")
        con.execute(f"""
            CREATE TABLE session_full_text AS
            SELECT * FROM ({_session_text_sql('raw_records', 'v_sessions')})
            UNION ALL
            SELECT session_id, full_text FROM archive_text
        """)
        con.execute(
            "PRAGMA create_fts_index('session_full_text', 'session_id', 'full_text')"
        )
    except Exception:
        pass



def search_sessions_fts(
    con: duckdb.DuckDBPyConnection, query: str
) -> list[str] | None:
    """Return matching session IDs ordered by FTS score, or None on fallback."""
    try:
        rows = con.execute(
            """
            SELECT session_id
            FROM session_full_text
            WHERE fts_main_session_full_text.match_bm25(session_id, $1) IS NOT NULL
            ORDER BY fts_main_session_full_text.match_bm25(session_id, $1) DESC
            """,
            [query],
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return None


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
            plan_path=r[10],
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


_DIALOG_SELECT = """
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
    FROM {src}
    WHERE sessionId = $1::UUID
      AND type IN ('user', 'assistant', 'system')
      AND isSidechain = false
    ORDER BY timestamp, uuid
"""


def get_session_dialog(
    con: duckdb.DuckDBPyConnection, session_id: str
) -> list[DialogTurn]:
    """Reconstruct the conversation for a single session.

    Archived sessions are not held in raw_records, so their transcript is read
    straight off disk here and discarded once the turns are built.
    """
    rows = con.execute(
        _DIALOG_SELECT.format(src="raw_records"), [session_id]
    ).fetchall()
    if not rows:
        rows = _read_archived_dialog(con, session_id)

    return _build_dialog_turns(rows)


def _read_archived_dialog(
    con: duckdb.DuckDBPyConnection, session_id: str
) -> list:
    """Read one archived session's records directly from its JSONL file."""
    try:
        row = con.execute(
            "SELECT filename FROM archive_files WHERE session_id = ? LIMIT 1",
            [session_id],
        ).fetchone()
        if not row:
            return []
        src = f"({_records_select(row[0])})"
        return con.execute(
            _DIALOG_SELECT.format(src=src), [session_id]
        ).fetchall()
    except Exception:
        return []

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
