"""tmux integration for resuming Claude Code sessions."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude/projects"

# Claude Code 2.x writes <pid>.json here at startup and removes it on exit, so
# the directory is a live index of running sessions — an exact pid → session
# mapping that makes the birthtime heuristic below unnecessary in the common case.
CLAUDE_SESSIONS_DIR = Path.home() / ".claude/sessions"

# How far a pid file's recorded start may drift from `ps` lstart before we treat
# it as stale (left behind by a since-recycled pid). `startedAt` is epoch millis
# and tracked lstart to within ~1.3s on every observed process, so this is
# generous. Use `startedAt`, never the sibling `procStart` field — that one is
# rendered in UTC while lstart is local time.
_PID_FILE_START_TOLERANCE = 10.0

# Fallback birthtime window (seconds), used only when a process's exact encoded
# project dir is missing and we scan sibling dirs instead. Tighter than the
# exact-dir +300s grace because the sibling scan widens the candidate pool — a
# loose window would let an unrelated session in the same parent tree get
# claimed. 90s comfortably covers the observed 4-72s launch→JSONL-creation lag.
_FALLBACK_BIRTH_WINDOW = 90.0


@dataclass
class ActivePane:
    """A claude process, optionally associated with a tmux or cmux pane."""

    pid: int
    tty: str
    session_id: str | None  # resolved from pid file, --resume arg, or JSONL match
    # mux-only fields (None when not running inside any multiplexer)
    pane_id: str | None = None
    session_name: str | None = None  # tmux session / cmux workspace
    window_index: int | None = None
    pane_index: int | None = None
    window_name: str | None = None
    mux: str | None = None  # "tmux" | "cmux" | None
    workspace_id: str | None = None  # cmux workspace ref for select-workspace fallback
    status: str | None = None  # "idle" | "busy", from the pid file

    @property
    def label(self) -> str:
        if self.mux == "cmux" and self.pane_id is not None:
            ws = self.session_name or "?"
            return f"{ws}:{self.pane_id}"
        if self.window_name is not None:
            return format_tmux_label(
                self.session_name, self.window_name,
                self.window_index, self.pane_index,
            )
        return f"pid:{self.pid}"


def format_tmux_label(
    session_name: str | None,
    window_name: str | None,
    window_index: int | None,
    pane_index: int | None,
) -> str:
    """Render a pane location as `<session>:<window>:<win_idx>.<pane_idx>`.

    The session name is included because window names alone repeat across tmux
    sessions, so `clack:9.1` is ambiguous once you run more than one.
    """
    loc = f"{window_name}:{window_index}.{pane_index}"
    return f"{session_name}:{loc}" if session_name else loc


def get_active_claude_panes() -> list[ActivePane]:
    """Detect all running Claude processes, with mux pane info when available.

    Dispatches to the cmux backend when running inside cmux, otherwise uses
    the tmux backend (which also covers the "no multiplexer" case — it just
    returns processes without pane info).
    """
    from clack import cmux

    if cmux.is_in_cmux():
        return cmux.get_active_claude_panes()
    return _get_active_tmux_panes()


def _get_active_tmux_panes() -> list[ActivePane]:
    """Detect all running Claude processes, with tmux pane info when available.

    For --resume <id> processes, session_id comes from the command args.
    For others (fresh starts, --continue, --resume without id), session_id
    is resolved by matching the process cwd + start time to JSONL files.

    When running inside tmux, each result also carries pane location fields
    (pane_id, session_name, window_index, pane_index, window_name).
    Outside tmux those fields are None and label falls back to "pid:<pid>".
    """
    # Step 1: Get all tmux panes with their TTYs and location info (tmux only)
    tty_to_pane: dict[str, dict] = {}
    if is_in_tmux():
        try:
            result = subprocess.run(
                [
                    "tmux", "list-panes", "-a", "-F",
                    "#{pane_id}\t#{pane_tty}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{window_name}",
                ],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                return []
        except FileNotFoundError:
            return []

        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) != 6:
                continue
            pane_id, tty, sess_name, win_idx, pane_idx, win_name = parts
            short_tty = tty.replace("/dev/", "")
            tty_to_pane[short_tty] = {
                "pane_id": pane_id,
                "session_name": sess_name,
                "window_index": int(win_idx),
                "pane_index": int(pane_idx),
                "window_name": win_name,
            }

    # Step 2: Find all claude processes with TTYs, args, and start times
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,tty,lstart,args"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return []
    except FileNotFoundError:
        return []

    claude_re = re.compile(r"(?:^|/)claude(?:\s|$)")
    resume_re = re.compile(r"--resume\s+([\w-]+)")
    # (pid, tty, resume_sid, start_timestamp)
    claude_procs: list[tuple[int, str, str | None, float]] = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if "claude" not in line:
            continue
        # Format: PID TTY DAY MON DD HH:MM:SS YYYY ARGS...
        # e.g.: 46797 ttys008 Tue Apr  7 11:48:14 2026 claude --continue
        m = re.match(
            r"(\d+)\s+(\S+)\s+\w+\s+(\w+\s+\d+\s+[\d:]+\s+\d+)\s+(.*)", line
        )
        if not m:
            continue
        pid_str, tty, date_str, args = m.groups()
        if not claude_re.search(args):
            continue
        # Inside tmux: only track processes that belong to a known pane
        if tty_to_pane and tty not in tty_to_pane:
            continue

        try:
            from datetime import datetime
            start_time = datetime.strptime(date_str, "%b %d %H:%M:%S %Y").timestamp()
        except ValueError:
            continue

        rm = resume_re.search(args)
        claude_procs.append((
            int(pid_str), tty,
            rm.group(1) if rm else None,
            start_time,
        ))

    if not claude_procs:
        return []

    # Step 3: Resolve each process to its session (pid file, then fallbacks)
    pid_to_session, pid_to_status = resolve_session_ids(claude_procs)

    active: list[ActivePane] = []
    for pid, tty, _resume_sid, _start_ts in claude_procs:
        pane_info = tty_to_pane.get(tty)

        active.append(ActivePane(
            pid=pid,
            tty=tty,
            session_id=pid_to_session.get(pid),
            status=pid_to_status.get(pid),
            pane_id=pane_info["pane_id"] if pane_info else None,
            session_name=pane_info["session_name"] if pane_info else None,
            window_index=pane_info["window_index"] if pane_info else None,
            pane_index=pane_info["pane_index"] if pane_info else None,
            window_name=pane_info["window_name"] if pane_info else None,
            mux="tmux" if pane_info else None,
        ))

    return active


def _read_pid_session_files(
    procs: list[tuple[int, str, str | None, float]],
) -> dict[int, tuple[str, str | None]]:
    """Read `~/.claude/sessions/<pid>.json` for each process.

    Returns {pid: (session_id, status)}. Processes with no file, an unreadable
    or malformed file, or a recorded start time that disagrees with `ps` are
    omitted so the caller falls through to the older heuristics.
    """
    out: dict[int, tuple[str, str | None]] = {}
    for pid, _tty, _resume_sid, start_ts in procs:
        try:
            data = json.loads((CLAUDE_SESSIONS_DIR / f"{pid}.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        session_id = data.get("sessionId")
        started_at = data.get("startedAt")
        if not isinstance(session_id, str) or not session_id:
            continue
        if not isinstance(started_at, (int, float)):
            continue
        if abs(started_at / 1000 - start_ts) > _PID_FILE_START_TOLERANCE:
            continue  # stale file from a recycled pid
        status = data.get("status")
        out[pid] = (session_id, status if isinstance(status, str) else None)
    return out


def resolve_session_ids(
    procs: list[tuple[int, str, str | None, float]],
) -> tuple[dict[int, str], dict[int, str]]:
    """Resolve pid -> session_id for claude processes, plus pid -> status.

    Prefers `~/.claude/sessions/<pid>.json`: it names the process's *current*
    session, so it stays correct after /clear or /compact rotates to a new id,
    whereas a `--resume <id>` arg is frozen at launch. Falls back to the resume
    arg, then to birthtime matching for anything still unresolved (older Claude
    Code versions that don't write the file).
    """
    from_pid_file = _read_pid_session_files(procs)

    sessions: dict[int, str] = {}
    statuses: dict[int, str] = {}
    for pid, (sid, status) in from_pid_file.items():
        sessions[pid] = sid
        if status:
            statuses[pid] = status

    for pid, _tty, resume_sid, _start_ts in procs:
        if pid not in sessions and resume_sid:
            sessions[pid] = resume_sid

    # Whatever is left predates the pid file (or lost its file); fall back to
    # matching process cwd + start time against JSONL birthtimes.
    needs_resolve = [p for p in procs if p[0] not in sessions]
    if needs_resolve:
        pid_to_cwd = _batch_get_cwds([p[0] for p in needs_resolve])
        sessions.update(
            _assign_sessions(needs_resolve, pid_to_cwd, claimed=set(sessions.values()))
        )

    return sessions, statuses


def _batch_get_cwds(pids: list[int]) -> dict[int, str]:
    """Get the cwd of multiple processes in a single lsof call."""
    if not pids:
        return {}
    pid_arg = ",".join(str(p) for p in pids)
    try:
        result = subprocess.run(
            ["lsof", "-d", "cwd", "-a", "-p", pid_arg],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return {}

    pid_to_cwd: dict[int, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("COMMAND"):
            continue
        parts = line.split()
        if len(parts) >= 9:
            pid_to_cwd[int(parts[1])] = parts[-1]
    return pid_to_cwd


def _assign_sessions(
    procs: list[tuple[int, str, str | None, float]],
    pid_to_cwd: dict[int, str],
    claimed: set[str] | None = None,
) -> dict[int, str]:
    """Assign session IDs to processes, ensuring each session is claimed once.

    Fallback for processes that `resolve_session_ids` couldn't place from
    `~/.claude/sessions/<pid>.json`. For each process, finds JSONL files that
    were created within 5 minutes of the process start and modified after it
    started.  The grace window accounts for the delay between process launch and
    JSONL file creation (observed 4-72s) and for mid-session JSONL rotation
    (e.g. /clear, /compact) — but not for a process that idles at the prompt
    longer than that before writing its first line, which is exactly the case
    the pid file handles.  When multiple processes match the same session, the
    process whose start time is closest to the JSONL birthtime wins.

    `claimed` holds session ids already resolved by a more reliable means, so
    this pass can't hand the same session to a second process.
    """
    # Build candidate pairs: (pid, session_id, distance)
    # where distance = process_start - jsonl_birthtime (smaller = better match)
    candidates: list[tuple[int, str, float]] = []

    # Cache project dir listings by encoded cwd, and sibling scans by cwd, so a
    # missing dir is only scanned once even with several unresolved processes.
    dir_cache: dict[str, list[tuple[str, float, float]]] = {}  # encoded -> [(sid, birth, mtime)]
    sibling_cache: dict[str, list[tuple[str, float, float]]] = {}  # cwd -> [(sid, birth, mtime)]

    for pid, _tty, _resume_sid, start_ts in procs:
        cwd = pid_to_cwd.get(pid)
        if not cwd:
            continue

        encoded = re.sub(r"[/._]", "-", cwd)
        if encoded not in dir_cache:
            dir_cache[encoded] = _list_project_jsonls(PROJECTS_DIR / encoded)

        entries = dir_cache[encoded]
        if entries:
            # Exact-dir match: proven behavior, generous +300s grace window.
            for sid, birthtime, mtime in entries:
                if birthtime <= start_ts + 300 and mtime >= start_ts:
                    candidates.append((pid, sid, abs(start_ts - birthtime)))
        else:
            # No project dir for this cwd — most often the working directory was
            # renamed after the session started, so its JSONL history lives
            # under the *old* encoded name. Scan sibling dirs sharing cwd's
            # parent path, with a tighter window to keep the widened pool from
            # claiming the wrong session.
            if cwd not in sibling_cache:
                sibling_cache[cwd] = _sibling_project_jsonls(cwd)
            for sid, birthtime, mtime in sibling_cache[cwd]:
                if abs(start_ts - birthtime) <= _FALLBACK_BIRTH_WINDOW and mtime >= start_ts:
                    candidates.append((pid, sid, abs(start_ts - birthtime)))

    # Greedy assignment: sort by distance (closest match first),
    # assign each session to at most one process
    candidates.sort(key=lambda x: x[2])
    claimed_sessions: set[str] = set(claimed or ())
    claimed_pids: set[int] = set()
    result: dict[int, str] = {}

    for pid, sid, _dist in candidates:
        if pid in claimed_pids or sid in claimed_sessions:
            continue
        result[pid] = sid
        claimed_pids.add(pid)
        claimed_sessions.add(sid)

    return result


def _list_project_jsonls(project_dir: Path) -> list[tuple[str, float, float]]:
    """Return [(session_id, birthtime, mtime), ...] for one project dir."""
    entries: list[tuple[str, float, float]] = []
    if not project_dir.is_dir():
        return entries
    for f in project_dir.glob("*.jsonl"):
        try:
            st = f.stat()
            entries.append((f.stem, st.st_birthtime, st.st_mtime))
        except (OSError, AttributeError):
            pass
    return entries


def encode_project_slug(path: str) -> str:
    """Encode a filesystem path the way Claude Code names its per-project dirs.

    Used for both `~/.claude/projects/<slug>` and the scratchpad tree under
    /tmp, which share the encoding.
    """
    return re.sub(r"[/._]", "-", path)


def scratchpad_dir(cwd: str, session_id: str) -> Path | None:
    """A session's scratchpad dir, or None when it no longer exists.

    These live under /tmp, so they are routinely cleaned up out from under an
    older session — absence is the normal case, not an error.
    """
    d = (
        Path(f"/private/tmp/claude-{os.getuid()}")
        / encode_project_slug(cwd)
        / session_id
        / "scratchpad"
    )
    return d if d.is_dir() else None


def _sibling_project_jsonls(cwd: str) -> list[tuple[str, float, float]]:
    """JSONLs from project dirs whose encoded name shares cwd's parent path.

    Used as a fallback when the exact encoded project dir for a process cwd is
    missing — typically because the working directory was renamed after the
    session started, leaving its JSONL history under the old encoded name. A
    rename changes only the trailing path segment, so restricting to dirs that
    share cwd's encoded parent prefix keeps the pool to a handful of siblings
    instead of every project dir (a stat-only scan; contents are never read).
    """
    prefix = encode_project_slug(str(Path(cwd).parent)) + "-"
    out: list[tuple[str, float, float]] = []
    try:
        children = list(PROJECTS_DIR.iterdir())
    except OSError:
        return out
    for d in children:
        if d.is_dir() and d.name.startswith(prefix):
            out.extend(_list_project_jsonls(d))
    return out


def jump_to_pane(pane: ActivePane) -> None:
    """Switch the attached client to the tmux window and pane."""
    target = f"{pane.session_name}:{pane.window_index}.{pane.pane_index}"
    subprocess.run(["tmux", "switch-client", "-t", target], check=False)


# A pane whose foreground command is one of these is sitting at a prompt, so
# it's safe to type a resume command into.
_IDLE_COMMANDS = frozenset({"zsh", "bash", "fish", "sh", "tcsh", "ksh", "dash"})


def _tmux_pane_state(
    session_name: str, window_index: int, pane_index: int
) -> str:
    """Return "idle", "busy", or "missing" for a recorded pane location."""
    try:
        result = subprocess.run(
            [
                "tmux", "list-panes", "-a", "-F",
                "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_current_command}",
            ],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return "missing"
    if result.returncode != 0:
        return "missing"

    want = (session_name, str(window_index), str(pane_index))
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        if (parts[0], parts[1], parts[2]) == want:
            return "idle" if parts[3] in _IDLE_COMMANDS else "busy"
    return "missing"


def _tmux_session_exists(session_name: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", f"={session_name}"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def _resume_command(session_id: str, cwd: str) -> str:
    return f"cd {shlex.quote(cwd)} && claude --resume {session_id}"


def _resume_at_last_loc(session_id: str, cwd: str) -> bool:
    """Try to resume in the pane (or at least the tmux session) last seen.

    Falls through to the caller's default when there's no usable record: the
    exact pane is reused only when it's idle, otherwise we settle for a new
    window in the same tmux session so the session stays where the user
    organized it.
    """
    from clack import lastloc

    entry = lastloc.get(session_id)
    if not entry or entry.get("mux") != "tmux":
        return False
    sess = entry.get("session_name")
    win = entry.get("window_index")
    pane = entry.get("pane_index")
    if not isinstance(sess, str) or not isinstance(win, int) or not isinstance(pane, int):
        return False

    cmd = _resume_command(session_id, cwd)

    if _tmux_pane_state(sess, win, pane) == "idle":
        target = f"{sess}:{win}.{pane}"
        r = subprocess.run(
            ["tmux", "send-keys", "-t", target, cmd, "Enter"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            subprocess.run(["tmux", "switch-client", "-t", target], check=False)
            return True

    if _tmux_session_exists(sess):
        window_name = f"claude-{session_id[:8]}"
        r = subprocess.run(
            ["tmux", "new-window", "-t", f"{sess}:", "-n", window_name, cmd],
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            subprocess.run(
                ["tmux", "switch-client", "-t", f"{sess}:{window_name}"], check=False
            )
            return True

    return False


def resume_session(app, session_id: str, cwd: str) -> None:
    """Resume a Claude Code session, jumping to an existing pane if one is active."""
    from clack import cmux

    if cmux.is_in_cmux():
        pane = cmux.find_pane_for_session(session_id)
        if pane and cmux.jump_to_pane(pane):
            return
        if cmux.resume_in_new_workspace(session_id, cwd):
            return
        # cmux CLI missing/failed — fall through to suspended exec.
        _resume_suspended(app, session_id, cwd)
        return
    if is_in_tmux():
        pane = find_pane_for_session(session_id)
        if pane:
            jump_to_pane(pane)
            return
        if _resume_at_last_loc(session_id, cwd):
            return
        _resume_tmux_window(session_id, cwd)
    else:
        _resume_suspended(app, session_id, cwd)


def find_pane_for_session(session_id: str) -> ActivePane | None:
    """Find an active tmux pane running a specific session."""
    for pane in _get_active_tmux_panes():
        if pane.session_id == session_id:
            return pane
    return None


def is_in_tmux() -> bool:
    """True when a real tmux server owns this terminal.

    cmux's tmux-compat shim sets TMUX too, but so does a real tmux running
    inside a cmux tab — and there both markers are present. We settle it by
    asking for the current pane's tty, which only real tmux reports.
    """
    if "TMUX" not in os.environ:
        return False
    if "CMUX_WORKSPACE_ID" not in os.environ:
        return True
    return _tmux_reports_pane_tty()


@lru_cache(maxsize=1)
def _tmux_reports_pane_tty() -> bool:
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_tty}"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return False
    return r.returncode == 0 and r.stdout.strip().startswith("/dev/")


def _resume_tmux_window(session_id: str, cwd: str) -> None:
    """Open a new tmux window and run claude --resume."""
    window_name = f"claude-{session_id[:8]}"
    cmd = _resume_command(session_id, cwd)
    subprocess.run(
        ["tmux", "new-window", "-n", window_name, cmd],
        check=False,
    )


def _resume_suspended(app, session_id: str, cwd: str) -> None:
    """Suspend the TUI and run claude directly."""
    _run_suspended(app, cwd, f"claude --resume {session_id}")


def _run_suspended(app, cwd: str, command: str) -> None:
    """Suspend the TUI, run a command in `cwd`, then restore the TUI."""
    with app.suspend():
        original_cwd = os.getcwd()
        try:
            os.chdir(cwd)
            os.system(command)
        finally:
            os.chdir(original_cwd)


def open_in_window(app, name: str, cwd: str, command: str) -> None:
    """Run a command in a new multiplexer window, or suspend the TUI for it.

    Same three-way dispatch as resume_session, minus the jump-to-existing-pane
    step: these windows are throwaway, so a second press opens a second one
    rather than hunting for the first.
    """
    from clack import cmux

    if cmux.is_in_cmux() and cmux.new_workspace(name, cwd, command):
        return
    if is_in_tmux():
        subprocess.run(
            ["tmux", "new-window", "-n", name, "-c", cwd, command],
            check=False,
        )
        return
    _run_suspended(app, cwd, command)
