# clack

[![PyPI](https://img.shields.io/pypi/v/clack-tui)](https://pypi.org/project/clack-tui/)
[![Python](https://img.shields.io/pypi/pyversions/clack-tui)](https://pypi.org/project/clack-tui/)
[![License](https://img.shields.io/github/license/jcc-ne/clack)](LICENSE)

A terminal UI for browsing, searching, and resuming [Claude Code](https://claude.ai/code) sessions.

Browse your full session history, read past conversations, jump into stats, and resume any session — all without leaving the terminal.

---

## Install

```bash
# pipx
pipx install clack-tui

# uvx (run without installing)
uvx clack-tui
```

The package name is `clack-tui` because `clack` is already taken on PyPI. The package installs both `clack` and `clack-tui` executables.

Requires Python 3.11+ and [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview) installed.

---

## Quick start

```bash
clack
```

clack reads your Claude Code session files directly from `~/.claude/projects/` — no configuration needed.

---

## Features

| Tab | Key | What it does |
|-----|-----|--------------|
| Dashboard | `1` | Browse all sessions, search with full-text search (DuckDB FTS / BM25) |
| Stats | `2` | Token usage and model breakdown, daily sparklines |
| Dialog | `3` | Read any conversation turn-by-turn, export to HTML |
| Query | `4` | Write SQL directly against your session data (DuckDB) |

### Dashboard key bindings

| Key | Action |
|-----|--------|
| `/` | Focus search |
| `Esc` | Clear search |
| `Enter` | Resume session (opens `claude --resume`) |
| `v` | View full conversation |
| `p` | Open the session's PR in a browser (`gh pr view -w`) |
| `r` | Refresh session list |
| `q` | Quit |

**PR column:** Each session records the git branch it was working on, so clack can show the pull request that branch belongs to — `repo#number` with a colored icon for its state:

| | State |
|---|-------|
| `○` blue | open |
| `◐` yellow | commented |
| `◐` red | changes requested |
| `●` green | approved |
| `✔` dim | merged |
| `✕` dim | closed |

This needs the [`gh` CLI](https://cli.github.com/) on your `PATH` and authenticated; without it the column is simply blank. PRs are fetched in the background — one `gh pr list` per repo, several repos at a time, cached for five minutes — so the dashboard stays responsive and the column fills in repo by repo.

**tmux / cmux:** If clack is running inside a tmux session, resuming opens the session in a new tmux window (or jumps to the existing pane if that session is already live). The same behavior works inside [cmux](https://github.com/manaflow-ai/cmux) — clack opens a new cmux workspace via `cmux new-workspace --cwd ... --command ...`, or focuses the existing pane with `cmux focus-pane`. Outside any multiplexer, clack suspends the TUI, runs `claude --resume`, and returns when you exit.

If the DuckDB FTS extension is unavailable, dashboard search falls back to simple substring matching.

### Query console

The Query console exposes your session data as DuckDB SQL views:

| View | Contents |
|------|----------|
| `v_sessions` | One row per session — date, project, summary, model, turn count |
| `v_assistant_turns` | Individual assistant turns with token counts |
| `v_stats` | Aggregated usage by model |
| `v_sessions_by_day` | Daily session and token totals |
| `raw_records` | Raw JSONL records |

Example queries:

```sql
-- Sessions from the last week
SELECT title, cwd, turn_count FROM v_sessions
WHERE last_active > now() - INTERVAL '7 days';

-- Most token-heavy sessions
SELECT sessionId, SUM(output_tokens) AS total
FROM v_assistant_turns GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

---

## Dev setup

```bash
git clone https://github.com/jcc-ne/clack
cd clack
uv sync
uv run clack
```

Release notes for TestPyPI and Trusted Publishing live in [docs/releasing.md](docs/releasing.md).

---

## Requirements

- Python 3.11+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview) (session files at `~/.claude/projects/`)
- tmux or [cmux](https://github.com/manaflow-ai/cmux) (optional — enables jumping to live sessions and opening resumed sessions in a new window/workspace)
