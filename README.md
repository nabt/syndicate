# Syndicate

A global team of software-engineering agent roles that you point at any
project, on Claude Code, Codex CLI, or both at once. Each role is a single
markdown file (mission, responsibilities, operating principles, stack-adaptation
rules, definition of done, handoff protocol) that adapts to whatever language
and framework the project uses.

- **One global team.** The roles in [`roles/`](roles) are available to every
  project. Nothing is installed into a project.
- **Per-project overrides.** A project can replace a role, add guidance to one,
  or define new roles of its own.
- **Per-role CLI.** Choose Claude Code or Codex for each role, per project. For
  example, `code-reviewer` on Codex while everything else runs on Claude.
- **Local-only.** A project's footprint is one git-excluded `.syndicate/`
  directory. Nothing is committed, and other teams' `.claude/` and `.codex/`
  setups are never touched, so several teams can work in the same repo without
  interfering.
- **Monitorable.** The orchestrator keeps `.syndicate/status.json` up to date
  as it works, so you can watch what every project's team is doing — and
  whether it's stuck waiting on you — from a console or browser dashboard.

## Roster

| Role | File | Summary |
|---|---|---|
| Orchestrator (Tech Lead) | [`roles/orchestrator.md`](roles/orchestrator.md) | Plans a project, decides which roles it needs, delegates, and integrates the result. |
| Backend Engineer | [`roles/backend-engineer.md`](roles/backend-engineer.md) | APIs, business logic, data layer, in any backend language/framework. |
| Frontend Engineer | [`roles/frontend-engineer.md`](roles/frontend-engineer.md) | Client-side UI: web, mobile, or desktop, any framework. |
| DevOps / Infrastructure Engineer | [`roles/devops-engineer.md`](roles/devops-engineer.md) | CI/CD, infra-as-code, containers, deployment, observability. |
| QA / Test Engineer | [`roles/qa-engineer.md`](roles/qa-engineer.md) | Automated tests, bug repro, test strategy. |
| Code Reviewer | [`roles/code-reviewer.md`](roles/code-reviewer.md) | Independent, read-only correctness and consistency review. |
| Security Engineer | [`roles/security-engineer.md`](roles/security-engineer.md) | Threat modeling, vuln review, defensive security only. |
| Technical Writer | [`roles/technical-writer.md`](roles/technical-writer.md) | READMEs, API docs, runbooks, ADRs. |

## Setup (once per machine)

Requires `python3`, the `claude` and/or `codex` CLI, and `jq` (for the review
gate). Put the launcher on your `PATH`:

```sh
ln -s "$PWD/bin/syndicate" ~/.local/bin/syndicate
```

If you use Codex and want the review gate there, run `syndicate
setup-codex-gate` once and approve the hook in Codex (see
[Review gate](#review-gate)).

## Use

```sh
# Run the orchestrator on a project (interactive)
syndicate -C ~/code/some-project "add rate limiting to the public API"

# Headless: prints the result and exits
syndicate -C ~/code/some-project -p "add rate limiting to the public API"

# Host the session on Codex for this run
syndicate -x codex -C ~/code/some-project "..."

syndicate init   -C ~/code/some-project   # create .syndicate/ (optional)
syndicate status -C ~/code/some-project   # show each role's CLI and definition source
```

Run from inside a project, `-C` can be omitted. The orchestrator decides which
roles the task needs and delegates to them. Extra arguments after the task are
passed to the underlying CLI.

## Customizing a project

Everything lives in `<project>/.syndicate/`, created by `syndicate init` and
excluded from git automatically:

```
.syndicate/config                  # which CLI runs each role
.syndicate/roles/<role>.md         # replace a role (or add a new one)
.syndicate/roles/<role>.append.md  # add project-specific guidance to a role
```

```
# .syndicate/config: reviews come from Codex, everything else from Claude
default = claude
code-reviewer = codex
```

The full reference (every file, every key, precedence, how mixed-CLI teams
work, and how the local-only guarantee is kept) is in
[`docs/configuration.md`](docs/configuration.md). To add or change a global
role, see [`docs/adding-a-role.md`](docs/adding-a-role.md).

## How mixed-CLI teams work

The orchestrator runs in one CLI and can only spawn that CLI's agents. A role
assigned to the *other* CLI shows up in the host as a thin relay agent with the
same name; delegating to it runs `syndicate run-role <role>`, which executes the
real role headlessly on its own CLI and returns the output. Roles are injected
at launch (Claude via `--agents`, Codex via `-c`), so no agent files are written
into the project. Note that a Codex host with a Claude-backed role enables
network access in Codex's sandbox for the session; see
[`docs/configuration.md`](docs/configuration.md#mixed-cli-teams).

## Usage-aware handoffs

Syndicate monitors account usage and saves a handoff at **10% remaining** by
default. It stops the managed team, then asks whether to continue with the
other CLI. A confirmed switch moves every role for that run. The bundled
handoff skill keeps a checkpoint so decisions and pending work survive.

```sh
syndicate --reserve 20 -C ~/code/some-project "your task"
syndicate limits --watch
```

Unknown usage pauses work after a grace period. Claude telemetry depends on
an interactive status line; headless Claude cannot supply it on its own.
Read [Usage-aware handoffs](docs/handoff.md) for setup, recovery commands,
provider limitations, and reset notifications.

## Monitoring

Every run registers its project in `~/.syndicate/projects.json` and keeps
`.syndicate/status.json` updated: current state (`running`,
`waiting_on_user`, or `done`), which role is active and what it's doing, and
a running history of past rounds. Two ways to watch it, across every
registered project at once:

```sh
# Console: refreshes every 2s, ctrl-c to stop. --once renders a single frame.
syndicate dashboard

# Browser: starts a local server and prints the URL (http://127.0.0.1:8765/ by default)
syndicate web
```

A project whose status still says `running` but whose process has died shows
up as `stale` rather than silently looking active forever — the dashboard
checks the recorded PID is actually still alive. This is also why you should
trust the dashboard over a status file you read by hand: a crashed or killed
session leaves no other signal that it stopped.

## Review gate

The role docs say work must pass through the code reviewer. The review gate
enforces it: if the orchestrator delegated work to any build role and never
invoked `code-reviewer`, finishing is blocked until it does. It's on by default
and controlled per project with `review-gate = on|off` in `.syndicate/config`.

- **Claude Code:** nothing to set up; the hook is passed at launch.
- **Codex:** run `syndicate setup-codex-gate` once, then open `codex` and accept
  the hook review prompt. Codex only runs hooks you've reviewed, and
  syndicate deliberately doesn't bypass that. The hook acts only inside
  syndicate projects. Remove it with `syndicate setup-codex-gate --remove`.

## Design

- **One source per role.** `roles/*.md` is the only copy. Nothing derived is
  committed or installed, so nothing drifts: edit a role and the next run uses it.
- **Framework-agnostic bodies.** Everything below a role's frontmatter is a
  portable system prompt that works in any agent framework. The frontmatter
  (`name`, `description`, `tools`) is Claude Code-shaped and is translated for
  Codex at launch.

## Tests

```sh
python3 -m unittest discover -s tests
```
