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
| `o` | Page the session's plan doc (`$PAGER`, default `less`) |
| `O` | Open a shell in the session's scratchpad dir |
| `r` | Refresh session list |
| `a` | Load all history into `raw_records` (see [Memory](#memory)) |
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

**Plan column:** A session that ended a plan-mode turn wrote a plan doc to `~/.claude/plans/`. Those sessions are marked `▤` in the Plan column (dimmed when the file has since been deleted), the doc's name shows in the detail bar, and `o` pages it in a new window. Plan bodies are recorded in the transcript itself, so they stay searchable even after the file is gone.

**Search scope:** The full-text index covers each session's early user messages, its title, summary, and cwd, and the body of its plan doc. If the DuckDB FTS extension is unavailable, dashboard search falls back to simple substring matching.

### Query console

The Query console exposes your session data as DuckDB SQL views:

| View | Contents |
|------|----------|
| `v_sessions` | One row per session — date, project, summary, model, turn count |
| `v_assistant_turns` | Individual assistant turns with token counts |
| `v_stats` | Aggregated usage by model |
| `v_sessions_by_day` | Daily session and token totals |
| `raw_records` | Raw JSONL records — recently active sessions only; press `a` to load all history |

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

### Memory

clack holds your session history in an in-memory DuckDB. To keep that bounded, transcripts for
sessions untouched for more than 14 days are not kept resident — only their aggregates are: the
dashboard row, token totals, and search text. Everything you see stays complete; the Dashboard,
Stats and search all cover your full history regardless of age, and opening an old conversation
reads that one file from disk on demand.

The one place the split is visible is the Query console, where `raw_records` holds recent sessions
only. Press `a` to load the archived transcripts into it — this costs roughly whatever those files
weigh on disk, and restarting clack releases it again.


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
