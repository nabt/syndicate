# Usage-aware handoffs

Syndicate monitors account usage by default and stops its managed team when
the host's last recorded window reaches **10% remaining**. It saves a handoff
and asks whether to leave agent work stopped or continue with the other CLI.
Switching the host requires an explicit answer. A non-interactive host stops
with exit code 75. Specialists can back off to another provider independently.

This is account usage, not the conversation's context window or an exact token
balance. Providers report percentages for usage windows. A context compaction
does not replenish account usage. Polling and provider reporting can lag; the
reserve is a precaution, not a guarantee that no request will hit a limit.

## Use

```sh
# Default: stop at 10% remaining
syndicate team task --project ~/code/project "implement the feature"

# Override for one run
syndicate team task --reserve 20 --project ~/code/project "implement the feature"

# Report limits for both providers
syndicate limits
# Keep reporting limits and show new reset notices on stderr
syndicate limits --watch

# Discard a recorded reading you know is outdated (see "Recovering from an
# outdated low reading" below); the reserve cannot be enforced for that
# provider until a new reading is recorded
syndicate limits --forget claude

# Resume later, using Codex for the orchestrator and every specialist
syndicate team task -x codex --all-runtime codex --project ~/code/project \
  --handoff ~/code/project/.syndicate/handoffs/<handoff-id>
```

User-wide `~/.config/syndicate/syndicate.conf` (or
`$XDG_CONFIG_HOME/syndicate/syndicate.conf`), and per-project
`.syndicate/syndicate.conf` for keys that should differ:

```ini
handoff = on
handoff-reserve = 10
```

A key in the project file replaces that same key from the user file. Other
keys still come from the user file.

`handoff = off` restores the original direct CLI launch without managed
interruptions or skill injection. It does not stop an already-running account
monitor; use `syndicate limits --stop` for that.

## What happens at the reserve

1. Syndicate interrupts the managed CLI and workers, waits briefly for exit,
   and terminates remaining managed processes. The supervisor lives outside
   the model loop, so this does not require another inference request.
2. It creates `.syndicate/handoffs/<id>/` containing the original request,
   semantic checkpoint, previous status/history, quota observations, and staged
   and unstaged diffs of the launch directory. Session output is retained in
   `.syndicate/runs/<id>.log`. The live worktree, including untracked files,
   remains in place. See [Task worktrees](#task-worktrees) for the snapshot of
   the task's own worktree.
3. It asks whether to stop or choose a CLI for continuation. You can also
   choose the current host after a legacy specialist lease causes a handoff.
   The prompt then names the exhausted provider and offers the host for every
   role. Pressing Enter, EOF, or non-interactive
   input leaves work stopped. A target whose recorded reading is not current
   and is at or below the reserve is checked once, for that CLI only, before
   it is refused. If the fresh reading is still at or below the reserve, it
   is not launched. A reading that is already current is not queried again.
   The refusal names `syndicate limits --forget` when the recorded reading
   still decides, and again when a fresh reading is still too low.
4. An approved switch starts a new conversation reading that handoff and uses
   the selected CLI for every role in that run. Saved project assignments are
   unchanged. Outstanding tests and reviews remain outstanding.

The launcher holds a project lock across the switch to prevent two Syndicate
orchestrators from editing the same project. The process group includes native
workers and relays; on Linux, tracked descendants in separate process groups
are also terminated with PID identity checks. Unrelated terminal sessions are
not stopped. Independently daemonized work that escapes tracking is outside
this supervisor; do not launch such workers for managed tasks. A killed tool
may leave partial changes that the next orchestrator must inspect.

## Task worktrees

Every project task works in a linked worktree that Syndicate creates and
locks:

```sh
syndicate tasks worktree TASK_ID -C SOURCE --repo REPO --branch work/topic-1234 \
  [--series NAME] [--base REF] [--json]
```

`SOURCE` is the task's launch source: the directory whose
`.syndicate/status.json` tracks the task. The command refuses to run when that
file does not exist, and warns (without refusing) when the task ID is not
listed there yet, because the worktree is created before the first edit, which
can precede recording the task. Branch names may contain only letters, digits,
`.`, `_`, `/` and `-`; series names are one line of at most 100 letters,
digits, spaces or `._/:@+-`, starting with a letter or digit. `--base` must
name a commit and may not start with `-`.

The worktree lives at
`~/.syndicate/worktrees/<repo>-<hash>/<branch with / replaced by ->`, where
`<hash>` is the first 8 hex digits of the SHA-1 of the repository's real Git
common directory. `$SYNDICATE_WORKTREE_ROOT` replaces `~/.syndicate/worktrees`.
Locations that do not survive a restart are refused: the system temporary
directory, `/tmp`, `/var/tmp`, `/dev/shm`, `/run`, `$TMPDIR`,
`$XDG_RUNTIME_DIR`, and on Linux any tmpfs or ramfs mount. The command prints
the absolute path (or the registry entry with `--json`).

An existing branch is checked out. A new branch starts from `--base` (default
`origin/main`, else `main`, else `HEAD`) without upstream tracking, so a push
cannot reach the base branch by accident. A branch that exists only on origin
tracks `origin/<branch>`. The worktree is locked against `git worktree prune`
as it is created, with the reason
`syndicate: task series <series> (task <id>); do not prune until series complete`.
The series defaults to the branch name. Rerunning the command is safe: it
re-adopts the worktree, locks it again if needed, and adds a later task ID of
the same series. A branch already checked out somewhere else, for example in
an ad hoc `/tmp` worktree, is not moved silently; the error explains how to
move it. A series may span several branches and worktrees: pass the same
`--series` for each.

`~/.syndicate/worktrees/registry.json` records each worktree's path, main
checkout, Git common directory, branch, series, base, task IDs, launch source,
creation time and lock reason. A registry file that exists but cannot be read
or is not a valid registry is never overwritten: creating, re-adopting and
cleaning up worktrees stop with an error naming the file. Inspect it; if it
cannot be repaired, move it aside and rerun `syndicate tasks worktree` for each
task worktree to register it again.

A handoff looks up the worktrees of the run's task there (and others of its
series) and writes `worktrees/<branch>/` in the handoff directory. The task is
the one the launcher recorded in `.syndicate/run.json`; when `status.json`
names a different current task, worktrees of both are captured and
`handoff.md` says so.

| File | Contents |
|---|---|
| `info.json` | path, branch, HEAD, base, series, `exists`, `locked`, snapshot ref and commit |
| `git-status.txt` | `git status --porcelain=v1 --branch --untracked-files=all` |
| `worktree.diff` / `staged.diff` | `git diff --binary HEAD` / `git diff --cached --binary` |
| `untracked.txt` | untracked, non-ignored files |
| `unpushed.log` | commits not on the upstream (or base, when there is none) |

It also records a recovery commit of tracked and untracked, non-ignored files
at `refs/syndicate/snapshots/<branch>/<handoff id>` in the shared repository.
A temporary index inside the handoff directory, seeded from a copy of the
worktree's index (or from `HEAD` if copying fails), builds it; the worktree,
its index and `HEAD` are unchanged. Snapshot refs therefore contain untracked
files that are not ignored, which can include local secrets such as an
unignored `.env`. Ordinary branch pushes never send them, but `git push
--mirror` or a push refspec covering `refs/*` would publish them.

`handoff.md` lists each worktree under `## Task worktrees`, or says that none is
registered or that the registry is unreadable. A worktree that is registered
but not locked gets a `WARNING` line with the command that locks it again; the
handoff itself only reads the worktree and adds the snapshot ref. A missing
worktree directory is reported, not fatal. `handoff.md` prints the recovery
commands with the actual values, shell-quoted. To recover a lost directory:

```sh
git -C REPO worktree unlock PATH && git -C REPO worktree prune
syndicate tasks worktree TASK_ID -C SOURCE --repo REPO --branch BRANCH --series SERIES
git -C PATH restore --source refs/syndicate/snapshots/<branch>/<id> --worktree -- .
```

The first line removes the stale, locked record (a locked record blocks both
`prune` and re-adding the path). The second adds the worktree at the same path
and locks it again. The third restores tracked and untracked files from the
snapshot without staging them. List snapshots with
`git -C REPO for-each-ref refs/syndicate/snapshots/<branch>/`.

Handoffs, session exits and quota stops never unlock or remove a task
worktree. `syndicate tasks cleanup-pr` does that after it verifies the merge:
it accepts only a lock whose reason starts with `syndicate:`, and refuses while
any other task registered to the same series and launch source, in any of the
series' worktrees, is neither done nor ended. Close an abandoned task with
`syndicate tasks edit TASK_ID --source SOURCE --state ended`; a task reported
as not found has no status record, so after confirming it is gone remove its
ID from `task_ids` in the registry. A Syndicate-locked worktree without a
registry entry, or an unreadable registry, also stops cleanup, because the
series cannot be verified. The series is checked again, under the registry
lock that worktree creation also takes, immediately before the worktree is
unlocked and removed; a task joining the series after the remote branch was
deleted therefore keeps its worktree, lock and local branch. If removal fails,
the lock is restored; if that fails too, the error says the worktree is
unlocked and prints the command to lock it again. After cleanup completes it
drops the registry entry and deletes that branch's snapshot refs with a
compare-and-delete, reporting each one. A registry error at that point is
reported as a warning; cleanup itself has finished.
Pass `--task TASK_ID` for the task running the cleanup; it defaults to the
current task of the worktree's launch source.

## The bundled skill

[`skills/syndicate-handoff/SKILL.md`](../skills/syndicate-handoff/SKILL.md) is
injected into either orchestrator's instructions while handoffs are enabled.
It instructs the orchestrator to maintain `.syndicate/checkpoint.md` before long
operations and after specialist results: goal, constraints, decisions, work
completed, pending work, active workers, validation, and next action.

That checkpoint supplies the reasoning context when the launcher must stop the
CLI without requesting a new summary. If the model has not written one, the
handoff explicitly records that it is missing. Git/status/output evidence is
still saved. Claude conversations and native agent IDs are not converted into
Codex sessions or vice versa. No global skill installation is needed for
Syndicate launches, and provider configuration directories are not modified.

## Usage sources and limitations

- **Codex:** the documented app-server `account/rateLimits/read` call, polled
  roughly every 30 seconds plus request time. No model turn is started.
  All returned usage buckets are considered conservatively. Account-limit
  telemetry requires a supported authenticated account; API-key-only usage may
  not expose it. See [OpenAI's app-server documentation](https://learn.chatgpt.com/docs/app-server).
- **Claude:** the documented `rate_limits` fields supplied to a launch-scoped
  status-line command. Supported accounts expose window percentages and reset
  times after an API response. See [Claude's status-line documentation](https://code.claude.com/docs/en/statusline).
  This replaces the status line for that Syndicate session only. Headless
  Claude and Claude-backed headless relays do not provide this status-line
  feed themselves; Claude readings only refresh while an interactive Claude
  session runs through Syndicate. There is no undocumented credential scraping
  or private quota API fallback.
- **Which providers are enforced (project sessions):** the host (the
  orchestrator's CLI) for the whole run. A delegated provider (a specialist
  launched through `syndicate team run-role`, such as a Claude role under a
  Codex host) is checked at launch and while the specialist runs. If the
  specialist reaches its reserve, its runner stops that attempt and checks
  the other providers. The host's own reserve still triggers the project
  handoff, which needs user confirmation to switch providers.
- **Latest real reading:** enforcement uses each provider's most recently
  recorded percentages, even when that reading is stale (older than 180
  seconds) or its reset time has passed. A passed reset is never treated as
  restored capacity, and no quota is inferred from tokens or elapsed time.
  Three budgets are recognized: monthly, weekly, and 5-hour. They are judged
  in that order. The first one at or below the reserve decides the refusal.
  When every recognized budget is above the reserve, the tightest one still
  counts, so a 5-hour window can block on its own. Choosing the engine with
  the most usage left compares those same budgets in that order. Claude's
  status line supplies `five_hour`, `seven_day`, and `spend_limit` (a monthly
  total unless its `period` says weekly or daily). Codex slots are not names:
  `windowDurationMins` classifies each one, and `individualLimit` is the
  monthly spend cap. Limitation: consumption after the last reading goes
  undetected until a new reading arrives, so a silent provider can cross the
  reserve unnoticed.
- **Choosing a CLI at startup:** before a host session, a specialist, or the
  Chief starts, Syndicate chooses the engine unless the command line named one
  (`-x`, `run-role --runtime`, or `--all-runtime`). A CLI named in the user or
  project file, or by `$SYNDICATE_RUNTIME`, is used when its remaining usage is
  above the reserve. Otherwise the installed CLI with the most remaining usage
  above the reserve is used, compared monthly, then weekly, then 5-hour.
  With no CLI named in either file or the
  environment, that fullest engine is always the one started. The built-in
  `claude` fallback is not a named CLI. A configured CLI that is already
  current and above the reserve is not compared with the other engine. A
  recorded reading the choice still depends on is queried when it is not
  current. A CLI that has never recorded a reading is not queried.
  Codex uses `account/rateLimits/read` and starts no model turn. Claude's
  status line does not run in print mode, so its check is one short terminal
  turn: Sonnet, low effort, tools disabled, and the prompt `ok`. The session
  stops as soon as `rate_limits` are recorded, and that throwaway session is
  removed. A current reading is not queried again. If a query fails, the last
  recorded reading still decides. A read-only role configured for Codex does
  not move onto Claude.
- **One check before a stale refusal:** when the CLI is already chosen and the
  reading that would refuse its launch is stale or awaiting reset confirmation,
  Syndicate queries that CLI once and decides from the new reading. The same
  Codex read and Claude turn described above are used. A launch decision does
  not query that CLI a second time.
- **Unknown or stale data (project sessions):** stale, awaiting-reset, or
  missing telemetry never stops a running session by itself. The supervisor
  warns once per provider per state change: it prints a warning when a
  provider's telemetry enters a non-current state (for example, `claude usage
  telemetry is stale (last observed 14:05, 42% remaining); enforcing the
  reserve with the last real reading`), stays quiet while that state persists
  or while telemetry is current, and warns again if it later leaves the
  current state again (stale, then current, then stale prints two warnings).
  A host with no reading at all launches with a warning: a Claude host is
  itself the source of Claude telemetry, so refusing would prevent the first
  reading. Malformed telemetry files (non-numeric, out-of-range, or
  non-finite values) are treated as no reading.
- **Specialist backoff:** without `--runtime`, `run-role` applies the startup
  choice above, then starts that provider. `--runtime` pins the first attempt.
  Upon a provider usage-limit failure or a refusal at the reserve, it refreshes
  any non-current reading for the remaining CLIs on `PATH` and retries on the
  provider with the highest usable percentage. Each provider gets at most one
  attempt. A missing reading is unknown. If all installed providers are known
  to have reached a limit or reserve, the task is marked
  **On hold** and the command exits with status 75. The launcher passes a run
  ID in `SYNDICATE_RUN_ID` so a specialist can update its owning task. Keep
  this variable in the host CLI's shell environment policy. An unbound,
  standalone `run-role` command can still retry. Read-only Codex roles require
  their isolated workspace to run shell checks and cannot fall back to Claude.
  The launch gate uses the stricter of the configured `handoff-reserve` and
  the active run's reserve.
  A non-current reading at or below that gate is refreshed once for that
  provider before the specialist is refused. A stale reading above the gate
  is accepted with a warning, without another query. The command
  continues using the active run's reserve while the specialist runs.
  Claude roles in text mode use structured results internally and print the
  result text. Explicit JSON and streaming JSON output pass through directly.
  `handoff = off` disables reserve enforcement. An explicit provider quota
  failure still triggers backoff using readings above zero.
- **Native specialists:** if the host can still respond after a provider
  usage-limit failure, the orchestrator invokes `team run-role ROLE
  --backoff-from PROVIDER` with the role's original brief and any known
  partial work. If the host itself exits or reaches its reserve first, the
  project handoff path applies.
- **Specialist leases:** the supervisor starts the CLI with
  `SYNDICATE_RUN_ID=<run id>` in its environment (Codex's default shell
  environment policy only drops names containing `KEY`, `SECRET` or `TOKEN`,
  so the variable reaches `run-role` invoked from Codex shell commands). While
  a delegated specialist runs, `run-role` keeps a randomly named lease,
  `.syndicate/specialists/<32 hex>.json` (mode 0600, directory 0700), holding
  the run id, provider, and role, and holds an exclusive `flock` on it for the
  specialist's whole lifetime. On exit it unlinks the lease and releases the
  lock. The runner enforces its own quota so only that specialist stops when
  its provider hits the reserve. The supervisor still enforces legacy leases.
  It counts a lease as active only while that lock is held
  by someone else and only when the run id is its own; this works when the
  specialist runs in another PID namespace (for example, inside the Codex
  `workspace-write` sandbox). A lease nobody holds (a killed specialist) is
  removed. Symlinked, FIFO, oversized, malformed, or foreign leases, and a
  lease directory that is symlinked, owned by another user, or group/world
  writable, are ignored. Each check reads at most 256 directory entries and
  examines at most 64 lease files; if the directory holds more, the
  supervisor warns once per run that leases beyond the cap are not enforced.
  If lease evaluation fails unexpectedly, the supervisor warns once and keeps
  enforcing the host; when evaluation works again it prints "specialist lease
  evaluation recovered" and a later failure warns again.
- **Lease limitation (SIGKILL):** the lease lives exactly as long as the
  `run-role` process holding it. SIGTERM and SIGINT unwind it normally (the
  lease is removed, and a tmux-hosted specialist's window is closed). If
  that process is killed with SIGKILL while its child CLI keeps running,
  the kernel releases the lock and the supervisor removes the lease. Usage
  enforcement by that runner stops. On Linux, the runner samples descendant
  processes during an attempt to stop tracked workers before retrying. A
  descendant that detaches between samples can escape that cleanup.
- **Lease location:** leases live in the `.syndicate/specialists/`
  directory of the project `run-role` resolves (the git top level when the
  project is a repository). In a project that is not a git repository,
  starting `run-role` from a subdirectory writes the lease where the
  supervisor does not look. Pass `--project` with the project root.
- **Lease write failure fails closed:** inside a supervised run, if
  `run-role` cannot record its lease (for example, a sandbox denies writing
  `.syndicate/specialists/`), it refuses to start the specialist: non-zero
  exit, "no <provider> CLI was launched", and advice to rerun the exact
  command outside the sandbox with approval.
- **Missing run id inside a supervised run:** if `SYNDICATE_RUN_ID` is not
  set or malformed (for example, a custom Codex `shell_environment_policy`
  strips it), `run-role` looks for a live supervised run of the project: it
  reads the run id from `.syndicate/run.json` (not following symbolic links;
  it must be a regular file owned by you, at most 1 MiB, with an id in the
  supervisor's format) and confirms the run is live because
  `.syndicate/session.lock` is currently held (a momentary non-blocking
  shared lock attempt fails; the lock file is never created, truncated, or
  held). If so, the lease uses that run id and `run-role` prints a notice
  that the id came from `run.json` (run ids are not secret). It fails closed
  (non-zero exit, no CLI launched) when `run.json` exists but the lock state
  cannot be determined (for example, `session.lock` is a symbolic link, FIFO,
  hard-linked, or unreadable), or when the lock is held but `run.json` does
  not name a valid run id. The lock is probed before and after reading
  `run.json`, so a run that ends in between is not credited with the lease.
  A specialist started without a run id while the project's own team is
  running is attributed to that run. The notice names the run id used.
- **Outside a supervised run:** when `SYNDICATE_RUN_ID` is not usable and no
  supervised run is live (no `run.json`, or `session.lock` missing or free;
  for example, `syndicate team run-role` started by hand), the runner enforces
  the configured reserve locally and does not write a lease.
- **Recovering from an outdated low reading:** before a stale or
  awaiting-reset reading at or below the reserve can refuse a launch,
  Syndicate queries that one CLI as described above. A fresh reading above
  the reserve is launched. If the query fails, or the fresh reading is still
  at or below the reserve, the launch stays refused. If you know a recorded
  reading is outdated and the query cannot replace it, run
  `syndicate limits --forget claude` (or `codex`). It removes only
  `~/.syndicate/limits/<provider>.json` (never through a symbolic link) and
  says so; until a new reading is recorded, the reserve cannot be enforced for
  that provider (the host launches with a warning; specialists on it are
  refused for lack of a reading). Syndicate never forgets a reading or infers a
  reset on its own. The preflight block, the launch-gate refusal, and the
  post-handoff "leaving work stopped" message all name this command.
- **Chief of Staff:** missing, stale, or awaiting-reset telemetry
  never triggers a Chief shutdown by itself. Preflight refreshes a non-current
  reading at or below the reserve once, for the Chief's CLI only, then enforces
  that reading. Running supervision keeps enforcing the latest recorded
  percentages, even after their expected reset. If an
  active statusline stops updating, consumption past the reserve goes undetected
  until the next reading; silence does not prove capacity. This applies with
  `--no-live` and in headless mode too. Unlike project sessions, the Chief does
  not print telemetry warnings. See [Chief quota policy](chief-of-staff.md).
- **Profiles:** observations describe the CLIs' default local account profiles,
  shared across projects. Custom account/profile overrides are not supported
  by this monitor. Do not use a different account for the worker and reader.

A delegated provider with no recorded reading cannot run specialists: the
launch gate fails loudly rather than run them unmonitored. For a
single-provider run, use both `-x` and `--all-runtime` to avoid depending on
another provider's telemetry.

## Reset notifications

A single background monitor starts with managed runs (or `syndicate limits`)
and continues after agent work stops. It records both providers' observations,
errors, and reset notifications in `~/.syndicate/limits/`. `syndicate limits
--watch`, active managed sessions, and the dashboards expose limit status.
Linux desktop notifications are attempted when `notify-send` is available;
all reset notices are retained in `notifications.log`.

A reset timestamp passing produces **reset expected**. Only a fresh provider
observation advancing that window produces **reset confirmed**. Inactive
Claude sessions cannot supply fresh usage; their scheduled reset can be
announced, but confirmed availability must wait for new telemetry. Notifications
are deduplicated across monitor restarts. No switch occurs merely because a
window resets.

Stop the background process with `syndicate limits --stop`. It may take the
current quota request (up to 15 seconds) to finish. Starting a new managed run
or calling `syndicate limits` starts it again. It is not installed as an OS
service: after reboot, start Syndicate or `syndicate limits` to resume monitoring.
