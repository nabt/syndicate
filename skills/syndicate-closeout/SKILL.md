---
name: syndicate-closeout
description: Close a managed Syndicate project session with verified outcomes, accurate task state, durable checkpoint and a Chief of Staff notification. Run before the orchestrator ends its session on Claude or Codex.
---

# Close a Syndicate session

## Mandatory project delegation

Every project task, including small tasks and non-code deliverables, requires
a specialist to perform the requested work and a different specialist in a
distinct role to independently review the final result and verify the outcome.
Use Code Reviewer for code. Send corrections back to implementation, then
return the updated result for independent review. Apply these requirements
with custom role instructions and with the optional transcript hook disabled.

Use the configured native agent or relay and confirm that the specialist
actually ran and returned the requested work. Record the role, actual provider,
participant ID, and completed work or verification in the checkpoint as each
stage returns. Leave project deliverables and independent verification with
their delegated specialists.

If a required specialist cannot start, report the role and actual launch
failure to the requester. Retry or delegate to an eligible replacement only
within the task and provider constraints. If the role remains unavailable, stop
dependent work and close the task as blocked with `--role-blocker ROLE REASON`.
Preserve completed work and outstanding verification. A relay counts only once
its underlying specialist actually performs the work.

## Closeout checklist

Requester-reported external outcomes must be verified before confirming them.

Run this ordered checklist before the final response. SOURCE is the launch
source containing status.json; use the supplied executable prefix. Preserve
current.task_id, current.session_id, all launcher ownership fields and the
owning task's `delegated_closeout_required` flag when present.

1. Read task-scoped requests and decisions with `requests list -C SOURCE
   --task TASK_ID --json` and `decisions list --json`. Match source/task/session,
   incorporate authorized input, and acknowledge the exact decision revision
   or request using the owning session. Record unresolved questions explicitly.
2. Verify external outcomes before confirming them. For a reported PR merge,
   use `gh pr view NUMBER --json state,mergedAt,mergeCommit,url` in the correct
   repository. Record URL, state, time/commit, tests actually run, review results
   and remaining work in task progress and checkpoint before confirming. Reconcile
   affected older records without changing ownership or unrelated pending work.
   A merge proves neither deployment nor full completion. If verification fails,
   record that the outcome remains unverified and say so. Record evidence in the
   matching task's status/progress. If either durable write fails, report which
   record failed and its error explicitly.
3. Inspect leftover queued tasks created by this session. Close obsolete
   placeholders as ended with a reason using `tasks edit`; retain real unfinished
   requests with an explicit owner/next step in the checkpoint and Chief message.
   Never close unrelated tasks or claim unfinished work done.
4. Choose an accurate state. Done requires all requested work and independent
   verification, with a distinct implementation role and review role performed
   by different specialists. Use ended for a session ending with incomplete work,
   waiting_on_user for a specific pending answer, and blocked for an identified
   obstacle. If a required role remains unavailable, use blocked and include the
   role and launch failure. Keep a truthful progress note. Preserve deliberate
   launcher handoffs under the handoff procedure.
5. Update `.syndicate/checkpoint.md` with goal, decisions, evidence, tests/reviews,
   remaining tasks, active workers and next action. Include the actual role,
   provider, participant ID, and outcome for each specialist who performed the
   work, and distinguish any unstarted roles. Stop completed workers. Write this
   before committing closeout and report a failed checkpoint write explicitly.
6. Commit task state, progress, completion timestamp and Chief notification using:

   ```sh
   syndicate tasks close TASK_ID -C SOURCE --session SESSION_ID --state done \
     --implementation IMPLEMENTATION_ROLE ACTUAL_PROVIDER IMPLEMENTER_ID \
     --review REVIEW_ROLE ACTUAL_PROVIDER REVIEWER_ID \
     --progress "Delivered PR URL, tests and review results, and remaining work" \
     --message "Outcome, evidence, limitations and requester next action"
   ```

   Use returned native agent IDs or underlying specialist completion IDs from
   relays, and repeat each flag when multiple specialists performed that work.
   Include completed participants in incomplete closeouts as well. For a required
   role that could not start, use `--state blocked --role-blocker ROLE "actual launch failure"`
   and omit the unavailable attempt from the participant flags. Supply only
   observed work and report verification results in the progress and message.

   This atomic command includes the durable message normally sent with
   `syndicate messages send`; do not send a duplicate. done/ended receive
   completed_at; waiting_on_user/blocked remain incomplete with completed_at null.
   The command atomically updates effective status through the portfolio overlay
   without rewriting agent-owned status.json. Use `tasks list --json` or briefing
   to read effective state. Identical retries while active return the same
   record. All four closeout states are final for that session, including
   waiting_on_user and blocked. A later resumed session has a new token. Do not claim
   closeout succeeded when the command failed. PID visibility failures require
   the normal outside-sandbox approval path, never bypassing ownership checks.
7. Confirm the returned record and task state, then give the requester a concise
   result with evidence and outstanding action. If the command failed, identify the error and remaining action.
