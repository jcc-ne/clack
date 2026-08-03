"""tmux integration for resuming Claude Code sessions."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude/projects"

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
    session_id: str | None  # resolved from --resume arg or JSONL matching
    # mux-only fields (None when not running inside any multiplexer)
    pane_id: str | None = None
    session_name: str | None = None  # tmux session / cmux workspace
    window_index: int | None = None
    pane_index: int | None = None
    window_name: str | None = None
    mux: str | None = None  # "tmux" | "cmux" | None
    workspace_id: str | None = None  # cmux workspace ref for select-workspace fallback

    @property
    def label(self) -> str:
        if self.mux == "cmux" and self.pane_id is not None:
            ws = self.session_name or "?"
            return f"{ws}:{self.pane_id}"
        if self.window_name is not None:
            return f"{self.window_name}:{self.window_index}.{self.pane_index}"
        return f"pid:{self.pid}"


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

    # Step 3: For processes without a --resume <id>, resolve via JSONL matching
    needs_resolve = [p for p in claude_procs if p[2] is None]
    pid_to_cwd = _batch_get_cwds([p[0] for p in needs_resolve]) if needs_resolve else {}

    # Assign sessions globally: each session claimed by the best-matching process
    pid_to_session = _assign_sessions(needs_resolve, pid_to_cwd)

    active: list[ActivePane] = []
    for pid, tty, resume_sid, start_ts in claude_procs:
        pane_info = tty_to_pane.get(tty)
        session_id = resume_sid or pid_to_session.get(pid)

        active.append(ActivePane(
            pid=pid,
            tty=tty,
            session_id=session_id,
            pane_id=pane_info["pane_id"] if pane_info else None,
            session_name=pane_info["session_name"] if pane_info else None,
            window_index=pane_info["window_index"] if pane_info else None,
            pane_index=pane_info["pane_index"] if pane_info else None,
            window_name=pane_info["window_name"] if pane_info else None,
            mux="tmux" if pane_info else None,
        ))

    return active


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
) -> dict[int, str]:
    """Assign session IDs to processes, ensuring each session is claimed once.

    For each process, finds JSONL files that were created within 5 minutes of
    the process start and modified after it started.  The grace window accounts
    for the delay between process launch and JSONL file creation (observed
    4-72s) and for mid-session JSONL rotation (e.g. /clear, /compact).
    When multiple processes match the same session, the process whose start
    time is closest to the JSONL birthtime wins.
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
    claimed_sessions: set[str] = set()
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


def _sibling_project_jsonls(cwd: str) -> list[tuple[str, float, float]]:
    """JSONLs from project dirs whose encoded name shares cwd's parent path.

    Used as a fallback when the exact encoded project dir for a process cwd is
    missing — typically because the working directory was renamed after the
    session started, leaving its JSONL history under the old encoded name. A
    rename changes only the trailing path segment, so restricting to dirs that
    share cwd's encoded parent prefix keeps the pool to a handful of siblings
    instead of every project dir (a stat-only scan; contents are never read).
    """
    prefix = re.sub(r"[/._]", "-", str(Path(cwd).parent)) + "-"
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
    # cmux's tmux-compat shim sets TMUX too; treat that as cmux, not tmux.
    return "TMUX" in os.environ and "CMUX_WORKSPACE_ID" not in os.environ


def _resume_tmux_window(session_id: str, cwd: str) -> None:
    """Open a new tmux window and run claude --resume."""
    window_name = f"claude-{session_id[:8]}"
    cmd = f"cd {cwd} && claude --resume {session_id}"
    subprocess.run(
        ["tmux", "new-window", "-n", window_name, cmd],
        check=False,
    )


def _resume_suspended(app, session_id: str, cwd: str) -> None:
    """Suspend the TUI and run claude directly."""
    with app.suspend():
        original_cwd = os.getcwd()
        try:
            os.chdir(cwd)
            os.system(f"claude --resume {session_id}")
        finally:
            os.chdir(original_cwd)
