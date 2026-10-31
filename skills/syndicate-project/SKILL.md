---
name: syndicate-project
description: Ask where to assign a Syndicate task started outside a Git repo, and record the user's project choice. Use when the current Syndicate task has needs_project set; skip when a repo or --project already determined the assignment.
---

# Assign a Syndicate task

Read `.syndicate/status.json` in the launch directory. Find the task matching
`current.task_id`. If `needs_project` is false or absent, no question is needed.
Check `syndicate tasks list --json` for its current `needs_project` flag first: a management edit
may already have assigned it without modifying the agent-owned status file.

For an unassigned task, use `syndicate dashboard --list` to find known repos.
Ask the user which repo should own this task, offering relevant repo names,
a repo path, or “Keep in Generic tasks.” Do not infer consent from silence.
In headless mode or without a response, leave it in Generic tasks and report
that assignment is pending. Independent work may continue in the launch
directory; do not edit a different repository based on an assumed assignment.

After an explicit answer, run:

```sh
syndicate tasks edit TASK_ID --source LAUNCH_DIRECTORY --project REPO_PATH
```

Use `--project generic` when the user chooses Generic tasks. Quote paths and
task identifiers as shell arguments. Confirm the resulting assignment using
`syndicate tasks list`. Assignment changes dashboard ownership only: keep
status and checkpoint writes in the original launch directory. If work needs
to continue in the chosen repo, use that repo explicitly for file operations.
Remember the choice for this task and do not ask again on continuation.
