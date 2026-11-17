---
name: syndicate-closeout
description: Close a managed Syndicate project session with verified outcomes, accurate task state, durable checkpoint and a Chief of Staff notification. Run before the orchestrator ends its session on Claude or Codex.
---

# Close a Syndicate session

Requester-reported external outcomes must be verified before confirming them.

Run this ordered checklist before the final response. SOURCE is the launch
source containing status.json; use the supplied executable prefix. Preserve
current.task_id, current.session_id and all launcher ownership fields.

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
4. Choose an accurate state: done only when all requested work and reviews are
   complete; ended for a session ending with incomplete work; waiting_on_user for
   a specific pending answer; blocked for an identified obstacle. Keep a truthful
   progress note. Do not mark a deliberate launcher handoff ended: checkpoint it
   and preserve its resumable state under the handoff procedure.
5. Update `.syndicate/checkpoint.md` with goal, decisions, evidence, tests/reviews,
   remaining tasks, active workers and next action. Stop completed workers. Write
   this before committing closeout; report a failed checkpoint write explicitly.
6. Commit task state, progress, completion timestamp and Chief notification using:

   ```sh
   syndicate tasks close TASK_ID -C SOURCE --session SESSION_ID --state done \
     --progress "Delivered PR URL; tests and reviews passed; remaining work: none" \
     --message "Completed TASK_ID: PR URL, validation, limitations and requester next action"
   ```

   This atomic command includes the durable message normally sent with
   `syndicate messages send`; do not send a duplicate. done/ended receive
   completed_at; waiting_on_user/blocked remain incomplete with completed_at null.
   The command atomically updates effective status through the portfolio overlay
   without rewriting agent-owned status.json. Use `tasks list --json` or briefing
   to read effective state. Identical retries while active return the same
   record; changed closeout content is rejected. A later resumed session has a new token. Do not claim
   closeout succeeded when the command failed. PID visibility failures require
   the normal outside-sandbox approval path, never bypassing ownership checks.
7. Confirm the returned record and task state, then give the requester a concise
   result with evidence and outstanding action. If the command failed, identify the error and remaining action.
