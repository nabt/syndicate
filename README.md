# Syndicate

A self-contained, portable roster of software engineering agent identities.
Each role is a single markdown file — mission, responsibilities, operating
principles, stack-adaptation rules, definition of done, and a handoff
protocol — that a coding agent can adopt to act as that role on any project,
in whatever language or framework the project actually uses.

Roles are swappable: install the ones a given project needs, skip the ones
it doesn't, and use them across as many separate projects as you want
without copying prompts by hand each time.

## Roster

| Role | File | Summary |
|---|---|---|
| Orchestrator (Tech Lead) | [`roles/orchestrator.md`](roles/orchestrator.md) | Plans a project, decides which roles it needs, delegates, and integrates the result. |
| Backend Engineer | [`roles/backend-engineer.md`](roles/backend-engineer.md) | APIs, business logic, data layer — any backend language/framework. |
| Frontend Engineer | [`roles/frontend-engineer.md`](roles/frontend-engineer.md) | Client-side UI — web, mobile, or desktop, any framework. |
| DevOps / Infrastructure Engineer | [`roles/devops-engineer.md`](roles/devops-engineer.md) | CI/CD, infra-as-code, containers, deployment, observability. |
| QA / Test Engineer | [`roles/qa-engineer.md`](roles/qa-engineer.md) | Automated tests, bug repro, test strategy. |
| Code Reviewer | [`roles/code-reviewer.md`](roles/code-reviewer.md) | Independent, read-only correctness and consistency review. |
| Security Engineer | [`roles/security-engineer.md`](roles/security-engineer.md) | Threat modeling, vuln review, defensive security only. |
| Technical Writer | [`roles/technical-writer.md`](roles/technical-writer.md) | READMEs, API docs, runbooks, ADRs. |

## Design

- **One file per role, framework-agnostic by construction.** The body of
  each file — everything below the frontmatter — is a portable system
  prompt: mission, responsibilities, operating principles, how the role
  adapts to whatever stack it finds, its definition of done, what it
  explicitly won't do, and who it hands off to. Drop that body into any
  agent framework (a raw system prompt, CrewAI, AutoGen, a custom harness)
  and it works unmodified.
- **The frontmatter is Claude Code-native** (`name`, `description`,
  `tools`) so these files work as [Claude Code
  subagents](https://docs.claude.com/en/docs/claude-code/sub-agents) with
  zero translation — just copy them into a project's `.claude/agents/`.
  A different framework can ignore those three lines, or map `tools:` to
  its own permission model; nothing about the identity itself depends on
  them.
- **No committed adapter layer.** Earlier drafts split each role into a
  generic identity file plus a generated Claude Code file, glued together
  by a build script — two files per role to keep in sync. Now `roles/*.md`
  is the only copy. Claude Code uses it as-is; the Codex runtime needs
  `.toml` agent files, which `install.sh` generates *into the target
  project* at install time (never committed here), so there's nothing
  to drift. Edit a role, reinstall, done.
- **Two runtimes:** Claude Code and Codex CLI. Pick per install
  (`--runtime`) and per run (`bin/syndicate -x`).

## Install

Copy (or symlink) the roles you want into a project's `.claude/agents/`
directory:

```sh
# Install every role into the current project
./install.sh

# Install into a specific project
./install.sh --target ~/code/some-project

# Install only the roles a given project actually needs
./install.sh --target ~/code/some-project --roles orchestrator,backend-engineer,qa-engineer,code-reviewer

# Install once, user-wide, so every project can use these roles
./install.sh --global

# Symlink instead of copy, so edits to this repo show up immediately (claude only)
./install.sh --target ~/code/some-project --symlink

# Codex CLI instead of (or as well as) Claude Code: writes .codex/agents/*.toml
./install.sh --target ~/code/some-project --runtime codex
./install.sh --target ~/code/some-project --runtime both
```

For Codex, roles without edit tools (the code reviewer) get a `read-only`
sandbox; the rest get `workspace-write`. Codex needs `jq` at install time.

Run `./install.sh --help` for the full option list.

Once installed, invoke a role in Claude Code the same way as any subagent —
by name, or let Claude Code auto-select one based on its `description`.

## Running the team

Run the orchestrator as the **main session agent**, not as a subagent —
Claude Code subagents can't spawn other subagents, so an orchestrator
invoked as one couldn't delegate to the specialists.

```sh
# Interactive
bin/syndicate -C ~/code/some-project "add rate limiting to the public API"

# Headless (prints the result and exits; suitable for scripts/CI)
bin/syndicate -C ~/code/some-project -p "add rate limiting to the public API"
```

`bin/syndicate` is a thin wrapper over `claude --agent orchestrator`; put it
on your `PATH` if you like. The orchestrator decides which roles the task
needs and delegates to them.

To drive Codex instead, use `-x codex` (or set `SYNDICATE_RUNTIME=codex`):

```sh
bin/syndicate -x codex -C ~/code/some-project -p "add rate limiting to the public API"
```

Codex has no `--agent` flag, so the launcher injects the orchestrator's
identity as developer instructions and the installed specialists are spawned
as Codex custom agents. Headless Codex runs use `workspace-write`. Extra
arguments after the task are passed through to whichever CLI is running.

### Review gate (optional)

The role docs say work must pass through Code Reviewer, but that's only an
instruction. Install the Stop hook to enforce it:

```sh
./install.sh --target ~/code/some-project --review-gate
```

If the orchestrator delegated work to any build role during a session and
never invoked `code-reviewer`, the hook blocks the session from finishing and
tells it to get a review first. It only triggers on delegation, so ordinary
sessions in the same project are unaffected. Requires `jq`.

It works for both runtimes: `--review-gate` writes the Claude Code hook to
`.claude/settings.json` and, with `--runtime codex` or `both`, the Codex hook
to `.codex/hooks.json`. The same script reads either transcript format (Claude
`Agent` calls or Codex `spawn_agent` calls). The Codex hook command is an
absolute path, so re-run `install.sh` if you move the project.

**Codex asks you to approve hooks once.** Codex won't run a project hook until
it's reviewed: open `codex` interactively in the project and accept the hook
review prompt (or use its `/hooks` menu). Until then the gate is inert. This
is Codex's own safety check, and the installer deliberately doesn't bypass it.

## Using a role outside Claude Code

Read the body of `roles/<role>.md` (everything after the closing `---`) and
use it as that role's system prompt in whatever framework or raw API call
you're driving. The frontmatter is optional context, not a dependency.

## Adding or changing a role

See [`docs/adding-a-role.md`](docs/adding-a-role.md). In short: copy an
existing role file as a template, adjust its identity, and reinstall.
There's nothing to regenerate or rebuild.
