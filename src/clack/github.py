"""GitHub PR lookup for sessions, via the `gh` CLI.

Sessions carry a `cwd` and a `git_branch`; that's enough to find the pull
request a session's work landed in. We resolve it in two steps:

- `git rev-parse --show-toplevel` groups sessions by repo root, so several
  sessions (and several worktrees) in the same checkout share one lookup.
- `gh pr list --state all` is fetched **once per repo** and keyed by
  `headRefName`, rather than one `gh pr view <branch>` per session. With
  hundreds of sessions that's the difference between a handful of subprocesses
  and a stalled UI.

`gh` is optional: every helper here degrades to None/{} on any failure, so a
machine without `gh` (or a session outside a git repo) just gets a blank PR
column.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clack.models import SessionSummary

# How long a repo's PR list stays fresh. The dashboard auto-refreshes every
# 60s; a longer TTL here keeps that from hammering the GitHub API. Review state
# changes on human timescales, so five minutes is plenty fresh.
_PR_TTL_SECONDS = 300

_PR_FIELDS = "number,headRefName,state,reviewDecision,latestReviews,isDraft,url"

# state -> (icon, rich style). `●` matches the Live column's glyph; `commented`
# and `changes_requested` share `◐` and differ only in color. Settled states
# (merged/closed/draft) go dim — the eye should land on what still needs work.
PR_STATE_DISPLAY: dict[str, tuple[str, str]] = {
    "merged": ("✔", "dim"),
    "closed": ("✕", "dim"),
    "changes_requested": ("◐", "red"),
    "approved": ("●", "green"),
    "commented": ("◐", "yellow"),
    "open": ("○", "blue"),
    "draft": ("○", "dim"),
}


@dataclass
class PRInfo:
    number: int
    repo: str  # short name, e.g. "clack"
    slug: str  # "owner/name"
    state: str  # key into PR_STATE_DISPLAY
    url: str
    is_draft: bool

    @property
    def label(self) -> str:
        """Display label, e.g. "clack#234"."""
        return f"{self.repo}#{self.number}"

    @property
    def display(self) -> tuple[str, str, str]:
        """(icon, label, style) for rendering."""
        icon, style = PR_STATE_DISPLAY.get(self.state, PR_STATE_DISPLAY["open"])
        return icon, self.label, style


@lru_cache(maxsize=1)
def _gh_bin() -> str | None:
    """Resolve the gh binary. Cached for the process lifetime."""
    return shutil.which("gh")


def _run(args: list[str], cwd: str | None = None, timeout: int = 20) -> str | None:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


@lru_cache(maxsize=256)
def _repo_root(cwd: str) -> str | None:
    """The git toplevel for cwd, or None if it isn't a checkout."""
    if not shutil.which("git"):
        return None
    return _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], timeout=5) or None


@lru_cache(maxsize=256)
def _repo_slug(root: str) -> str | None:
    """The "owner/name" slug for a repo root, via gh."""
    gh = _gh_bin()
    if not gh:
        return None
    out = _run(
        [gh, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=root,
    )
    return out or None


def _derive_state(pr: dict) -> str:
    """Collapse gh's state/reviewDecision/latestReviews into one display state."""
    state = (pr.get("state") or "").upper()
    if state == "MERGED":
        return "merged"
    if state == "CLOSED":
        return "closed"

    decision = (pr.get("reviewDecision") or "").upper()
    if decision == "CHANGES_REQUESTED":
        return "changes_requested"
    if decision == "APPROVED":
        return "approved"

    reviews = pr.get("latestReviews") or []
    if any(
        (r.get("state") or "").upper() == "COMMENTED"
        for r in reviews
        if isinstance(r, dict)
    ):
        return "commented"

    return "draft" if pr.get("isDraft") else "open"


# slug -> (fetched_at, {headRefName: PRInfo}). Read and written from several
# worker threads; dict get/set is atomic, and the worst race is two worktrees of
# the same repo both missing the cache and fetching it once each.
_pr_cache: dict[str, tuple[float, dict[str, PRInfo]]] = {}


def _prs_for_repo(root: str, slug: str) -> dict[str, PRInfo]:
    """All PRs for a repo, keyed by head branch. Cached with a TTL."""
    cached = _pr_cache.get(slug)
    now = time.monotonic()
    if cached and now - cached[0] < _PR_TTL_SECONDS:
        return cached[1]

    gh = _gh_bin()
    if not gh:
        return {}
    out = _run(
        [gh, "pr", "list", "--state", "all", "--limit", "200", "--json", _PR_FIELDS],
        cwd=root,
    )
    if not out:
        return {}
    try:
        raw = json.loads(out)
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, list):
        return {}

    short = slug.split("/")[-1]
    by_branch: dict[str, PRInfo] = {}
    for pr in raw:
        if not isinstance(pr, dict):
            continue
        branch = pr.get("headRefName")
        number = pr.get("number")
        if not branch or not isinstance(number, int):
            continue
        info = PRInfo(
            number=number,
            repo=short,
            slug=slug,
            state=_derive_state(pr),
            url=pr.get("url") or "",
            is_draft=bool(pr.get("isDraft")),
        )
        # gh lists newest first; keep the newest PR per branch.
        by_branch.setdefault(branch, info)

    _pr_cache[slug] = (now, by_branch)
    return by_branch


def _map_one_repo(root: str, group: list[SessionSummary]) -> dict[str, PRInfo]:
    """session_id -> PRInfo for the sessions living in one repo root."""
    slug = _repo_slug(root)
    if not slug:
        return {}
    prs = _prs_for_repo(root, slug)
    if not prs:
        return {}
    return {
        s.session_id: prs[s.git_branch]
        for s in group
        if s.git_branch and s.git_branch in prs
    }


def build_pr_map(
    sessions: list[SessionSummary],
    on_partial: Callable[[dict[str, PRInfo]], None] | None = None,
    max_workers: int = 6,
) -> dict[str, PRInfo]:
    """Map session_id -> PRInfo for every session whose branch has a PR.

    Blocking (shells out to git and gh); call from a worker thread.

    A session list spanning many repos means many round trips to GitHub, so
    repos are fetched concurrently and `on_partial` — if given — is called with
    each repo's results as they land, letting the UI fill in progressively
    instead of waiting on the slowest repo. `on_partial` runs on a worker
    thread.
    """
    if not _gh_bin():
        return {}

    # Group sessions by repo root so each repo is fetched once. `git rev-parse`
    # is local and cached, so this stays on the calling thread.
    by_root: dict[str, list[SessionSummary]] = {}
    for s in sessions:
        if not s.cwd or not s.git_branch:
            continue
        root = _repo_root(s.cwd)
        if root:
            by_root.setdefault(root, []).append(s)

    result: dict[str, PRInfo] = {}
    if not by_root:
        return result

    with ThreadPoolExecutor(max_workers=min(max_workers, len(by_root))) as pool:
        futures = [
            pool.submit(_map_one_repo, root, group) for root, group in by_root.items()
        ]
        for future in as_completed(futures):
            try:
                partial = future.result()
            except Exception:
                continue
            if not partial:
                continue
            result.update(partial)
            if on_partial is not None:
                on_partial(partial)
    return result


def open_pr_in_browser(pr: PRInfo) -> bool:
    """Open a PR in the default browser via `gh pr view -w`. Non-blocking."""
    gh = _gh_bin()
    if not gh:
        return False
    try:
        subprocess.Popen(
            [gh, "pr", "view", str(pr.number), "-w", "-R", pr.slug],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True
