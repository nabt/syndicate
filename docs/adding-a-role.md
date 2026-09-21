# Adding or changing a role

Roles come in two scopes.

- **Global** roles live in this repo's `roles/` and apply to every project.
- **Project** roles and overrides live in `<project>/.syndicate/roles/` and are
  local-only. See [configuration.md](configuration.md).

There is no build or install step for either: edit the file and the next
`syndicate` run uses it.

## Adding a global role

1. Copy a role close in shape to what you need (`roles/backend-engineer.md`
   for a build role, `roles/code-reviewer.md` for a read-only one) to
   `roles/<new-role>.md`. The name is lowercase letters, digits and dashes.
2. Set the frontmatter:
   - `name`: matches the filename.
   - `description`: states plainly when the orchestrator should use this role.
   - `tools`: the Claude Code tools it needs, kept minimal. A role that only
     reviews shouldn't have `Edit`/`Write`; that also gives it a `read-only`
     sandbox under Codex. Available: `Read`, `Edit`, `Write`, `Bash`, `Grep`,
     `Glob`, `WebFetch`, `WebSearch`, `Agent`.
3. Write the body using the same sections as the existing roles, so they read
   consistently: Mission, Core Responsibilities, Operating Principles, Stack
   Adaptation, Definition of Done, Anti-Goals, Handoff Protocol.
4. If it delegates to or receives work from other roles, update their Handoff
   Protocol sections too.

## Changing or removing a global role

Edit or delete `roles/<role>.md`. Remove references to a deleted role from other
roles' Handoff Protocol sections, and from any project's `.syndicate/config`
(an unknown role name there is an error).

## Customizing a role for one project only

Don't edit the global file. In the project, either add
`.syndicate/roles/<role>.append.md` (guidance layered on the global role) or
`.syndicate/roles/<role>.md` (a full replacement). See
[configuration.md](configuration.md) for exactly how each behaves.
