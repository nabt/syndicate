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
   and unstaged diffs. Session output is retained in `.syndicate/runs/<id>.log`.
   The live worktree, including untracked files, remains in place. The snapshot
   is recovery context, not a complete project backup.
3. It asks whether to stop or choose a CLI for continuation. You can also
   choose the current host after a legacy specialist lease causes a handoff.
   The prompt then names the exhausted provider and offers the host for every
   role. Pressing Enter, EOF, or non-interactive
   input leaves work stopped. A target whose last recorded reading is at or
   below the reserve is not launched, even if that reading is stale or its
   reset time has passed; the message names `syndicate limits --forget`.
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
  recorded percentages (the most constrained window), even when that reading
  is stale (older than 180 seconds) or its reset time has passed. A passed
  reset is never treated as restored capacity, and no quota is inferred from
  tokens or elapsed time. Limitation: consumption after the last reading goes
  undetected until a new reading arrives, so a silent provider can cross the
  reserve unnoticed.
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
- **Specialist backoff:** `run-role` starts with the configured provider.
  Upon a provider usage-limit failure or a refusal at the reserve, it reads
  the global last-known usage of each supported CLI on `PATH` and retries on
  the provider with the highest recorded usable percentage. Each provider
  gets at most one attempt. A missing reading is unknown. If all installed
  providers are known to have reached a limit or reserve, the task is marked
  **On hold** and the command exits with status 75. The launcher passes a run
  ID in `SYNDICATE_RUN_ID` so a specialist can update its owning task. Keep
  this variable in the host CLI's shell environment policy. An unbound,
  standalone `run-role` command can still retry. Read-only Codex roles require
  their isolated workspace to run shell checks and cannot fall back to Claude.
  The launch gate uses the stricter of the configured `handoff-reserve` and
  the active run's reserve.
  A stale reading above that gate is accepted with a warning. The command
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
  lease is removed, and a tmux-hosted specialist's session is stopped). If
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
- **Recovering from an outdated low reading:** a Claude reading only refreshes
  while an interactive Claude session runs through Syndicate, so a stale
  reading at or below the reserve would otherwise block Claude host preflight,
  Claude specialists, and a Claude handoff target indefinitely. If you know it
  is outdated (for example, the window has since reset), run
  `syndicate limits --forget claude` (or `codex`). It removes only
  `~/.syndicate/limits/<provider>.json` (never through a symbolic link) and
  says so; until a new reading is recorded, the reserve cannot be enforced for
  that provider (the host launches with a warning; specialists on it are
  refused for lack of a reading). Syndicate never forgets a reading or infers a
  reset on its own. The preflight block, the launch-gate refusal, and the
  post-handoff "leaving work stopped" message all name this command.
- **Chief of Staff:** missing, stale, or awaiting-reset telemetry
  never triggers a Chief shutdown. Preflight and running supervision enforce
  the latest recorded percentages, even after their expected reset. If an
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
