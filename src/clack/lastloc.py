"""Persisted record of where each session was last seen running.

The DuckDB store is rebuilt from JSONL on every run (:memory:), so the last
known mux location needs its own file-backed cache. One JSON file, keyed by
session id, living beside the cmux debug log.

Everything here degrades to a no-op on I/O trouble: a stale or unreadable
cache should never take the TUI down.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clack.tmux import ActivePane

STORE_PATH = Path.home() / ".cache" / "clack" / "last_loc.json"

# Entries older than this are dropped on the next write, so the file tracks
# roughly the same horizon a user would plausibly want to jump back to.
_MAX_AGE = timedelta(days=30)

_cache: dict[str, dict] | None = None


def load() -> dict[str, dict]:
    """Return the store, reading it from disk on first use."""
    global _cache
    if _cache is None:
        _cache = _read()
    return _cache


def get(session_id: str) -> dict | None:
    return load().get(session_id)


def record(panes: Iterable[ActivePane]) -> None:
    """Upsert one entry per located pane, writing only when something changed."""
    store = load()
    now = datetime.now().isoformat(timespec="seconds")
    changed = False

    for pane in panes:
        if not pane.session_id or not pane.session_name:
            continue
        entry = {
            "mux": pane.mux,
            "session_name": pane.session_name,
            "window_index": pane.window_index,
            "pane_index": pane.pane_index,
            "window_name": pane.window_name,
            "label": pane.label,
            "seen_at": now,
        }
        prev = store.get(pane.session_id)
        if prev == entry:
            continue
        # A refresh that only bumps seen_at isn't worth a disk write.
        if prev is not None and {k: v for k, v in prev.items() if k != "seen_at"} == {
            k: v for k, v in entry.items() if k != "seen_at"
        }:
            store[pane.session_id] = entry
            continue
        store[pane.session_id] = entry
        changed = True

    if changed:
        _write(_prune(store))


def _prune(store: dict[str, dict]) -> dict[str, dict]:
    cutoff = datetime.now() - _MAX_AGE
    kept: dict[str, dict] = {}
    for sid, entry in store.items():
        try:
            if datetime.fromisoformat(entry["seen_at"]) >= cutoff:
                kept[sid] = entry
        except (KeyError, TypeError, ValueError):
            pass  # unparseable entry — drop it
    return kept


def _read() -> dict[str, dict]:
    try:
        with STORE_PATH.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _write(store: dict[str, dict]) -> None:
    global _cache
    _cache = store
    tmp = STORE_PATH.with_suffix(".json.tmp")
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w") as f:
            json.dump(store, f, indent=1)
        os.replace(tmp, STORE_PATH)
    except OSError:
        pass
