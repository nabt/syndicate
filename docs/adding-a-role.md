# Adding or changing a role

Every role is one file under `roles/`. There's no build step — edit the
file, reinstall, done.

## Adding a new role

1. Copy an existing role file close in shape to what you need (e.g.
   `roles/backend-engineer.md` for another build role, `roles/code-reviewer.md`
   for another read-only review role) to `roles/<new-role>.md`.
2. Update the frontmatter:
   - `name`: kebab-case identifier, matches the filename.
   - `description`: written for Claude Code's auto-invocation — state
     plainly what situations should trigger this agent. Look at the other
     roles' descriptions for the pattern.
   - `tools`: the Claude Code tools this role actually needs. Keep it
     minimal — a role that only reviews shouldn't get `Edit`/`Write`; a
     role that never touches the network shouldn't get `WebFetch`/
     `WebSearch`. Available tool names: `Read`, `Edit`, `Write`, `Bash`,
     `Grep`, `Glob`, `WebFetch`, `WebSearch`, `Agent` (for a role that
     delegates to other roles).
3. Rewrite the body to match this repo's existing section structure, so
   every role reads consistently:
   - `## Mission` — one to two sentences.
   - `## Core Responsibilities` — what it actually does.
   - `## Operating Principles` — how it makes judgment calls.
   - `## Stack Adaptation` — how it detects and matches the project's
     existing language/framework/tooling instead of imposing a default.
   - `## Definition of Done` — what "finished" means for this role.
   - `## Anti-Goals` — what it explicitly stays out of, including where
     it overlaps with another role.
   - `## Handoff Protocol` — inputs expected, outputs produced, and which
     roles it hands off to or receives from.
4. If the new role delegates to or receives work from others, update the
   relevant `Handoff Protocol` sections of those roles too (e.g. adding
   an "architect" role that the orchestrator delegates to before build
   roles start).
5. Reinstall: `./install.sh --target <project>` (or `--global`).

## Changing an existing role

Edit the file directly. If you only changed the body (mission,
responsibilities, principles, etc.), nothing else needs updating. If you
changed what tools the role needs, update the `tools:` frontmatter line to
match. Reinstall to pick up the change in any project using a copied
(non-symlinked) install.

## Removing a role

Delete `roles/<role>.md`, and remove references to it from other roles'
`Handoff Protocol` sections if any exist. Re-run `install.sh` on any
project that had it installed to remove the stale copy, or delete
`.claude/agents/<role>.md` there directly.
