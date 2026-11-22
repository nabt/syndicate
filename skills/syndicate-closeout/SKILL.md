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

## Task worktrees

Create each project task's worktree with `syndicate tasks worktree TASK_ID -C
SOURCE --repo REPO --branch TASK_BRANCH` before any edits, never under a
temporary directory, and keep task work out of the main checkout. Commit work
in progress to the task branch after each specialist returns; push `work/*`
branches once a PR is intended. Leave the worktree and its Syndicate lock in
place at every stop, including handoffs, quota stops, and session exit. Only
post-merge cleanup unlocks and removes it.

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
2. Verify external outcomes before confirming them. If the current task has a
   PR, run the post-merge check before committing closeout:

   ```sh
   syndicate tasks cleanup-pr NUMBER -C REPO --branch TASK_BRANCH --worktree TASK_WORKTREE --task TASK_ID
   ```

   Take the PR, branch and absolute linked worktree path from the current
   task's checkpoint. Use a repository checkout outside the task worktree and
   make sure the launch source and active workers are outside it too. Keep
   both checkouts idle during cleanup. Stop editors, builds, other writers,
   and concurrent Git operations in either checkout before running it.
   If provenance is missing or ambiguous, stop cleanup and report the blocker.
   Use the helper only for the current task, including when the requester
   reports its merge. It checks GitHub PR state and checks that the merge
   commit is on fetched `origin/main` before removing any task artifacts.
   Open and closed-unmerged PRs leave Git artifacts alone. For other tasks'
   reported merges, use `gh pr view NUMBER --json state,mergedAt,mergeCommit,url`
   in the correct repository and verify the commit on fetched `origin/main`.

   A merged PR must match the recorded same-repository task branch and target
   `main`. For a merge commit, the helper requires the task branch tips to be
   reachable from both fetched `origin/main` and the PR head reported by
   GitHub. For a squash or rebase merge, every local and remote task branch tip
   must equal the PR head exactly, and every path the PR changed (including
   deletions, renames, mode and submodule changes) must be identical in the
   merge commit. Otherwise it preserves the branches and worktree; report why
   cleanup stopped. A dirty task worktree, including its
   ignored files, stops cleanup. The helper accepts the worktree's Syndicate
   lock, unlocks it only immediately before removal, and stops on any other
   lock. While another task registered to the same series (in any of its
   worktrees) is not done or ended, it stops before changing anything and
   leaves the worktree, lock and branches intact; report that the series is
   still active. Close a sibling task only if it was really abandoned, with
   `syndicate tasks edit TASK_ID --source SOURCE --state ended`; ask the user
   before editing the registry for a task reported as not found. An unreadable
   registry, or a Syndicate-locked worktree without a registry entry, also
   stops cleanup. After removal the helper deletes the branch's handoff
   snapshot refs and reports each one. A remote branch that is already absent is
   safe to skip. Remote deletion is conditional on the inspected tip still
   being current. The helper uses a deletion-only refspec with an explicit
   lease and never force-updates a branch. The existing local main checkout
   must be clean and able to fast-forward. The helper never switches a checkout
   to main, resets main, or overwrites local files.

   Record each outcome and any partial cleanup or blocker in the task progress
   and checkpoint. Preserve blocked artifacts and report the reason without
   forcing removal or synchronization. If cleanup is blocked after a verified
   merge, report the merge and the cleanup blocker separately.

   Record URL, state, time/commit, tests actually run, review results
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
