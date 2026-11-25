# Per-user installation

Syndicate can run from an installed, immutable release instead of from a
checkout. `bin/syndicate-install` builds a release from an approved Git tag.
It verifies the release, activates it atomically and installs a stable
`syndicate` command. Running sessions keep the release they started with.
Upgrades and rollbacks only change what new invocations run.

Configuration and state stay where they are today: `~/.config/syndicate/`
(or `$XDG_CONFIG_HOME/syndicate/`), `~/.syndicate/` and each project's
`.syndicate/`. The installer never writes to them. See
[configuration](configuration.md) for config precedence.

## Prerequisites

- Python 3.11 or newer, standard library only. The installer records one
  absolute interpreter per activation and checks that it runs.
- Git, used by the installer to fetch and archive releases.
- A POSIX `sh`, used by the stable entry point.
- Depending on which features you use: the `claude` and/or `codex` CLIs,
  `tmux`, `gh` (PR cleanup), and `bash` plus `jq` (the review-gate hook).

## Layout

| Path | Contents |
| --- | --- |
| `$XDG_DATA_HOME/syndicate/releases/<commit>/` | One immutable release per full commit ID: `bin/`, `roles/`, `skills/`, `hooks/`, `web/`, `docs/`, `resources/`, `portfolio/chief-of-staff.md`, `README.md` and `RELEASE.json`. Files and directories are read-only. |
| `$XDG_DATA_HOME/syndicate/current` | Symlink to `releases/<commit>`, replaced atomically on activation. |
| `$XDG_DATA_HOME/syndicate/interpreters/<commit>` | The absolute interpreter the shim uses for that release. |
| `$XDG_DATA_HOME/syndicate/activations.jsonl` | Append-only activation log. |
| `$XDG_DATA_HOME/syndicate/entrypoints.jsonl`, `entrypoint-backups/` | What the stable entry point replaced, for recovery. |
| `$XDG_DATA_HOME/syndicate/activation.lock` | Lock that serializes publish, activate, rollback and prune. |
| `$XDG_DATA_HOME/syndicate/in-use/<commit>/<pid>` | Optional markers. A live PID keeps that release from being pruned. |
| `$XDG_CACHE_HOME/syndicate/source.git` | Bare release-source cache. It is the only Git input to a release and lives outside every project worktree. |
| `~/.local/bin/syndicate` | Stable entry point (shim). |

`$XDG_DATA_HOME` defaults to `~/.local/share` and `$XDG_CACHE_HOME` to
`~/.cache`. Every command accepts `--install-root DIR` (the `syndicate`
directory itself), `--bin-dir DIR` and `--source-cache DIR` overrides.
Rehearsals and tests use these overrides, or a disposable `HOME` and XDG
variables. The runtime looks for `activations.jsonl` under the default
`$XDG_DATA_HOME/syndicate` when it reports its identity, so production
installs should keep the default install root.

### Release contents and RELEASE.json

A release contains exactly the allowlisted files from the tag's commit:

- `bin/syndicate`, every `bin/syndicate_*.py` present at the tag, and
  `bin/syndicate-install`. The installer ships with the release, so you can
  upgrade or roll back from an installed copy after the checkout is gone.
- `roles/`, `skills/`, `hooks/`, `web/`, `docs/` and `resources/`.
  `resources/` holds the Codex rules referenced from the docs.
- `portfolio/chief-of-staff.md` and `README.md`.

Tests, `.gitignore` and everything else in the repository are left out.
`RELEASE.json` is keyed by commit. The tag is not stored in it:

```json
{"format": 1, "commit": "<40-hex>", "files": {"<relpath>": "<sha256>"}, "python_min": "3.11"}
```

Each activation appends one line to `activations.jsonl`. It records
`at`, `commit`, `tag`, `source_ref`, `upstream`, `archive_digest` (sha256 of
the `git archive` tar), `manifest_sha256`, `release_dir`, `interpreter`,
`previous_commit`, and `action` (`install` or `rollback`).

### The stable entry point

`~/.local/bin/syndicate` is a small `sh` script. It resolves `current` once
with `cd -P`, reads `interpreters/<commit>`, and runs
`exec <interpreter> -B <release>/bin/syndicate "$@"` with
`PYTHONDONTWRITEBYTECODE=1`. Activations after that point do not affect a
running invocation. The script embeds only the install root, so it stays the
same across upgrades.

When the installer first writes the shim, it records whatever was at that
path in `entrypoints.jsonl`. A symlink's target is recorded. A regular file
is copied to `entrypoint-backups/` with its mode and sha256.
`syndicate-install restore-entrypoint` puts the recorded entry point back. It
refuses to overwrite anything that is not the managed shim.

## Approved release tags

Only an approved release tag can be installed or upgraded to. A tag is
approved when all of these hold:

1. The operator names it exactly with `--tag`, and it matches
   `^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`. Branch names,
   commit IDs, `refs/...` and other revision syntax are refused before any
   Git command runs.
2. The tag exists at the trusted upstream right now. Each install fetches
   upstream `main` and that tag into the release-source cache, under
   `refs/syndicate/upstream/`. Old fetch results are deleted first, so a
   failed or offline fetch never falls back to stale refs.
3. The tag peels to a commit. Annotated and lightweight tags both work, and
   the log records which kind it was.
4. `git merge-base --is-ancestor <commit> <fetched upstream main>` succeeds.
   The commit must be on upstream `main` as fetched by this same invocation.

The release is built with `git archive` of that commit from the cache. Each
archived file is compared with its committed blob, so `export-subst` and
`export-ignore` attributes cannot change the result. A dirty or unmerged
working tree is never an input. To run an unmerged checkout, use
`syndicate dev --checkout PATH`, which is separate from installed releases.

The trusted upstream is set once with `--upstream URL|PATH` and remembered in
the cache. For this project that is `https://github.com/nabt/syndicate.git`.
Passing a different `--upstream` later changes it explicitly and prints a
notice.

## Bootstrapping the first release

Creating the first release tag is a requester action at cutover time. The
installer and the release-aware runtime have to be merged to `main` first.

1. Merge the installer and runtime-boundary changes to `main` on GitHub.
2. The requester chooses the commit on `main`, then creates and pushes an
   annotated tag:

   ```sh
   git fetch origin
   git tag -a v1.0.0 -m "Syndicate 1.0.0" <commit-on-origin/main>
   git push origin v1.0.0
   ```

3. Rehearse with disposable roots before touching the real entry point:

   ```sh
   scratch=$(mktemp -d)
   python3 -B bin/syndicate-install install --tag v1.0.0 \
     --upstream https://github.com/nabt/syndicate.git \
     --install-root "$scratch/syndicate" --bin-dir "$scratch/bin" \
     --source-cache "$scratch/source.git" --python /usr/bin/python3
   "$scratch/bin/syndicate" --version
   ```

4. Run the real install as part of the release transition runbook below.

Pass `--python` with a stable absolute interpreter, such as
`/usr/bin/python3`. By default the installer records the interpreter that is
running it, which might be a versioned path that a package upgrade removes.

## Install and upgrade

```sh
python3 -B bin/syndicate-install install --tag vX.Y.Z --upstream https://github.com/nabt/syndicate.git
# later, from any copy of the installer (checkout or installed release):
python3 -B ~/.local/share/syndicate/current/bin/syndicate-install upgrade --tag vX.Y.Z
```

`install` and `upgrade` are the same operation:

1. Preflight the interpreter: it must be an absolute path to an executable
   that reports Python 3.11 or newer.
2. Fetch and approve the tag, as described above.
3. Stage the release in `releases/.staging-<pid>-*`, a sibling of its final
   location. The installer extracts the archive, writes `RELEASE.json` and
   checks the required assets. These are every shipped role and skill, all
   four bundled skills, `hooks/require-review.sh` (executable), `bin/syndicate`
   (executable), `web/dashboard.html`, `portfolio/chief-of-staff.md`,
   `README.md`, and every `syndicate_*` module the launcher imports or lists
   in `RUNTIME_MODULES`. Relative Markdown links must resolve inside the
   release. Anchors are not checked.
4. Make the stage read-only and smoke-test it from an unrelated directory
   with a disposable `HOME`, XDG, Codex and Claude directories, and no
   tmux. The smoke test runs `syndicate --help`, `syndicate --version` (when
   the release supports it, which also checks the commit), and
   `chief-of-staff --dry-run -x codex`. Afterwards the stage is re-verified
   against its manifest.
5. Take `activation.lock`. Rename the stage to `releases/<commit>`. If that
   directory already exists, it is verified and must be identical; the
   installer then reuses it and records the new tag. A differing or damaged
   existing release is refused. Write `interpreters/<commit>`, replace
   `current` atomically, append to the activation log, and install or
   refresh the shim. Pass `--no-shim` to skip the shim.

If anything fails before the switch, the previous release stays active and
the stage is removed. If the installer is killed during staging, a
`.staging-*` directory is left behind. `prune` removes it once its PID has
exited. Concurrent installers serialize on the lock, and `current` always
points at a complete, verified release.

## Status, list and verify

```sh
syndicate-install status            # current commit, tag, interpreter, previous, shim state
syndicate-install list              # retained releases with current/previous/in-use marks
syndicate-install verify [COMMIT]   # recheck a release (default: current) against RELEASE.json
```

Add `--json` for machine-readable output. `syndicate --version` reports the
running release, as seen from inside Syndicate.

## Rollback

```sh
syndicate-install rollback                 # most recent other release that was activated and is retained
syndicate-install rollback --to <commit>   # a specific previously activated release (unique prefix allowed)
```

Rollback works offline. It verifies the target against its manifest,
preflights the interpreter recorded for it (override with `--python`),
switches `current` under the lock, and logs `"action": "rollback"`. Only
previously activated releases are eligible. Rolling back changes the
executable only. Portfolio data, decisions, messages and requests written in
the meantime are kept. Stored formats must stay compatible across the
retained pair. A future incompatible format change needs its own backup and
reconciliation procedure.

## Prune and retention

```sh
syndicate-install prune --dry-run
syndicate-install prune
```

`prune` never removes:

- the current release,
- the previous known-good release (the default rollback target),
- a release with a live `in-use/<commit>/<pid>` marker,
- a release referenced in the command line of any running process (found
  through `/proc`),
- a release registered as the Codex Stop hook in `$CODEX_HOME/hooks.json`
  (`--codex-home` overrides the location).

Keep a release while any Chief, team, dashboard, monitor, hook or explicit
resume path still uses it. If a session is idle with no process on that
release (for example a handoff you plan to resume with its recorded prefix),
create an `in-use` marker or skip pruning.

## Codex Stop hook migration

The optional Codex review gate is registered as an absolute path in
`$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`). Migration is an
explicit operator step and never runs automatically:

```sh
syndicate-install migrate-codex-hook --dry-run    # show what would change
syndicate-install migrate-codex-hook              # default target: current release
syndicate-install migrate-codex-hook --target <commit>
```

The migration:

- Changes only Stop hooks whose command is a single absolute
  `.../hooks/require-review.sh` path. It replaces that hook's `command`
  field and nothing else. Unrelated entries, other events, sibling hooks in
  combined entries, and fields such as `matcher` or `timeout` are preserved.
- Verifies where the old hook came from before changing anything. Its
  content must equal `HEAD:hooks/require-review.sh` of the checkout it lives
  in, or the digest in its retained release's `RELEASE.json`. If any matching
  hook cannot be verified, the file is left unchanged.
- Never adds a hook when none is registered. Use `syndicate setup-codex-gate`
  for that. It collapses duplicates, so the release command appears once.
- Copies the original file to `hooks.json.syndicate-backup-<time>-<pid>`
  before writing atomically with the same mode.
- Does not approve the hook. Open `codex` and approve the changed hook
  through its normal hook review prompt.

The new command is pinned to `releases/<commit>/hooks/require-review.sh`.
After an upgrade, the hook keeps pointing at the release it was migrated to,
and `prune` keeps that release. Run the migration again when you want the
gate to follow the new release, then approve it again.

## Release transition runbook

Use this procedure for the first cutover from a checkout, for every upgrade,
and for rollback with the roles of the two versions reversed. Production
cutover needs the requester's separate approval.

### 1. Record and prepare

- Record the current release (`syndicate-install status --json`) or, before
  the first cutover, the existing entry point (`ls -l ~/.local/bin/syndicate`).
  Also record the active Chief and teams (`syndicate sessions --json`,
  `syndicate list workers`), the registered review hook command and its
  approval state, the limits monitor (`~/.syndicate/limits/monitor.json` and
  lock owner), and any dashboard processes.
- Stage both the target and the rollback release. Install the rollback
  target first, or confirm it is retained with `verify`.
- **First cutover with no prior installed release:** record a
  checkout-backed fallback before changing the entry point.
  1. Export a snapshot of a verified commit to a preserved directory outside
     the editable checkout, for example
     `git -C <checkout> archive <commit> | tar -x -C ~/.local/share/syndicate-fallback/<commit>`.
     Make it read-only.
  2. Record the exact fallback prefix (`/usr/bin/python3 -B
     ~/.local/share/syndicate-fallback/<commit>/bin/syndicate`), its assets
     (`roles/`, `skills/`, `hooks/require-review.sh`, `web/`) and its hook
     target.
  3. Preflight it: run `<prefix> --help` and
     `<prefix> chief-of-staff --dry-run -x codex` from an unrelated directory.
     Then change the entry point.
  The installer records the old symlink in `entrypoints.jsonl`, but it
  cannot tell whether that symlink points at a preserved snapshot.
- Back up `$CODEX_HOME/hooks.json`. `migrate-codex-hook` also makes its own
  backup.

### 2. Drain and switch

- Let the Chief and teams finish or checkpoint under their current owners.
  Wait for any worker that relies on the global Codex hook or approval to
  finish before switching it.
- Stop or quiesce every old-release dashboard. `syndicate web` and its
  `/api/limits` endpoint auto-start the shared monitor. A dashboard you must
  keep running through the transition has to be restarted with
  `syndicate web --no-monitor`, which never starts the monitor, before you run
  `limits --stop`; otherwise it can restart the old-release monitor at once.
  Pause other autostart paths too: Chief and team launches and direct
  `limits` invocations.
- Stop the monitor with `syndicate limits --stop`, then wait until
  `~/.syndicate/limits/monitor.lock` is free. The stop command can return
  before the monitor exits.
- Activate the target: `syndicate-install install --tag vX.Y.Z` (or
  `rollback`).
- If the Codex gate is enabled, run `syndicate-install migrate-codex-hook`.
  Approve it in Codex, then check its absolute target and that it blocks a
  stop as expected. If you defer the hook, record an explicit exception: its
  path, its dependency on the checkout, and its owner. Keep that file in
  place. The checkout is not fully independent until the exception clears.

### 3. Restart and verify

- Run `syndicate --version` and check the commit and release directory.
- Restart the monitor and only the dashboards you intend to run, from the
  selected release (a plain `syndicate web` starts the monitor; restart any
  `--no-monitor` dashboard without the flag, or start the monitor with
  `syndicate limits`). Check the monitor lock owner and its release, the served
  dashboard and its data endpoints, and the hook's effective target.
- Start the Chief and teams only after the services they need and their
  approvals are verified. Check paused consumers and sessions still pinned to
  the old release separately.

### What changes for users at the first cutover

- **Provider approvals re-prompt once.** Relay and session commands now start
  with the pinned prefix `<interpreter> -B <release>/bin/syndicate`, for
  example `Bash(/usr/bin/python3 -B /home/me/.local/share/syndicate/releases/<commit>/bin/syndicate team run-role *)`.
  Saved Claude or Codex approvals keyed on the old
  `.../bin/syndicate team run-role` prefix no longer match, so each provider
  asks once more; the prefix changes again with each release directory.
  Generated session settings carry the new rule automatically.
- **No `)` in paths.** Claude's `Bash(...)` rule cannot contain `)`, so the
  install root, the release path and the interpreter path must not contain
  one. `syndicate-install install` and `rollback` refuse such paths.
- **Reserved subcommands.** `dev`, `version` and `--version` are Syndicate
  subcommands. `syndicate version` used to start the Chief of Staff with the
  task "version"; to give the Chief such a task now, use
  `syndicate chief-of-staff version`.
- **Bytecode stays off in children.** Every Syndicate entry path sets
  `PYTHONDONTWRITEBYTECODE=1`, and provider CLIs (Claude, Codex) and the
  commands they run inherit it. Python tools run inside a session do not
  write `__pycache__` unless they set it themselves.

### 4. Partial failure

- Keep both releases and pause further launches. Record the actual shim
  target, the hook and its approval, and the monitor and dashboard versions.
- Finish the transition only if the intended set of services can be
  verified. Otherwise:
  1. Stop any services you started.
  2. Select the prior artifact with `syndicate-install rollback`. If there
     was no prior release, use `syndicate-install restore-entrypoint` or the
     recorded checkout-backed fallback.
  3. Restore only the recorded hook entry from the backup, and approve it
     through the normal provider flow.
  4. Restart and verify only the intended prior services before resuming.
- Do not overwrite durable portfolio data while recovering services.

## Development

Installed releases are for normal use. To test a branch, use
`syndicate dev --checkout PATH`. It runs a snapshot of that checkout with
disposable state and never changes `current`, the shim or provider
registrations. Run the test suite with bytecode disabled:

```sh
python3 -B -m unittest discover -s tests
```

`tests/test_install.py` builds fixture releases from the current worktree in
a local bare upstream. It runs every installer path against disposable
`HOME`, XDG, Codex and Claude directories.
