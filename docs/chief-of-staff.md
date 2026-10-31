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

Answers are persisted, not pushed into running CLI sessions. Orchestrators
check the shared inbox at work/resume boundaries and acknowledge answers after
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
team checks requests at safe boundaries; acknowledgment means incorporated,
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
