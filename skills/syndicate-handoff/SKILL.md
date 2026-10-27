---
name: syndicate-handoff
description: Maintain a durable checkpoint for Syndicate work and continue it across Claude Code and Codex CLI when account usage reaches the configured reserve. Use within a Syndicate-managed project or when explicitly handing off its work.
---

# Syndicate handoff

The launcher monitors account usage and owns stopping processes and asking the
user to switch. A prompt cannot measure subscription quotas or guarantee a stop.
Do not estimate account availability from context tokens, cost, or message count.

At the beginning of work and after each specialist returns, update
`.syndicate/checkpoint.md` with the current goal, constraints, decisions,
completed work, outstanding tasks, active workers, relevant files, tests and
review results, and the next useful action. Keep it concise and self-contained.
Record failed or unrun checks honestly. Preserve unresolved user requests.
This file is the semantic checkpoint if the launcher must interrupt you without
another model response. Update it before launching a long task as well.

When asked to continue a handoff, read the handoff directory's `handoff.md`,
`checkpoint.md`, and `status.json`, then inspect the current worktree and diff.
The snapshot can contain incomplete writes or interrupted tools; verify them.
A handoff is not completion or review approval. Carry outstanding reviews into
the new run. Do not assume the old conversation or native agent IDs transferred.

The launcher asks after saving the snapshot and stopping its managed process
group. Only an explicit user choice may start another provider. EOF, a timeout,
non-interactive input, or an unavailable target is not consent. Do not launch
another CLI yourself or restart old workers. The target run uses that provider
for every role to avoid continuing to consume the depleted provider's quota.

`.syndicate/limits` in the user's home stores last-observed account usage,
observation times, and reset notifications. Unknown and stale values are not
available capacity. A reset time passing is only an expected reset; fresh
provider telemetry is needed to confirm it. Monitoring must be running to
notify. Never read authentication files or use undocumented quota endpoints.
