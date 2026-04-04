"""tmux integration for resuming Claude Code sessions."""

from __future__ import annotations

import os
import subprocess


def resume_session(app, session_id: str, cwd: str) -> None:
    """Resume a Claude Code session, using tmux if available."""
    if is_in_tmux():
        _resume_tmux_window(session_id, cwd)
    else:
        _resume_suspended(app, session_id, cwd)


def is_in_tmux() -> bool:
    return "TMUX" in os.environ


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
