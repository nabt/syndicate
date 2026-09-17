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
- **No generation step, no adapter layer.** Earlier drafts of this repo
  split each role into a generic identity file plus a generated Claude
  Code file, glued together by a build script. That's two files to keep
  in sync per role — real drift risk if the generated file is ever
  hand-edited or the generator forgotten. Collapsing to one file per role
  removes that failure mode entirely: edit the file, you're done.

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

# Symlink instead of copy, so edits to this repo show up immediately
./install.sh --target ~/code/some-project --symlink
```

Run `./install.sh --help` for the full option list.

Once installed, invoke a role in Claude Code the same way as any subagent —
by name, or let Claude Code auto-select one based on its `description`.

## Using a role outside Claude Code

Read the body of `roles/<role>.md` (everything after the closing `---`) and
use it as that role's system prompt in whatever framework or raw API call
you're driving. The frontmatter is optional context, not a dependency.

## Adding or changing a role

See [`docs/adding-a-role.md`](docs/adding-a-role.md). In short: copy an
existing role file as a template, adjust its identity, and reinstall.
There's nothing to regenerate or rebuild.
