"""cmux integration for resuming Claude Code sessions.

Mirrors the surface of clack.tmux, but talks to manaflow-ai/cmux
(https://github.com/manaflow-ai/cmux) via its `cmux` CLI.

cmux differs from tmux in two ways that matter to us:
- It exposes no pane-tty path, so we can't join `ps` rows to panes via tty.
  Instead we read `CMUX_SURFACE_ID` from each claude process's environment
  (via `ps -E`) and look that surface up in `cmux list-panes --json`.
- `cmux new-workspace --cwd ... --command ...` is the only spawn command that
  natively takes both a working directory and a command line, so it stands in
  for `tmux new-window -n NAME "cd CWD && claude --resume SID"`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from clack.tmux import (
    ActivePane,
    _resume_suspended,
    is_in_tmux,
    resolve_session_ids,
)

_DEBUG_LOG = Path.home() / ".cache" / "clack" / "cmux.log"


def _log(msg: str) -> None:
    if not os.environ.get("CLACK_DEBUG_CMUX"):
        return
    try:
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG.open("a") as f:
            f.write(f"{datetime.now().isoformat()} {msg}\n")
    except OSError:
        pass


def is_in_cmux() -> bool:
    """True when cmux — and not a real tmux inside a cmux tab — owns panes.

    Running tmux inside a cmux tab leaves CMUX_WORKSPACE_ID set, but the panes
    the user sees are tmux's, so the tmux backend has to win that case.
    """
    return "CMUX_WORKSPACE_ID" in os.environ and not is_in_tmux()


# Known install paths to probe if `cmux` isn't on PATH (e.g. clack-tui was
# launched from a shell with a slimmer PATH than the interactive one).
_CMUX_FALLBACK_PATHS = (
    "/Applications/cmux.app/Contents/Resources/bin/cmux",
    "/opt/homebrew/bin/cmux",
    "/usr/local/bin/cmux",
)


@lru_cache(maxsize=1)
def _cmux_bin() -> str | None:
    """Resolve the cmux binary, honoring PATH first then known install paths.

    Cached for the lifetime of the process; restart clack-tui to re-probe.
    """
    found = shutil.which("cmux")
    if found:
        return found
    for path in _CMUX_FALLBACK_PATHS:
        if os.access(path, os.X_OK):
            return path
    return None


def get_active_claude_panes() -> list[ActivePane]:
    """Detect Claude processes and join them to cmux panes via env vars.

    On any cmux CLI failure or unexpected JSON shape we degrade gracefully:
    each pane that we can't enrich falls back to a "pid:<pid>" label.
    """
    # 1. Discover workspace ids → workspace names.
    workspaces = _list_workspaces()

    # 2. For each workspace, list panes and build a surface_id → pane info map.
    # We key by surface UUID because cmux's hierarchy is workspace → pane →
    # surface, and a single pane can contain multiple surfaces (tabs). The
    # surface UUID is what `focus-panel` consumes to land on the right tab.
    surface_to_pane: dict[str, dict] = {}
    for ws_id, ws_name in workspaces.items():
        for pane_ref, surface_ids in _list_panes(ws_id):
            for sid in surface_ids:
                surface_to_pane[sid] = {
                    "surface_id": sid,
                    "pane_ref": pane_ref,
                    "workspace_id": ws_id,
                    "workspace_name": ws_name,
                }

    # 3. Walk `ps` for claude processes (same parsing as tmux.py).
    claude_procs = _ps_claude_processes()
    if not claude_procs:
        return []

    # 4. For each claude pid, read its env to grab CMUX_SURFACE_ID / WORKSPACE_ID.
    pid_to_env = _batch_get_envs([p[0] for p in claude_procs])
    _log(f"pid_to_env={pid_to_env}  surface_to_pane_keys={list(surface_to_pane.keys())}")

    # 5. Resolve each process to its session. Shared with the tmux backend —
    # ~/.claude/sessions/<pid>.json is written by Claude Code itself, so it
    # carries no multiplexer state and works the same under cmux.
    pid_to_session, pid_to_status = resolve_session_ids(claude_procs)

    active: list[ActivePane] = []
    for pid, tty, _resume_sid, _start_ts in claude_procs:
        env = pid_to_env.get(pid, {})
        surface_id = env.get("CMUX_SURFACE_ID")
        pane_info = surface_to_pane.get(surface_id) if surface_id else None
        # Fall back: even without a known pane, the env may still tell us
        # the workspace/surface so the user can at least see which workspace.
        if pane_info is None and surface_id:
            pane_info = {
                "pane_ref": surface_id,
                "workspace_id": env.get("CMUX_WORKSPACE_ID"),
                "workspace_name": workspaces.get(
                    env.get("CMUX_WORKSPACE_ID", ""), None
                ),
            }

        # For cmux we stash the surface UUID in pane_id (it's what focus-panel
        # consumes to land on the correct tab) and the pane_ref in window_name
        # (used as a fallback for focus-pane if focus-panel doesn't exist).
        active.append(ActivePane(
            pid=pid,
            tty=tty,
            session_id=pid_to_session.get(pid),
            status=pid_to_status.get(pid),
            pane_id=pane_info["surface_id"] if pane_info else None,
            window_name=pane_info["pane_ref"] if pane_info else None,
            session_name=pane_info["workspace_name"] if pane_info else None,
            mux="cmux" if pane_info else None,
            workspace_id=pane_info["workspace_id"] if pane_info else None,
        ))

    return active


def find_pane_for_session(session_id: str) -> ActivePane | None:
    for pane in get_active_claude_panes():
        if pane.session_id == session_id:
            return pane
    return None


def jump_to_pane(pane: ActivePane) -> bool:
    """Focus a cmux surface (tab) within its pane.

    cmux's hierarchy is workspace → pane → surface, and a pane can host
    multiple surfaces. `focus-pane` lands on the pane but doesn't switch
    surfaces, so a user with two claude sessions in the same pane would
    keep landing on whichever was most recently active. We use
    `focus-panel <surface-uuid>` (cmux's name for the focus-a-surface
    primitive) and fall back through focus-pane → select-workspace.
    """
    cmux_bin = _cmux_bin()
    if not cmux_bin:
        return False
    # 1. focus-panel --panel <surface-uuid> — lands on the exact tab
    if pane.pane_id and _run_focus(
        cmux_bin, "focus-panel", "--panel", pane.pane_id, pane.workspace_id
    ):
        return True
    # 2. focus-pane <pane-ref> — at least lands in the right pane
    if pane.window_name and _run_focus(
        cmux_bin, "focus-pane", None, pane.window_name, pane.workspace_id
    ):
        return True
    # 3. select-workspace — lands in the right workspace
    if pane.workspace_id:
        return _select_workspace(pane.workspace_id)
    return False


def _run_focus(
    cmux_bin: str,
    subcmd: str,
    target_flag: str | None,
    target: str,
    workspace_id: str | None,
) -> bool:
    args = [cmux_bin, subcmd]
    if target_flag:
        args.extend([target_flag, target])
    else:
        args.append(target)
    if workspace_id:
        args.extend(["--workspace", workspace_id])
    try:
        r = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        _log(f"{subcmd}: FileNotFoundError args={args}")
        return False
    _log(
        f"{subcmd} rc={r.returncode} args={args} "
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    return r.returncode == 0


def resume_in_new_workspace(session_id: str, cwd: str) -> bool:
    """Open a new cmux workspace running `claude --resume`, and focus it.

    cmux's `new-workspace` returns `OK workspace:NN` on success but doesn't
    bring the new workspace to the front, so we parse the ref out and follow
    up with `select-workspace` to make the spawn actually visible.
    """
    cmux_bin = _cmux_bin()
    if not cmux_bin:
        return False
    name = f"claude-{session_id[:8]}"
    args = [
        cmux_bin, "new-workspace",
        "--name", name,
        "--cwd", cwd,
        "--command", f"claude --resume {session_id}",
    ]
    try:
        r = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        _log(f"new-workspace: FileNotFoundError args={args}")
        return False
    _log(
        f"new-workspace rc={r.returncode} args={args} "
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    if r.returncode != 0:
        return False

    ref = _parse_workspace_ref(r.stdout)
    if ref:
        _select_workspace(ref)
    return True


def _parse_workspace_ref(stdout: str) -> str | None:
    """Extract a `workspace:NN` ref from a cmux command's stdout.

    new-workspace prints `OK workspace:33\n`; tolerate surrounding text and
    alternative id formats (uuid, etc.) by grabbing the first `workspace:*`
    token we see.
    """
    m = re.search(r"workspace:\S+", stdout)
    return m.group(0) if m else None


def _select_workspace(ref: str) -> bool:
    cmux_bin = _cmux_bin()
    if not cmux_bin:
        return False
    args = [cmux_bin, "select-workspace", "--workspace", ref]
    try:
        r = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        _log(f"select-workspace: FileNotFoundError args={args}")
        return False
    _log(
        f"select-workspace rc={r.returncode} args={args} "
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    return r.returncode == 0


def resume_session(app, session_id: str, cwd: str) -> None:
    """Resume a Claude Code session inside cmux, with fallback on CLI failure."""
    if is_in_cmux():
        pane = find_pane_for_session(session_id)
        if pane and jump_to_pane(pane):
            return
        if resume_in_new_workspace(session_id, cwd):
            return
        # cmux CLI missing or errored — fall back to suspended exec.
    _resume_suspended(app, session_id, cwd)


# --- internal helpers --------------------------------------------------------


def _run_cmux_json(args: list[str]) -> object | None:
    cmux_bin = _cmux_bin()
    if not cmux_bin:
        return None
    try:
        result = subprocess.run(
            [cmux_bin, *args],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None
    _log(
        f"cmux-json args={args} rc={result.returncode} "
        f"stdout={result.stdout[:600]!r} stderr={result.stderr[:200]!r}"
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _list_workspaces() -> dict[str, str]:
    """Return {workspace_id: workspace_name}. Empty on any failure."""
    data = _run_cmux_json(["list-workspaces", "--json"])
    out: dict[str, str] = {}
    items = _coerce_list(data, "workspaces")
    for item in items:
        if not isinstance(item, dict):
            continue
        wid = _first_str(item, "id", "ref", "uuid", "workspace_id")
        name = _first_str(item, "name", "title", "ref") or wid or "?"
        if wid:
            out[wid] = name
    return out


def _list_panes(workspace_id: str) -> list[tuple[str, list[str]]]:
    """Return [(pane_ref, [surface_id, ...]), ...] for the workspace."""
    data = _run_cmux_json([
        "list-panes",
        "--workspace", workspace_id,
        "--json",
        "--id-format", "both",
    ])
    out: list[tuple[str, list[str]]] = []
    for item in _coerce_list(data, "panes"):
        if not isinstance(item, dict):
            continue
        pane_ref = _first_str(item, "ref", "id", "uuid", "pane_id")
        if not pane_ref:
            continue
        surfaces: list[str] = []
        raw_surfaces = item.get("surfaces") or item.get("surface_ids") or []
        if isinstance(raw_surfaces, list):
            for s in raw_surfaces:
                if isinstance(s, str):
                    surfaces.append(s)
                elif isinstance(s, dict):
                    sid = _first_str(s, "id", "ref", "uuid", "surface_id")
                    if sid:
                        surfaces.append(sid)
        out.append((pane_ref, surfaces))
    return out


def _coerce_list(data: object, key: str) -> list:
    """Handle both `[...]` and `{"<key>": [...]}` shapes."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        v = data.get(key)
        if isinstance(v, list):
            return v
    return []


def _first_str(d: dict, *keys: str) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _ps_claude_processes() -> list[tuple[int, str, str | None, float]]:
    """Same as the tmux backend, minus the tty-to-pane filtering."""
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
    out: list[tuple[int, str, str | None, float]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if "claude" not in line:
            continue
        m = re.match(
            r"(\d+)\s+(\S+)\s+\w+\s+(\w+\s+\d+\s+[\d:]+\s+\d+)\s+(.*)", line,
        )
        if not m:
            continue
        pid_str, tty, date_str, args = m.groups()
        if not claude_re.search(args):
            continue
        try:
            start_ts = datetime.strptime(date_str, "%b %d %H:%M:%S %Y").timestamp()
        except ValueError:
            continue
        rm = resume_re.search(args)
        out.append((int(pid_str), tty, rm.group(1) if rm else None, start_ts))
    return out


def _batch_get_envs(pids: list[int]) -> dict[int, dict[str, str]]:
    """Read each pid's env via `ps -E`. Only the user's own procs are readable.

    macOS `ps -E -p PID -o pid,command=` prints the command followed by
    `KEY=VAL KEY=VAL ...` env pairs on the same line. We only care about the
    CMUX_* keys, so a coarse split is enough.
    """
    if not pids:
        return {}
    try:
        result = subprocess.run(
            ["ps", "-E", "-o", "pid=,command=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return {}
    if result.returncode != 0:
        return {}

    out: dict[int, dict[str, str]] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        try:
            pid = int(head)
        except ValueError:
            continue
        env: dict[str, str] = {}
        for token in rest.split():
            if "=" in token and token.split("=", 1)[0].isidentifier():
                k, v = token.split("=", 1)
                if k.startswith("CMUX_"):
                    env[k] = v
        if env:
            out[pid] = env
    return out
