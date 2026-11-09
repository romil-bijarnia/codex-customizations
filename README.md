# Personal Codex changes, not another Codex repository

This standalone repository contains only the differences from official Codex,
our added helper files, and the machinery to apply, build and install them.
It has no upstream Git history or complete upstream source tree. The original
`romil-bijarnia/codex` fork is retained as historical source, not used for updates.

## Using it

The current machine is an Apple-silicon Mac running Linux. Its package target is
`aarch64-unknown-linux-gnu`; this does not silently install Linux binaries on macOS.

```sh
codex update          # follow current openai/codex main, preserving our changes
codex update --check  # availability only; no build requested or package installed
codex update --no-wait # request a missing remote build, return without installing
codex custom-status  # selected build, upstream commit and stored-diff identity
codex update --revision FULL_SHA # optional exact revision for recovery/validation
```

The command updates this small checkout with a fast-forward pull, resolves the
current upstream commit and checks for a matching published build. If available,
it downloads the finished package, verifies its checksum and executable hashes,
and selects it atomically. Otherwise it requests GitHub Actions and waits, or
returns immediately with `--no-wait`. GitHub CLI authentication is reused; no
credentials are stored here. The working installation is retained on any patch,
build, check or download failure. Existing chats are not restarted. The persistent backend is updated by a detached
worker only after it observes no active loaded turns for 15 seconds. Failed or
incomplete observations never count as idle. Progress is recorded under
`$CODEX_HOME/state/codex-customizations/backend.json`.

Ordinary CLI arguments are passed directly to the selected native executable.
No prompt, account, chat history, personal database or access setting is replaced.
The two owned API/visualization helpers are updated along with their native CLI.

Normal startup keeps the saved model, reasoning and permission defaults. The
`/daybreak` toggle independently selects GPT-6-Sol with Daybreak Blue and Extra high
reasoning for the active conversation, retaining its permissions, including EL2
Control. Turning Daybreak off restores that conversation's previous model and
reasoning. It does not overwrite the normal launch defaults or enable Daybreak
for other conversations. Blue and Extra high must both be advertised by the server;
if unavailable, the selection is left unchanged rather than silently downgraded.

## What is the stored difference?

- `patches/personal-cli.patch`: code changes relative to the official commit in
  `manifest.json`, including native helpers, picker/Daybreak/reasoning choices,
  steering-only input, scrolling, doctor fixes and the EL2 UI integration.
- `overlays/`: files added by us, not copies of unchanged upstream source.
- `build.py`, `manage.py`, `bin/codex`: mechanical build/update/install operations.
- `checks/`: existing focused terminal checks, using disposable profiles and
  localhost model responses rather than paid inference.

Cargo workspace-version stamping is build metadata rather than a stored diff.
The builder stamps a constant development version per upstream release series,
so timestamps do not invalidate every Rust crate. The package version and
manifest record the exact upstream SHA and patch-set digest.

## Automated builds

GitHub Actions checks upstream `main` every six hours. A manual `codex update`
can request an exact source revision sooner. A matching release is reused rather
than rebuilt. Standard public Linux/ARM64 runners perform the actual compilation;
the laptop does not clone upstream or compile Rust during an update.

The workflow fetches the upstream base and target only into its temporary runner,
applies the stored patch with Git's three-way patch application, copies our added
files, and builds the CLI, exec-server, code-mode host and bwrap together. Upstream
checksum-pinned V8 artifacts and official package resources are used. The source
workspace disappears with the runner; finished packages are release assets, not
files committed into this repository.

The remote checks cover defaults, picker display/order, helper dispatch, voice
selection/reminder behaviour, literal pastes/API arguments and steering. The
installed bundled Visualize plugin is a target-machine dependency, not present on
a clean GitHub runner: its real renderer is checked locally before package
selection, without opening a browser or changing settings. Kernel/EL2 activation cannot
be established on a GitHub runner and is explicitly not reported as verified.
The bootstrap package reuses the previously verified local build; it is labelled
as reused, not as a newly compiled build of a newer upstream revision.

If upstream changes the code under a patch, automation may stop with the exact
conflict instead of silently losing a feature or requiring an agent for every
routine update. The last working package remains selected. A recorded diff is
reviewable, but it is not a universally compatible binary addition.

## Installation / recovery

Clone this small repository and run `python3 manage.py install-launcher` after a
complete native package is selected at `$CODEX_HOME/packages/standalone/current`.
The installer replaces only the expected personal CLI symlink; it refuses an
unrelated launcher. Re-running it is idempotent. To remove only the update wrapper,
point `~/.local/bin/codex` back at that `current/bin/codex` path. Neither operation
changes settings or terminates sessions.

Python 3.12+, Git and the authenticated GitHub CLI are required. Native macOS and
other architecture artifacts need their own workflow target before selection.
Personal AGENTS.md, account state and machine-specific private instructions stay
in the existing private GRID/profile installation, not this public repository.

Compilation caching also records source hashes and timestamps. After a fresh
checkout, only byte-identical tracked files regain their prior timestamps;
changed files retain new timestamps and are rebuilt. This avoids rebuilding
every unchanged workspace crate merely because CI performed a new checkout.
Completed compilation is cached even if a subsequent runtime check fails.

The CLI's `--version` reports the compiled development series (for example
`0.169.0-dev`). The package manifest and `codex custom-status` carry the complete
source revision, patch digest and build label. These are deliberately distinct;
the updater verifies both metadata and exact executable checksums.
