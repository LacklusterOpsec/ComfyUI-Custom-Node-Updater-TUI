"""TUI for finding outdated ComfyUI custom-node repos and pulling them safely.

Scans a directory of git repositories, fetches remotes in parallel, and shows
which checkouts are behind the branch that actually tracks upstream. Pulls are
fast-forward only: a repo with its own commits, a dirty tree, or a detached HEAD
is skipped rather than clobbered. Dirty repos can be pulled with a temporary
stash when you opt in.

Usage:
    <python> -X utf8 ComfyUI-Custom-Node-Updater-TUI.py [--nodes-dir DIR] [--no-fetch] [--no-autostash] [--self-test]

    --nodes-dir     Directory of git repos to scan (default: $COMFYUI_CUSTOM_NODES,
                    then the folder chosen last time, then $COMFYUI_DIR, then a
                    custom_nodes directory found by walking up from the script.
                    If none is found, the TUI asks for one.)
    --no-fetch      Compare against local refs only; skip the network fetch
    --no-autostash  Never stash local edits; skip dirty repos instead
    --self-test     Build throwaway repos, run a headless scan and pull, then exit

In the TUI, press `o` to pick a different custom_nodes folder; the choice is
remembered in a small config file for next time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    ProgressBar,
    RichLog,
    Static,
)

GIT_TIMEOUT = 60
FETCH_TIMEOUT = 180
MAX_DETAIL_COMMITS = 15
MAX_FETCH_THREADS = 8
THEMES = ["catppuccin-mocha", "catppuccin-macchiato", "tokyo-night", "gruvbox", "nord", "textual-dark"]

GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "echo",
    "GCM_INTERACTIVE": "never",
    "GIT_OPTIONAL_LOCKS": "0",
}

# Column layout: key, header, width. The cell order in Repo.cells must match.
COLUMNS = [
    ("sel", "", 2),
    ("repo", "Repo", 30),
    ("branch", "Branch", 14),
    ("behind", "Behind", 6),
    ("ahead", "Ahead", 5),
    ("local", "Local", 6),
    ("target", "Tracks", 15),
]

STATE_STYLE = {
    "pending": "dim",
    "error": "bold red",
    "behind": "bold red",
    "diverged": "bold magenta",
    "ahead": "yellow",
    "modified": "yellow",
    "current": "green",
    "pulled": "bold cyan",
}


def config_path() -> Path:
    """Where the remembered custom_nodes folder lives."""
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "ComfyUI-Custom-Node-Updater-TUI" / "config.json"


def load_saved_nodes_dir() -> Path | None:
    try:
        saved = json.loads(config_path().read_text(encoding="utf-8")).get("nodes_dir")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(saved, str) and Path(saved).is_dir():
        return Path(saved)
    return None


def save_nodes_dir(path: Path) -> None:
    target = config_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"nodes_dir": str(path)}, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def find_custom_nodes(start: Path) -> Path | None:
    """Walk up from start looking for a ComfyUI-style custom_nodes directory."""
    if start.name == "custom_nodes" and start.is_dir():
        return start.resolve()
    for parent in start.parents:
        cand = parent / "custom_nodes"
        if cand.is_dir():
            return cand.resolve()
    return None


def resolve_nodes_dir(candidate: Path | None) -> Path | None:
    """Locate the directory of repos, or None when the TUI should ask.

    Order: explicit --nodes-dir, $COMFYUI_CUSTOM_NODES, the folder chosen last
    time, $COMFYUI_DIR, then a custom_nodes found by walking up from the working
    directory and the script. None means nothing was found on this machine yet.
    """
    if candidate is not None:
        return Path(candidate).expanduser().resolve()
    env = os.environ.get("COMFYUI_CUSTOM_NODES")
    if env:
        return Path(env).expanduser().resolve()
    env = os.environ.get("COMFYUI_DIR")
    if env:
        path = Path(env).expanduser()
        if path.name != "custom_nodes" and (path / "custom_nodes").is_dir():
            path = path / "custom_nodes"
        if path.is_dir():
            return path.resolve()
    saved = load_saved_nodes_dir()
    if saved is not None:
        return saved
    return find_custom_nodes(Path.cwd()) or find_custom_nodes(Path(__file__).resolve().parent)


def git(path: Path, *args: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess[str] | None:
    """Run git in a repo without ever prompting for credentials. None means timeout."""
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=GIT_ENV,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_out(path: Path, *args: str, timeout: int = GIT_TIMEOUT) -> str | None:
    proc = git(path, *args, timeout=timeout)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip()


def git_lines(path: Path, *args: str, timeout: int = GIT_TIMEOUT) -> list[str]:
    """Like git_out, but keeps per-line leading spaces (needed for --porcelain)."""
    proc = git(path, *args, timeout=timeout)
    if proc is None or proc.returncode != 0:
        return []
    return [line.rstrip("\r") for line in proc.stdout.split("\n") if line.rstrip("\r")]


def ref_exists(path: Path, ref: str) -> bool:
    proc = git(path, "rev-parse", "--verify", "--quiet", ref)
    return proc is not None and proc.returncode == 0

@dataclass
class Repo:
    path: Path
    name: str
    branch: str | None = None
    head: str | None = None
    remotes: list[str] = field(default_factory=list)
    tracking: str | None = None
    target: str | None = None
    ahead: int = 0
    behind: int = 0
    changes: list[str] = field(default_factory=list)
    untracked: int = 0
    pulled: bool = False
    scanned: bool = False
    error: str | None = None

    @property
    def dirty(self) -> bool:
        return bool(self.changes)

    @property
    def is_fork(self) -> bool:
        return "upstream" in self.remotes

    @property
    def detached(self) -> bool:
        return self.branch is None and self.head is not None

    @property
    def state(self) -> str:
        if self.error:
            return "error"
        if not self.scanned:
            return "pending"
        if self.pulled:
            return "pulled"
        if self.behind and not self.ahead:
            return "behind"
        if self.ahead and self.behind:
            return "diverged"
        if self.ahead:
            return "ahead"
        if self.dirty:
            return "modified"
        return "current"

    def cells(self, selected: bool) -> list[Text]:
        style = STATE_STYLE.get(self.state, "")
        sel = Text("\u25cf" if selected else "\u25cb", style="bold cyan" if selected else "dim")
        repo = Text(self.name, style=style)
        branch = Text(self.branch or "(detached)", style="dim")
        behind = Text(str(self.behind) if self.behind else "", style="bold red")
        ahead = Text(str(self.ahead) if self.ahead else "", style="bold yellow")
        dirty_count = len(self.changes) + self.untracked
        local = Text(str(dirty_count) if dirty_count else "clean", style="yellow" if dirty_count else "dim")
        target = Text(self.target or "\u2014", style="cyan" if self.is_fork else "dim")
        return [sel, repo, branch, behind, ahead, local, target]


def inspect_repo(repo: Repo) -> None:
    """Refresh local (non-network) facts about a repo."""
    out = git_out(repo.path, "remote") or ""
    repo.remotes = [line for line in out.splitlines() if line]
    repo.branch = git_out(repo.path, "symbolic-ref", "-q", "--short", "HEAD")
    repo.head = git_out(repo.path, "rev-parse", "--short", "HEAD")
    repo.tracking = git_out(repo.path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    lines = git_lines(repo.path, "status", "--porcelain")
    repo.changes = [line for line in lines if not line.startswith("??")]
    repo.untracked = sum(1 for line in lines if line.startswith("??"))
    repo.target = resolve_target(repo)
    repo.scanned = True


def resolve_target(repo: Repo) -> str | None:
    """Pick the ref that best represents 'upstream' for this checkout.

    A repo with a separate `upstream` remote is a fork: compare against
    upstream so fork staleness shows up. Otherwise use the branch's own
    tracking ref, falling back to origin.
    """
    path = repo.path
    if repo.is_fork:
        candidates = []
        if repo.branch:
            candidates.append(f"upstream/{repo.branch}")
        head = git_out(path, "symbolic-ref", "-q", "--short", "refs/remotes/upstream/HEAD")
        if head:
            candidates.append(head)
        candidates += ["upstream/main", "upstream/master"]
        for cand in candidates:
            if ref_exists(path, cand):
                return cand
    if repo.tracking and ref_exists(path, repo.tracking):
        return repo.tracking
    if repo.branch and ref_exists(path, f"origin/{repo.branch}"):
        return f"origin/{repo.branch}"
    return None


def update_counts(repo: Repo) -> None:
    """Set ahead/behind against repo.target. Left count is ahead, right is behind."""
    if not repo.target:
        repo.ahead = repo.behind = 0
        return
    out = git_out(repo.path, "rev-list", "--left-right", "--count", f"HEAD...{repo.target}")
    if not out:
        repo.error = f"cannot compare with {repo.target}"
        return
    ahead, behind = (int(value) for value in out.split())
    repo.ahead, repo.behind = ahead, behind


def remote_fetch(repo: Repo) -> None:
    if repo.target:
        remote = repo.target.split("/", 1)[0]
    elif "origin" in repo.remotes:
        remote = "origin"
    elif repo.remotes:
        remote = repo.remotes[0]
    else:
        repo.error = "no remotes"
        return
    proc = git(repo.path, "fetch", "--quiet", "--prune", "--no-tags", remote, timeout=FETCH_TIMEOUT)
    if proc is None:
        repo.error = f"fetch timed out ({remote})"
        return
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        repo.error = f"fetch failed: {lines[-1] if lines else remote}"
        return


def overlap_with_incoming(repo: Repo) -> bool:
    """True when local work touches a path the incoming commits also change.

    Stashing and popping those would leave conflict markers in the working
    tree, so they are skipped instead of half-applied.
    """
    if not repo.target or not repo.dirty:
        return False
    incoming = set(git_lines(repo.path, "diff", "--name-only", f"HEAD..{repo.target}"))
    if not incoming:
        return False
    touched = {line[3:] for line in repo.changes}
    if touched & incoming:
        return True
    if repo.untracked:
        added = set(git_lines(repo.path, "diff", "--name-only", "--diff-filter=A", f"HEAD..{repo.target}"))
        untracked = {line[3:] for line in git_lines(repo.path, "status", "--porcelain") if line.startswith("??")}
        if untracked & added:
            return True
    return False


def pull_reason(repo: Repo, autostash: bool) -> str | None:
    """Why this repo cannot be fast-forwarded, or None when it is safe."""
    if repo.error:
        return "error"
    if not repo.scanned:
        return "not scanned"
    if repo.detached:
        return "detached HEAD"
    if not repo.target:
        return "no upstream"
    if repo.behind == 0:
        return "up to date"
    if repo.ahead:
        return "local commits (diverged)"
    if repo.dirty:
        if not autostash:
            return "local changes"
        if overlap_with_incoming(repo):
            return "edits overlap incoming changes"
    return None


def pull(repo: Repo, autostash: bool) -> tuple[bool, str]:
    """Fast-forward a repo onto its target. Never creates a merge commit."""
    reason = pull_reason(repo, autostash)
    if reason:
        return False, reason
    stashed = False
    # pull_reason() already rejects a missing target, but the type checker cannot
    # see that, and passing None through to git() would raise instead of report.
    target = repo.target
    if target is None:
        return False, "no upstream"
    if repo.dirty and autostash:
        proc = git(repo.path, "stash", "push", "--include-untracked", "-m", "ComfyUI-Custom-Node-Updater-TUI: auto-stash")
        if proc is None or proc.returncode != 0:
            return False, "stash failed"
        stashed = True
    proc = git(repo.path, "merge", "--ff-only", target)
    if proc is None:
        if stashed:
            git(repo.path, "stash", "pop")
        return False, "fast-forward timed out"
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        if stashed:
            git(repo.path, "stash", "pop")
        return False, "fast-forward failed: " + (lines[-1] if lines else target)
    if stashed:
        pop = git(repo.path, "stash", "pop")
        if pop is None or pop.returncode != 0:
            # Keep the update, drop the conflict markers, and leave the edits
            # recoverable in the stash rather than half-applied.
            git(repo.path, "reset", "--hard")
            return True, "pulled; edits kept in stash (run 'git stash pop')"
        return True, "pulled (local edits restored)"
    return True, f"pulled {repo.target}"


class ConfirmScreen(ModalScreen[bool]):
    """Show exactly what will be pulled and what will be skipped."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel", show=False),
    ]

    def __init__(self, plan: list[tuple[str, str | None]], autostash: bool, ready: int) -> None:
        super().__init__()
        self._plan = plan
        self._autostash = autostash
        self._ready = ready

    def action_cancel(self) -> None:
        self.dismiss(False)

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self._header(), id="confirm-title")
            with VerticalScroll(id="confirm-list"):
                yield Static(self._body(), id="confirm-body")
            with Horizontal(id="confirm-buttons"):
                yield Button(f"Pull {self._ready}", variant="primary", id="confirm-yes", disabled=self._ready == 0)
                yield Button("Cancel", id="confirm-no")

    def _header(self) -> str:
        stash = "stash local edits" if self._autostash else "skip repos with local edits"
        return f"[bold]Fast-forward {self._ready} repo(s)[/bold]  [dim]({stash})[/dim]"

    def _body(self) -> str:
        lines = []
        for name, reason in self._plan:
            if reason is None:
                lines.append(f"[green]pull[/green]   {escape(name)}")
            else:
                lines.append(f"[dim]skip[/dim]   {escape(name)}  [dim]- {escape(reason)}[/dim]")
        return "\n".join(lines) if lines else "[dim]nothing selected[/dim]"

    @on(Button.Pressed, "#confirm-yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def _no(self) -> None:
        self.dismiss(False)


class FolderPickerScreen(ModalScreen[Path | None]):
    """Browse for the custom_nodes folder when it cannot be found automatically."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, start: Path) -> None:
        super().__init__()
        self._start = start if start.is_dir() else Path.home()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static("[bold]Choose your custom_nodes folder[/bold]", id="picker-title")
            yield Static(
                "[dim]Browse to the folder holding your node repos, then press Use folder. "
                "Type or paste a path above the tree too.[/dim]",
                id="picker-hint",
            )
            yield Input(value=str(self._start), id="picker-path")
            yield DirectoryTree(str(self._start), id="picker-tree")
            with Horizontal(id="picker-buttons"):
                yield Button("Use folder", variant="primary", id="picker-use")
                yield Button("Cancel", id="picker-cancel")

    @on(DirectoryTree.NodeHighlighted)
    def _highlighted(self, event: DirectoryTree.NodeHighlighted) -> None:
        entry = event.node.data
        if entry is not None:
            self.query_one("#picker-path", Input).value = str(entry.path)

    @on(DirectoryTree.DirectorySelected)
    def _selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.query_one("#picker-path", Input).value = str(event.path)

    @on(Button.Pressed, "#picker-use")
    def _use(self) -> None:
        raw = self.query_one("#picker-path", Input).value.strip().strip('"')
        target = Path(raw).expanduser()
        if not raw or not target.is_dir():
            self.notify("That path is not a directory", severity="error")
            return
        self.dismiss(target.resolve())

    @on(Button.Pressed, "#picker-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class ComfyUICustomNodeUpdaterApp(App[None]):
    TITLE = "ComfyUI-Custom-Node-Updater-TUI"
    SUB_TITLE = "custom nodes"

    CSS = """
    #summary {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #progress {
        height: 1;
        padding: 0 1;
    }
    #body {
        height: 1fr;
    }
    #left {
        width: 1fr;
    }
    #filter {
        height: 3;
        border: round $panel;
    }
    #repos {
        height: 1fr;
        border: round $primary;
    }
    #side {
        width: 48;
    }
    #detail {
        height: 1fr;
        border: round $secondary;
        padding: 0 1;
    }
    #activity {
        height: 13;
        border: round $panel;
        padding: 0 1;
    }
    ConfirmScreen {
        align: center middle;
    }
    #confirm-box {
        width: 76;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #confirm-title {
        height: auto;
        padding-bottom: 1;
    }
    #confirm-list {
        height: auto;
        max-height: 18;
    }
    #confirm-body {
        height: auto;
    }
    #confirm-buttons {
        height: auto;
        padding-top: 1;
        align-horizontal: right;
    }
    #confirm-buttons Button {
        margin-left: 2;
    }
    FolderPickerScreen {
        align: center middle;
    }
    #picker-box {
        width: 90;
        height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #picker-title {
        height: auto;
    }
    #picker-hint {
        height: auto;
        padding-bottom: 1;
    }
    #picker-path {
        height: 3;
    }
    #picker-tree {
        height: 1fr;
        border: round $panel;
    }
    #picker-buttons {
        height: auto;
        padding-top: 1;
        align-horizontal: right;
    }
    #picker-buttons Button {
        margin-left: 2;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "refresh", "Refresh"),
        Binding("space", "toggle_select", "Select"),
        Binding("a", "select_behind", "Select outdated"),
        Binding("u", "clear_selection", "Clear"),
        Binding("p", "pull", "Pull"),
        Binding("s", "toggle_autostash", "Autostash"),
        Binding("f", "toggle_fetch", "Fetch"),
        Binding("o", "choose_folder", "Folder"),
        Binding("t", "cycle_theme", "Theme"),
        Binding("ctrl+f", "focus_filter", "Filter"),
        Binding("escape", "focus_table", "Repos", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, nodes_dir: Path | None = None, fetch_enabled: bool = True, autostash: bool = True) -> None:
        super().__init__()
        self._nodes_dir = resolve_nodes_dir(nodes_dir)
        self._fetch_enabled = fetch_enabled
        self._autostash = autostash
        self._repos: list[Repo] = []
        self._by_name: dict[str, Repo] = {}
        self._selected: set[str] = set()
        self._visible: list[str] = []
        self._highlighted: str | None = None
        self._scanning = False
        self._pulling = False
        self._pending_pull: list[str] = []
        self._filter_text = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="summary")
        yield ProgressBar(id="progress", show_eta=False)
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Input(placeholder="filter repos...", id="filter")
                yield DataTable(id="repos", cursor_type="row", zebra_stripes=True, show_row_labels=False)
            with Vertical(id="side"):
                with VerticalScroll(id="detail"):
                    yield Static("", id="detail-body")
                yield RichLog(id="activity", markup=True, max_lines=400, wrap=True, min_width=24)
        yield Footer()

    def on_mount(self) -> None:
        self.theme = THEMES[0]
        table = self.query_one("#repos", DataTable)
        for key, label, width in COLUMNS:
            table.add_column(label, key=key, width=width)
        table.cursor_background_priority = "css"
        table.cursor_foreground_priority = "renderable"
        self._progress = self.query_one("#progress", ProgressBar)
        self._progress.display = False
        table.border_title = "Repositories"
        self.query_one("#detail", VerticalScroll).border_title = "Details"
        self.query_one("#activity", RichLog).border_title = "Activity"
        table.focus()
        if self._nodes_dir is None or not self._nodes_dir.is_dir():
            self.sub_title = "choose a folder"
            self._log("no custom_nodes folder found - [bold]press o[/bold] or pick one below")
            self._prompt_for_folder()
        else:
            self._activate(self._nodes_dir, remember=False)

    def _prompt_for_folder(self) -> None:
        start = Path.cwd()
        self.push_screen(FolderPickerScreen(start), callback=self._folder_chosen)

    def _folder_chosen(self, chosen: Path | None) -> None:
        if chosen is None:
            self.notify("No folder chosen - press o to try again", severity="warning")
            return
        self._activate(chosen, remember=True)

    def _activate(self, nodes_dir: Path, remember: bool) -> None:
        """Point the app at a custom_nodes directory and scan it."""
        self._nodes_dir = nodes_dir
        self._selected.clear()
        self._highlighted = None
        self.sub_title = str(nodes_dir)
        if remember:
            save_nodes_dir(nodes_dir)
        self._discover()
        self._rebuild_table()
        self._update_summary()
        self._update_detail()
        self._log(f"scanning [bold]{escape(str(nodes_dir))}[/bold]")
        self._start_scan(fetch=self._fetch_enabled)

    # -- discovery and rendering ------------------------------------------------

    def _discover(self) -> None:
        self._repos = []
        if self._nodes_dir is None:
            self._by_name = {}
            return
        for entry in sorted(self._nodes_dir.iterdir()):
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            if not (entry / ".git").exists():
                continue
            self._repos.append(Repo(path=entry, name=entry.name))
        self._by_name = {repo.name: repo for repo in self._repos}

    def _matches(self, repo: Repo) -> bool:
        if not self._filter_text:
            return True
        return self._filter_text in repo.name.lower()

    def _rebuild_table(self) -> None:
        table = self.query_one("#repos", DataTable)
        table.clear()
        self._visible = []
        for repo in self._repos:
            if not self._matches(repo):
                continue
            table.add_row(*repo.cells(repo.name in self._selected), key=repo.name)
            self._visible.append(repo.name)

    def _update_row(self, repo: Repo) -> None:
        if repo.name not in self._visible:
            return
        table = self.query_one("#repos", DataTable)
        for cell, (key, _, _) in zip(repo.cells(repo.name in self._selected), COLUMNS, strict=False):
            table.update_cell(repo.name, key, cell)

    def _update_summary(self) -> None:
        total = len(self._repos)
        behind = sum(1 for r in self._repos if r.state == "behind")
        diverged = sum(1 for r in self._repos if r.state == "diverged")
        modified = sum(1 for r in self._repos if r.changes or r.untracked)
        forks = sum(1 for r in self._repos if r.is_fork)
        selected = len(self._selected)
        bits = [
            f"[bold]{total}[/bold] repos",
            f"[red]{behind}[/red] behind",
            f"[magenta]{diverged}[/magenta] diverged",
            f"[yellow]{modified}[/yellow] modified",
            f"[cyan]{forks}[/cyan] forks",
            f"[bold cyan]{selected}[/bold cyan] selected",
            f"autostash {'on' if self._autostash else 'off'}",
            f"fetch {'on' if self._fetch_enabled else 'off'}",
        ]
        self.query_one("#summary", Static).update("  -  ".join(bits))

    def _update_detail(self) -> None:
        repo = self._by_name.get(self._highlighted or "")
        body = self.query_one("#detail-body", Static)
        if repo is None:
            body.update("[dim]Select a repo to see its status and incoming commits.[/dim]")
            return
        body.update(self._detail_markup(repo))

    def _detail_markup(self, repo: Repo) -> str:
        lines = [
            f"[bold]{escape(repo.name)}[/bold]",
            f"[dim]state[/dim]   {escape(repo.state)}",
            f"[dim]branch[/dim]  {escape(repo.branch or '(detached)')}  [dim]head {escape(repo.head or '?')}[/dim]",
            f"[dim]tracks[/dim]  {escape(repo.target or '-')}"
            + ("  [cyan](fork -> upstream)[/cyan]" if repo.is_fork else ""),
            f"[dim]remote[/dim]  {escape(', '.join(repo.remotes) or 'none')}",
            f"[dim]local[/dim]   {len(repo.changes)} changed, {repo.untracked} untracked",
        ]
        if repo.error:
            lines.append(f"[red]{escape(repo.error)}[/red]")
        if not repo.scanned:
            lines.append("[dim]scanning...[/dim]")
            return "\n".join(lines)
        if repo.ahead:
            lines.append("")
            lines.append(f"[yellow]your commits ({repo.ahead})[/yellow]")
            lines.extend(self._commit_lines(repo, f"{repo.target}..HEAD", "yellow"))
        if repo.behind:
            lines.append("")
            lines.append(f"[red]incoming commits ({repo.behind})[/red]")
            lines.extend(self._commit_lines(repo, f"HEAD..{repo.target}", "red"))
        if repo.changes:
            lines.append("")
            lines.append("[yellow]changed files[/yellow]")
            lines.extend(f"  [dim]{escape(line)}[/dim]" for line in repo.changes[:MAX_DETAIL_COMMITS])
        return "\n".join(lines)

    def _commit_lines(self, repo: Repo, range_spec: str, style: str) -> list[str]:
        out = git_out(
            repo.path,
            "log",
            f"--max-count={MAX_DETAIL_COMMITS}",
            "--no-decorate",
            "--oneline",
            range_spec,
        )
        if not out:
            return ["  [dim](unavailable)[/dim]"]
        return [f"  [{style}]{escape(line)}[/{style}]" for line in out.splitlines()]

    def _log(self, markup: str) -> None:
        self.query_one("#activity", RichLog).write(f"[dim]{self._stamp()}[/dim] {markup}")

    @staticmethod
    def _stamp() -> str:
        return datetime.now().strftime("%H:%M:%S")
    # -- scanning ---------------------------------------------------------------

    def _start_scan(self, fetch: bool) -> None:
        if self._nodes_dir is None:
            self.notify("Choose a custom_nodes folder first (press o)", severity="warning")
            return
        if self._scanning:
            self.notify("Scan already running", severity="warning")
            return
        for repo in self._repos:
            repo.pulled = False
            repo.error = None
            repo.scanned = False
        self._scanning = True
        self._progress.display = True
        self._progress.update(total=max(len(self._repos), 1), progress=0)
        self.scan(fetch)

    @work(thread=True, exclusive=True, group="scan")
    def scan(self, fetch: bool) -> None:
        repos = list(self._repos)
        with ThreadPoolExecutor(max_workers=min(MAX_FETCH_THREADS, max(len(repos), 1))) as pool:
            futures = {pool.submit(self._scan_one, repo, fetch): repo for repo in repos}
            for done, future in enumerate(as_completed(futures), start=1):
                repo = futures[future]
                try:
                    future.result()
                except Exception as exc:  # keep one bad repo from killing the scan
                    repo.error = f"scan error: {exc}"
                self.call_from_thread(self._scan_one_done, repo, done, len(repos))
        self.call_from_thread(self._end_scan)

    def _scan_one(self, repo: Repo, fetch: bool) -> None:
        inspect_repo(repo)
        if fetch:
            remote_fetch(repo)
        repo.target = resolve_target(repo)
        update_counts(repo)

    def _scan_one_done(self, repo: Repo, done: int, total: int) -> None:
        self._update_row(repo)
        if self._highlighted == repo.name:
            self._update_detail()
        self._progress.update(progress=done, total=max(total, 1))

    def _end_scan(self) -> None:
        self._scanning = False
        self._progress.display = False
        if not isinstance(self.focused, Input):
            self.query_one("#repos", DataTable).focus()
        self._update_summary()
        self._update_detail()
        behind = sum(1 for r in self._repos if r.state == "behind")
        errors = sum(1 for r in self._repos if r.state == "error")
        self._log(f"scan complete: [red]{behind}[/red] behind, [red]{errors}[/red] errors")
        self.notify(f"{behind} repo(s) behind upstream")

    # -- selection and pulling --------------------------------------------------

    def action_toggle_select(self) -> None:
        name = self._highlighted
        if not name:
            return
        if name in self._selected:
            self._selected.discard(name)
        else:
            self._selected.add(name)
        self._update_row(self._by_name[name])
        self._update_summary()

    def action_select_behind(self) -> None:
        for repo in self._repos:
            if repo.state == "behind":
                self._selected.add(repo.name)
        for name in self._visible:
            self._update_row(self._by_name[name])
        self._update_summary()
        self.notify(f"Selected {len(self._selected)} repo(s)")

    def action_clear_selection(self) -> None:
        self._selected.clear()
        for name in self._visible:
            self._update_row(self._by_name[name])
        self._update_summary()

    def action_pull(self) -> None:
        if self._pulling:
            self.notify("Pull already running", severity="warning")
            return
        if self._scanning:
            self.notify("Wait for the scan to finish", severity="warning")
            return
        names = [name for name in self._visible if name in self._selected]
        if not names:
            self.notify("Nothing selected", severity="warning")
            return
        plan = [(name, pull_reason(self._by_name[name], self._autostash)) for name in names]
        ready = [name for name, reason in plan if reason is None]
        self._pending_pull = ready
        self.push_screen(ConfirmScreen(plan, self._autostash, len(ready)), callback=self._confirmed)

    def _confirmed(self, confirmed: bool | None) -> None:
        if not confirmed or not self._pending_pull:
            return
        names = self._pending_pull
        self._pulling = True
        self._progress.display = True
        self._progress.update(total=len(names), progress=0)
        self.pull_many(names)

    @work(thread=True, exclusive=True, group="pull")
    def pull_many(self, names: list[str]) -> None:
        for index, name in enumerate(names, start=1):
            repo = self._by_name[name]
            ok, message = pull(repo, self._autostash)
            if ok:
                repo.pulled = True
                inspect_repo(repo)
                repo.target = resolve_target(repo)
                update_counts(repo)
            self.call_from_thread(self._pull_one_done, repo, ok, message, index, len(names))
        self.call_from_thread(self._end_pull)

    def _pull_one_done(self, repo: Repo, ok: bool, message: str, done: int, total: int) -> None:
        self._update_row(repo)
        if self._highlighted == repo.name:
            self._update_detail()
        color = "green" if ok else "yellow"
        self._log(f"[{color}]{'ok  ' if ok else 'skip'}[/{color}] {escape(repo.name)} - {escape(message)}")
        self._progress.update(progress=done, total=max(total, 1))

    def _end_pull(self) -> None:
        self._pulling = False
        self._progress.display = False
        pulled = sum(1 for r in self._repos if r.pulled)
        self._update_summary()
        self.notify(f"Pull complete - {pulled} repo(s) updated", severity="success")

    # -- events -----------------------------------------------------------------

    @on(DataTable.RowHighlighted, "#repos")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is None:
            return
        self._highlighted = str(event.row_key.value)
        self._update_detail()

    @on(DataTable.RowSelected, "#repos")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is None:
            return
        self._highlighted = str(event.row_key.value)
        self.action_toggle_select()

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self._filter_text = event.value.strip().lower()
        self._rebuild_table()
        table = self.query_one("#repos", DataTable)
        if self._visible:
            self._highlighted = self._visible[0]
            table.move_cursor(row=0)
        self._update_detail()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_focus_table(self) -> None:
        self.query_one("#filter", Input).blur()
        self.query_one("#repos", DataTable).focus()

    def action_refresh(self) -> None:
        self._start_scan(fetch=self._fetch_enabled)

    def action_choose_folder(self) -> None:
        if self._scanning:
            self.notify("Wait for the scan to finish", severity="warning")
            return
        start = self._nodes_dir or Path.cwd()
        self.push_screen(FolderPickerScreen(start), callback=self._folder_chosen)

    def action_toggle_autostash(self) -> None:
        self._autostash = not self._autostash
        self._update_summary()
        self.notify(f"Autostash {'enabled' if self._autostash else 'disabled'}")

    def action_toggle_fetch(self) -> None:
        self._fetch_enabled = not self._fetch_enabled
        self._update_summary()
        self.notify(f"Fetch {'enabled' if self._fetch_enabled else 'disabled'} - press r to rescan")

    def action_cycle_theme(self) -> None:
        index = (THEMES.index(self.theme) + 1) % len(THEMES) if self.theme in THEMES else 1
        self.theme = THEMES[index]
        self.notify(f"Theme: {self.theme}")

def self_test() -> None:
    """Build throwaway repos on disk and exercise scan + pull without a network."""

    def run(cwd: Path, *args: str) -> None:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=GIT_ENV,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit(f"git {args} failed: {proc.stderr or proc.stdout}")

    def commit(cwd: Path, message: str) -> None:
        run(cwd, "-c", "user.email=t@example.com", "-c", "user.name=test", "add", "-A")
        run(cwd, "-c", "user.email=t@example.com", "-c", "user.name=test", "commit", "-m", message)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        nodes = tmp / "nodes"
        nodes.mkdir()
        origin = tmp / "origin.git"
        run(tmp, "init", "--bare", "-b", "main", str(origin))

        seed = tmp / "seed"
        run(tmp, "clone", str(origin), str(seed))
        (seed / "README.md").write_text("v1\n", encoding="utf-8")
        (seed / "base.txt").write_text("base\n", encoding="utf-8")
        commit(seed, "initial")
        run(seed, "push", "-u", "origin", "main")

        demo = nodes / "demo"
        run(tmp, "clone", str(origin), str(demo))

        overlap = nodes / "overlap"
        run(tmp, "clone", str(origin), str(overlap))

        diverged = nodes / "diverged"
        run(tmp, "clone", str(origin), str(diverged))
        (diverged / "local.txt").write_text("mine\n", encoding="utf-8")
        commit(diverged, "my local work")

        # Upstream touches CHANGELOG.md and README.md only.
        (seed / "CHANGELOG.md").write_text("changes\n", encoding="utf-8")
        (seed / "README.md").write_text("v1\nupstream edit\n", encoding="utf-8")
        commit(seed, "upstream update")
        run(seed, "push")

        (demo / "base.txt").write_text("base\nlocal edit\n", encoding="utf-8")  # no overlap
        (overlap / "README.md").write_text("v1\nlocal edit\n", encoding="utf-8")  # overlaps README

        app = ComfyUICustomNodeUpdaterApp(nodes, fetch_enabled=True, autostash=True)

        async def exercise() -> None:
            async with app.run_test(size=(150, 45)) as pilot:
                for _ in range(300):
                    await pilot.pause(0.1)
                    if not app._scanning:
                        break
                demo_repo = app._by_name["demo"]
                overlap_repo = app._by_name["overlap"]
                diverged_repo = app._by_name["diverged"]
                assert demo_repo.behind == 1, f"expected demo behind by 1, got {demo_repo.behind}"
                assert overlap_repo.behind == 1, f"expected overlap behind by 1, got {overlap_repo.behind}"
                assert diverged_repo.ahead == 1, f"expected diverged ahead by 1, got {diverged_repo.ahead}"
                assert pull_reason(diverged_repo, True) == "local commits (diverged)"
                assert pull_reason(demo_repo, True) is None, "autostash should make a dirty repo pullable"
                assert pull_reason(demo_repo, False) == "local changes", "without autostash a dirty repo is skipped"
                assert pull_reason(overlap_repo, True) == "edits overlap incoming changes"

                app._pending_pull = ["demo"]
                app._confirmed(True)
                for _ in range(300):
                    await pilot.pause(0.1)
                    if not app._pulling:
                        break
                assert demo_repo.behind == 0, f"expected demo up to date, got {demo_repo.behind}"
                assert demo_repo.pulled, "demo should be marked pulled"
                assert "local edit" in (demo / "base.txt").read_text(encoding="utf-8")
                assert overlap_repo.behind == 1 and not overlap_repo.pulled, "overlapping repo must be untouched"
                assert "local edit" in (overlap / "README.md").read_text(encoding="utf-8")
                assert diverged_repo.ahead == 1 and not diverged_repo.pulled, "diverged repo must be left untouched"

                print(
                    "OK: scanned 4 repos, pulled demo (kept local edits), "
                    f"skipped overlap ({pull_reason(overlap_repo, True)}) "
                    f"and diverged ({pull_reason(diverged_repo, True)})"
                )

        asyncio.run(exercise())


def main() -> None:
    parser = argparse.ArgumentParser(description="ComfyUI-Custom-Node-Updater-TUI")
    parser.add_argument("--nodes-dir", type=Path, default=None, help="Directory of git repos to scan")
    parser.add_argument("--no-fetch", action="store_true", help="Compare against local refs only")
    parser.add_argument("--no-autostash", action="store_true", help="Skip dirty repos instead of stashing")
    parser.add_argument("--self-test", action="store_true", help="Run headless self-tests and exit")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    app = ComfyUICustomNodeUpdaterApp(
        nodes_dir=args.nodes_dir,
        fetch_enabled=not args.no_fetch,
        autostash=not args.no_autostash,
    )
    app.run()


if __name__ == "__main__":
    main()
