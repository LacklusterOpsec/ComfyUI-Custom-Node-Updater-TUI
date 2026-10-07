# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-10-07

### Fixed

- **The TUI now notices terminal resizes on Windows.** Textual's Windows driver puts
  stdin into virtual-terminal input mode, which stops `WINDOW_BUFFER_SIZE_EVENT` from
  reaching the app, so it kept painting at the old size and never grew to fill the
  window. The app now polls the OS terminal size and reposts a resize event when it
  changes, which is how Windows Terminal resizes are picked up.
- **The interface now fills the terminal when it is resized.** The repository table's
  `Repo` column stretches to consume the width left over by the fixed columns instead of
  leaving a blank gutter, and the detail/activity pane scales with the window (38% wide,
  clamped to 36-72 cells) instead of being pinned at 48 cells. Previously the layout
  reflowed on resize but the table columns kept their hard-coded widths, so a wider
  terminal showed empty space.

## [0.2.0] - 2026-10-05

### Added

- **PowerShell launcher** (`ComfyUI-Custom-Node-Updater-TUI.ps1`). Mirrors the batch
  launcher's interpreter discovery for PowerShell users: it checks `$env:COMFYUI_PYTHON`,
  then searches `.venv`, `venv`, `env`, `.venv132`, and `venv132` for `Scripts\python.exe`
  under `COMFYUI_DIR` (default: two levels above the script), the script's parent
  directory, and the script folder, then falls back to `python` on `PATH`. Arguments are
  forwarded and the exit code is propagated.
- **POSIX shell launcher** (`ComfyUI-Custom-Node-Updater-TUI.sh`) for macOS and Linux.
  Uses the same discovery order but looks for `bin/python` inside each venv candidate,
  and falls back to `python3` then `python`. Resolves its own directory (following
  symlinks) and `exec`s the script so signals and exit codes pass through cleanly.
- **`.gitattributes`** to normalize line endings per platform: LF for `*.sh` and `*.py`,
  CRLF for `*.bat` and `*.ps1`. Without this, a Windows checkout with `core.autocrlf=true`
  would give the shell launcher CRLF endings and break it on Linux with a
  `bad interpreter` error.
- **`ruff.toml`** lint configuration: `target-version = "py310"`, a 120-character line
  limit, and a curated rule set (`E4`, `E7`, `E9`, `F`, `W`, `I`, `UP`, `B`, `C4`, `SIM`,
  `RUF`) with `SIM105` ignored.

### Changed

- **README** now documents all three launchers under a single "Launcher scripts" section
  (Windows batch/PowerShell and macOS/Linux shell), and the Requirements, Quick start,
  and `COMFYUI_PYTHON` references were updated to match. The old "Launcher script
  (Windows)" and "Running on macOS and Linux" sections were merged.
- **`.gitignore`** now covers sibling virtual environments (`.venv*/`, `venv*/`, `env*/`)
  in addition to the bare `.venv/`, so ComfyUI installs that ship `.venv132`/`venv132`
  are ignored. Also ignores the pytest and coverage caches (`.pytest_cache/`,
  `.coverage`, `htmlcov/`) and local logs and scratch files (`*.log`, `*.tmp`).
- **`pull()`** now reads `repo.target` into a local and returns `"no upstream"` early
  when it is `None`, instead of passing a literal `None` into the `git merge --ff-only`
  argument list. `pull_reason()` already rejected a missing target, so behaviour is
  unchanged for any repo with an upstream; this makes the invariant local and satisfies
  the type checker.
- **`self_test()`** imports `asyncio` and `tempfile` at module scope instead of inside
  the function body, matching every other import in the file.
- **Scan loop** uses `enumerate(as_completed(futures), start=1)` instead of a manually
  incremented `done` counter. The value is still the 1-based progress count passed to
  `_scan_one_done`.

### Fixed

- **`_update_row`** passes `strict=False` to the `zip()` of `repo.cells()` against
  `COLUMNS`. The two sequences are only equal-length by convention; the explicit flag
  documents that surplus columns are intentionally ignored and satisfies `ruff B905`.
- **`BINDINGS` tables** on `ConfirmScreen`, `FolderPickerScreen`, and
  `ComfyUICustomNodeUpdaterApp` are annotated as `ClassVar[list[BindingType]]`, so the
  type checker knows they are Textual binding tables rather than mutable instance state
  (`ruff RUF012`). No change to how Textual reads them.

## [0.1.0] - 2026-09-26

Initial public version.

### Added

- **Terminal UI** for scanning a ComfyUI `custom_nodes` folder and updating every
  git-based custom node safely.
- **Parallel fetching** of remotes (up to 8 concurrent) so a full scan stays fast with a
  large `custom_nodes` folder.
- **Fast-forward-only updates** via `git merge --ff-only`. There is no code path that
  creates a merge commit or a rebase.
- **Safety checks** that skip and explain repos with local commits (diverged), a detached
  HEAD, no upstream, or conflicting local edits, rather than forcing anything.
- **Optional autostash** that stashes dirty repos, updates them, and restores the stash;
  repos whose local edits overlap incoming changes are skipped instead of half-applied.
- **Upstream tracking** that compares against the branch that actually tracks upstream
  and understands forks with a separate `upstream` remote.
- **Folder discovery** by `--nodes-dir`, `$COMFYUI_CUSTOM_NODES`, the remembered config
  choice, `$COMFYUI_DIR`, or by walking up from the working directory and the script.
- **In-app folder picker** (press `o`) with a directory tree and a path field, shown
  automatically when no folder can be found; the choice is remembered in a config file.
- **Multi-select and a detail pane** for selecting individual repos or every outdated
  one, and previewing incoming commits before pulling.
- **Keyboard-driven interface** with six built-in color themes and a filter box.
- **No credential prompts**: git is invoked with interactive prompts disabled, so a scan
  never hangs waiting for a password.
- **Headless self-test** (`--self-test`) that builds throwaway repositories in a temp
  directory and exercises a full scan and pull without a network.
- **Windows batch launcher** (`ComfyUI-Custom-Node-Updater-TUI.bat`) that finds a ComfyUI
  venv by pattern without hardcoding its name, runs the script with `-X utf8`, and pauses
  so errors stay readable.
- **README**, **GPL-3.0 license**, and **`.gitignore`**.

[0.3.0]: https://github.com/LacklusterOpsec/ComfyUI-Custom-Node-Updater-TUI/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/LacklusterOpsec/ComfyUI-Custom-Node-Updater-TUI/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/LacklusterOpsec/ComfyUI-Custom-Node-Updater-TUI/releases/tag/v0.1.0
