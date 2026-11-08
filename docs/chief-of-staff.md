# Chief of Staff

Cross-project coordination layer for Syndicate. The first delivery provides
persistent decisions, durable project briefs, session ownership, a team request
channel, and briefing/inbox views in both dashboards. The Chief of Staff agent runs separately from project teams using those tools.

## Launching the agent

```sh
syndicate chief-of-staff -x claude
syndicate chief-of-staff -x codex
syndicate chief-of-staff -x codex -p "Brief me on projects needing attention"
syndicate chief-of-staff -x claude "Route my release-review request to the owning team"
syndicate chief-of-staff -x codex --dry-run "Brief me"
```

The provider comes from `-x`, then `SYNDICATE_RUNTIME`, then `claude`.
`-p` runs one headless request. `--dry-run` prints the working directory and
provider arguments without starting a provider, checking provider availability,
or writing state. Task text is passed as one positional argument, never
interpreted as provider flags.

The agent definition lives in [`portfolio/chief-of-staff.md`](../portfolio/chief-of-staff.md),
outside `roles/`. It is not injected into project teams and does not load their
role overrides or runtime configuration. It uses a dedicated workspace at
`~/.syndicate/portfolio/workspace`, with its own `.syndicate/status.json`,
checkpoint and supervised session logs. It does not register a project or
replace any project status or orchestrator session. A portfolio lock allows
one Chief of Staff session at a time while project teams run independently.

Claude receives only the Chief of Staff custom role and Read/Bash built-in
tools, without automatic permission bypass. Codex launches in the portfolio
workspace with the portfolio store as an additional writable directory and
multi-agent features disabled. No project repository is added as a writable
root by this launcher. The role coordinates through supported portfolio
commands and maintains its own checkpoint; it does not implement project work
or directly delegate to specialists.

Quota supervision uses the existing monitor and defaults to a 10% reserve;
`--reserve` changes it. If monitoring stops the session, the launcher saves a
handoff and returns status 75 without starting another provider:

```sh
syndicate chief-of-staff --handoff /path/to/chief/handoff
# Explicitly choose a different provider when desired:
syndicate chief-of-staff -x codex --handoff /path/to/chief/handoff
```

A resumed session defaults to the snapshot's provider, even when the runtime
environment default has changed. Only Chief of Staff snapshots with valid
status and checkpoints are accepted. On resume, the agent reads the snapshot
and reconciles it with fresh portfolio records; it preserves unresolved work
without assuming that interrupted actions succeeded.

## Live inbox delivery

New managed interactive sessions enable automatic inbox delivery by default.
Restart already-running teams and Chief of Staff once to load it. Codex's root
agent runs the supplied `syndicate live attach -C SOURCE` startup instruction;
this binds its `CODEX_THREAD_ID` to that managed run. Specialists must not attach.
Claude registers a native session timer at startup. No terminal input is
injected, and receiving an event does not start a stopped team or switch providers.

Codex's default host queue checks for inbox events approximately once a second.
Claude defaults to a session timer that runs the bundled
[`syndicate-inbox` skill](../skills/syndicate-inbox/SKILL.md) every five minutes;
preview channels are not required. Both use the same durable event queue:

| Event | Recipient |
|---|---|
| New queued request | Live owner of that exact source/task and bound session |
| New answer or revised answer | Live owner of the decision's source/task |
| Open or edited decision question | Running Chief of Staff |
| Request acknowledged or failed; decision resolved | Running Chief of Staff |
| Explicit project message | Running Chief of Staff |

Codex uses its native [`queue` command](https://learn.chatgpt.com/docs/developer-commands#codex-queue)
without `--resume`; Claude uses its native
[`/loop`/cron scheduler](https://code.claude.com/docs/en/scheduled-tasks).
Provider scheduling determines the next safe delivery point. Tool calls and
permission prompts are not forcibly interrupted. Normal tool permissions and
account reserve monitoring still apply. Ordinary project status changes do not
wake Chief of Staff; ask it for a status refresh when needed.

Configure polling in the user-wide or project `syndicate.conf`:

```ini
inbox-interval = 5
inbox-transport = auto
```

The interval is in minutes. Supported values divide an hour evenly: 1, 2, 3, 4,
5, 6, 10, 12, 15, 20, 30, or 60. Unsupported values fail instead of silently
rounding the schedule. `auto` selects Codex queue delivery or Claude scheduled
polling. `poll` selects polling for either provider; Codex then checks on the
configured host interval and queues a model turn only when new events exist.
Chief of Staff inherits these two user settings, ignoring project settings.
Override a run with `--inbox-interval MINUTES` or `--inbox-transport MODE`:

```sh
syndicate chief-of-staff -x claude --inbox-interval 10
syndicate -x codex --all-runtime codex -C /source/repo \
  --inbox-transport poll --inbox-interval 5 "Continue the project"
```

Claude's root agent registers one recurring CronCreate job and records its ID.
Restored inbox jobs are reconciled on resume, without altering unrelated jobs.
No global scheduler or provider configuration is modified. Native scheduling
runs between turns, can be delayed by work or provider timing, and expires after
seven days unless renewed. It needs a running session and available cron tools;
when unavailable, the agent reports that polling is unavailable and retains
boundary checks. Empty Claude timer runs still use model turns. Polling never
refreshes routine project status or bypasses account reserve monitoring.

`channel` is an explicit Claude-only alternative using an MCP
[channel notification](https://code.claude.com/docs/en/channels-reference).
Claude custom channels currently require its preview feature and the
`--dangerously-load-development-channels server:syndicate-live` flag, supplied
only to interactive managed launches that select `channel`. This admits Syndicate's bundled channel;
it does not bypass tool permissions or relay approvals. If the account does not
support channels, use the default scheduled polling. `--no-live` is also
available for Codex and Chief of Staff. Headless launches and `handoff = off`
retain boundary checks without automatic delivery.

Orchestrators use decisions for questions requiring a durable answer. For other
prompts to present to the requester:

```sh
syndicate messages send "Please inspect the proposed onboarding flow" \
  -C /source/repo --task TASK_ID --session SESSION_ID --key onboarding-preview
syndicate messages list --json
# Chief of Staff acknowledges after presenting the message:
syndicate messages acknowledge MESSAGE_ID
syndicate messages list --all --json
```

Messages are durable in `~/.syndicate/portfolio/messages.json`. Sending requires
the active owning session; repeated source/task/session/key values return the
same message. Changed text needs a new key. Presentation acknowledgment does not
resolve a decision or complete project work.

Inspect transport readiness and delivery errors with:

```sh
syndicate live status -C /source/repo
syndicate live status -C ~/.syndicate/portfolio/workspace
```

For Codex, expect a `thread_id`. For Claude polling, expect `mode: poll`, a
`scheduler_job_id`, and `last_checked_at` after a successful poll. For an opt-in
Claude channel, expect `channel_ready: true`. `delivered` tracks provider
queue/channel submissions or handled polling envelopes, independently
of request, decision, and message acknowledgments. Failed queue/channel submissions
retain `last_error` and retry after ten seconds while the supervised CLI is running.
Failed scheduled checks leave events unseen and retry at the next polling interval.
Delivery is at least once: a crash between provider submission and recording it
can repeat a notification. Receivers must read fresh records and keep normal
acknowledgments idempotent. Undelivered records remain available after shutdown.

### Common message envelope and ordering

Requests, decision changes, and orchestrator messages retain their typed durable
records and existing acknowledgments. All transports wrap record references in
the same versioned envelope, stored in `~/.syndicate/portfolio/events.json`:

```json
{
  "schema_version": 1,
  "event_id": "request:RECORD_ID:queued:CONTENT_DIGEST",
  "sequence": 42,
  "correlation_id": "request:RECORD_ID",
  "kind": "request",
  "state": "queued",
  "occurred_at": "2026-11-08T18:00:00+00:00",
  "enqueued_at": "2026-11-08T18:00:01+00:00",
  "sender": {"role": "chief-of-staff"},
  "recipient": {
    "role": "project-orchestrator",
    "source_path": "/source/repo",
    "task_id": "TASK_ID",
    "session_id": "SESSION_ID"
  },
  "payload": {
    "record_type": "request",
    "record_id": "RECORD_ID",
    "answer_revision": null
  }
}
```

`sequence` is the authoritative global enqueue order, allocated under a shared
lock. Simultaneously observed records are sorted by occurrence time and stable
ID before enqueuing. Later discoveries get later sequences, even if their source
timestamp is older. The stable event ID deduplicates repeated reports; changed
content, state, or answer revision creates a distinct event. Correlation IDs
group changes to the same request, decision, or message. Sender/recipient roles
are explicit, with source/task/session identity when available. Timestamps are
informational and do not authorize session reassignment.

Polling returns up to 20 relevant, unseen envelopes in FIFO order:

```sh
syndicate live poll -C /source/repo
# After incorporation/presentation and any application acknowledgment:
syndicate live seen EVENT_ID -C /source/repo
```

`seen` does not replace a request, decision, or message acknowledgment. Unseen
envelopes retry on the next poll. Seen markers are per managed run, so pending
durable work can be presented again after restart. Receivers read current records
before acting, skip canceled work, and incorporate the newest answer revision.
An independent event can proceed if an earlier one is blocked; its seen marker
does not discard the earlier event. `has_more` indicates another bounded batch.

Host process ownership checks must run outside PID-isolated sandboxes, with
normal approval. New session records retain their PID namespace so an isolated
reader gets an explicit validation error instead of falsely reporting a dead
owner. Older records lack this guard; refresh them outside the sandbox or
restart their sessions. Invisible processes do not prove a team has stopped.

## Using the workspace

```sh
syndicate briefing                  # cross-project work, reports, and decisions
syndicate briefing --json           # structured briefing
syndicate decisions list            # open and answered decisions
syndicate decisions list --all --json

# Run from the owning task's source repo, or supply -C /source/repo.
syndicate decisions request "Which database should we use?" \
  --key database --context "Need persistent storage" \
  --option SQLite --option Postgres --recommendation SQLite --blocking

syndicate decisions answer DECISION_ID "Use SQLite"
# The owning orchestrator reads and incorporates the answer, then:
syndicate decisions acknowledge DECISION_ID -C /source/repo --task TASK_ID --answer-revision N
syndicate decisions withdraw DECISION_ID  # question no longer needed
```

The task defaults to `current.task_id` in the source status file. A stable
`--key` identifies one question within that source/task; repeated requests
update its context without losing an answer. A changed question needs a new
key. Closed decisions stay closed when reported again. Use `--deadline` only
for an explicitly supplied deadline. `decisions list --project NAME_OR_PATH`
filters by dashboard ownership, which can differ from the source directory.

Answers are persisted, and live notifications prompt managed interactive teams
to read them. Other runs check the inbox at work/resume boundaries. Teams acknowledge answers after
incorporating them. Acknowledgment must include the exact `answer_revision` read
with the incorporated answer; a revision mismatch is rejected under the store
lock, requiring the team to read and incorporate the new answer. Answered questions remain visible until then; resolved and
withdrawn records remain accessible with `--all`.

Portfolio records live in `~/.syndicate/portfolio/decisions.json`, independently
of project status, with file locking and atomic replacement for concurrent
writers. Invalid inbox data produces an error rather than being overwritten.
The browser uses a read-only `/api/briefing` endpoint; use the CLI to record
answers. No new browser write endpoints or session-control commands are added.

Both dashboards show pending decisions above projects. Console `--details`
adds context, options, recommendations, answers, and deadlines. New project
sessions record their provider; older reports display `unknown` when absent.
The briefing preserves reported task state and timestamps, including stale
process reports; it does not infer active progress from an old report.

## Project briefs, ownership, and team requests

```sh
syndicate briefs update --project theforge --goal "Ship the release" \
  --priority high --constraint "Keep deployment manual" \
  --dependency "Graphcue API contract" --next-step "Review release plan" --revision 0
syndicate briefs show --project theforge --json
syndicate briefs list --json

syndicate sessions                   # latest owner per source, including inactive owners
syndicate sessions --project graphcue --all --json

syndicate requests send "Review the release plan before implementation" \
  -C /source/repo --task TASK_ID --key release-review
syndicate requests list -C /source/repo --task TASK_ID --json
# The owning orchestrator incorporates the request in its plan/checkpoint:
syndicate requests acknowledge REQUEST_ID -C /source/repo --task TASK_ID \
  --session SESSION_ID --note "Added release review to the plan"
# Or reports why it cannot accept the request:
syndicate requests fail REQUEST_ID -C /source/repo --task TASK_ID \
  --session SESSION_ID --note "Needs a deployment decision first"
syndicate requests retry REQUEST_ID
syndicate requests cancel REQUEST_ID
syndicate requests list --all --json
```

Brief updates merge only the specified fields. Repeated `--constraint`,
`--decision`, `--dependency`, and `--next-step` flags replace their respective
lists. Use `--clear constraints` (or another field name) to clear a field.
Read the current revision and supply `--revision N` to reject edits based on
outdated context; 0 means no brief exists yet. Priorities are explicit
requester choices, not inferred by the system. Dependencies are reported text,
not an automatically enforced execution graph.

New project launches register a unique session token, source directory, task,
provider, PID, and process start identity. The token is published in
`current.session_id` and the task record. Sessions close when supervised runs
exit; exec-based launches and abrupt exits are recognized through process
liveness/start identity checks. Reused PIDs do not count as the original owner.
Legacy sessions without ownership records are not inferred to be active.

Requests are durable messages, not injected keystrokes or executable commands.
They target a source/task and bind to the current live session when available.
Without an owner, they wait for a session on that exact task. They are not
silently moved to a new task when a project starts unrelated work. An owning
team receives a live inbox notification when supported and checks requests at
safe boundaries; acknowledgment means incorporated,
not completed. It must supply its active session token, source/task identity,
and a note. Another session cannot acknowledge a request bound to an old one.

If a targeted session exits before acknowledgment, delivery failure is visible.
An abrupt exit is detected when reading the request view, even if it could not
write an exit record. `retry` explicitly rebinds the request to a current owner
of the same task, or waits for one; `cancel` dismisses an obsolete pending or
failed request. Resend with a new task ID/key to request work from another task.
Repeated `send` calls with the same source/task/key return the existing record;
a changed message needs a new key.

Coordination state lives in `~/.syndicate/portfolio/coordination.json` using a
separate lock and atomic replacement. Brief updates do not alter project status.
Briefings and both dashboards expose pending/failed requests and session
ownership; console brief/session details remain behind `dashboard --details`.
The browser remains read-only. No session is started, paused, or switched by
sending a request.

## Mission

Help the requester direct multiple project teams by keeping priorities,
decisions, dependencies, and requests for attention organized.

For example, a Claude CLI team can work on theforge while a Codex CLI team
works on graphcue. Each project has its own orchestrator and specialists;
the Chief of Staff brings their reported activity into one useful briefing.

## Personality

Be a trusted, organized colleague with a view across the work. Be warm,
steady, concise, and practical. Lead with what needs attention and recommend
a next step. Keep decisions and loose ends visible without adding ceremony.

An opening with verified context might be:

> Hey, Nick. Theforge is moving, and Graphcue needs your decision on onboarding.
> I'd start with that decision so its team can continue.

Use the requester's name only when known. When a task is already supplied,
acknowledge it and proceed instead of asking what to work on.

## Ownership

| Chief of Staff | Project orchestrator |
|---|---|
| Summarizes activity across projects | Plans and delivers work within one project |
| Maintains requester-approved project priorities | Sequences project tasks and delegates to specialists |
| Collects decisions requiring the requester | Explains the project question, options, and recommendation |
| Tracks reported cross-project dependencies | Resolves implementation blockers within its scope |
| Routes requests to the owning orchestrator | Owns validation, review, and completion reporting |

The Chief of Staff routes project work through its orchestrator. It does not
directly assign that team's specialists or take over an active project session.
Changes in priorities must reflect the requester's direction; recommendations
remain recommendations until adopted.

## First delivery: briefing and decision inbox

Start with a shared view of reported project activity before adding an
autonomous coordination agent.

The cross-project briefing should show:

- Which projects are working, waiting for attention, queued, or complete.
- Each project's current task, latest reported progress, and report time.
- The owning session and provider when recorded; otherwise show them as unknown.
- Decisions awaiting the requester, followed by a recommended next action when
  enough context exists.

The shared decision inbox should record:

- A stable decision ID, project, and related task ID.
- The question, relevant context, options, and the team's recommendation.
- Whether it blocks work and any explicitly supplied deadline.
- Its state (open, answered, resolved, or withdrawn), timestamps, and answer.

An answer is recorded before it is routed to the owning team. Mark it resolved
only when the owning team acknowledges that it has incorporated the answer.
Repeated reports update an existing decision rather than create duplicates.

Briefings must distinguish stale or unavailable reports from current activity.
A generic waiting status alone is not enough to invent a decision question.
Keep source project/task references visible so the requester can inspect context.

## Later delivery

The requester deferred the following work for a future session:

1. Mixed-provider live smoke test: run Claude on theforge, Codex on graphcue, and a separate
   Chief of Staff session; verify ownership/current work, a routed request and
   acknowledgment, and a decision question/answer/versioned acknowledgment cycle.
   The Codex-only request/acknowledgment and decision/answer/revision acknowledgment
   smoke test passed on 2026-11-08. Mixed-provider validation remains unrun.
   Automatic delivery added afterward still needs a provider-backed smoke test.
2. Show Chief of Staff running/waiting/stopped status and its latest checkpoint
   summary in the dashboards.
3. Notify the requester about decisions needing attention and failed requests.
4. Add user-directed team start, pause, and handoff controls.

Project briefs, session ownership, the acknowledged request channel, and the
separate Chief of Staff agent are implemented. The remaining phase is team
lifecycle controls for requesting starts, pauses, and handoffs under the
requester's direction, respecting existing project locks and handoff consent.

Starting another provider still requires the existing explicit user choice;
account monitoring and process shutdown remain launcher responsibilities.

## Integration boundary

Keep portfolio state separate from each project's agent-owned status file.
Project orchestrators publish progress and decision requests; the portfolio
layer aggregates them and stores answers without overwriting project history.
Coordination state must survive session restarts and retain the source project
and task identity.

The current launcher loads `roles/*.md` into project teams. The separate
`chief-of-staff` launch path loads `portfolio/chief-of-staff.md` instead, so it
does not become a specialist under every project orchestrator.
