# Usage-aware handoffs

Syndicate monitors account usage by default and stops its managed team when any
observed window reaches **10% remaining**. It saves a handoff and asks whether to
leave agent work stopped or continue with the other CLI. Switching requires an
explicit answer. Non-interactive runs stop with exit code 75 and never switch
providers automatically.

This is account usage, not the conversation's context window or an exact token
balance. Providers report percentages for usage windows. A context compaction
does not replenish account usage. Polling and provider reporting can lag; the
reserve is a precaution, not a guarantee that no request will hit a limit.

## Use

```sh
# Default: stop at 10% remaining
syndicate -C ~/code/project "implement the feature"

# Override for one run
syndicate --reserve 20 -C ~/code/project "implement the feature"

# Inspect both providers and reset notices
syndicate limits
syndicate limits --watch

# Resume later, using Codex for the orchestrator and every specialist
syndicate -x codex --all-runtime codex -C ~/code/project \
  --handoff ~/code/project/.syndicate/handoffs/<handoff-id>
```

Per-project `.syndicate/config`:

```ini
handoff = on
handoff-reserve = 10
```

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
   choose the current host to move a depleted mixed-team specialist onto it. Pressing Enter, EOF, or non-interactive
   input leaves work stopped. A target already known to be below the reserve
   is not launched.
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
  feed themselves. They need a fresh observation from an interactive Claude
  session; otherwise managed execution pauses. There is no undocumented
  credential scraping or private quota API fallback.
- **Unknown or stale data:** observations expire after 180 seconds. After
  60 continuous seconds without usable data for any assigned provider,
  Syndicate stops and saves a handoff rather than assume there is capacity.
  This startup grace lets interactive Claude produce its first observation.
  It also means initial work can start before a quota is known.
- **Profiles:** observations describe the CLIs' default local account profiles,
  shared across projects. Custom account/profile overrides are not supported
  by this monitor. Do not use a different account for the worker and reader.

Monitoring covers providers assigned to the team, even before a particular
specialist is invoked. For a single-provider run, use both `-x` and
`--all-runtime` to avoid depending on another provider's telemetry.

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
