# Configuration

Syndicate has one **global team** (the roles in this repo's `roles/`) and any
number of **projects**. A project customizes the team through a single
directory, `<project>/.syndicate/`, which is kept out of git. Nothing else in
the project is created or modified.

```
<project>/
└── .syndicate/                 local-only, git-excluded, safe to delete
    ├── config                  which CLI runs each role, plus options
    ├── roles/
    │   ├── <role>.md           replaces (or adds) a role for this project
    │   └── <role>.append.md    adds project-specific guidance to a role
    ├── status.json             current state + history, for dashboard/web; never edit
    └── .generated/             written by syndicate on every run; never edit
```

`status.json` is written by `bin/syndicate` (an initial stub, before it launches
the CLI) and then kept current by the orchestrator role itself as it works. It's
how `syndicate dashboard`/`syndicate web` know what's happening — see the
[README's Monitoring section](../README.md#monitoring). The one piece of state
that lives outside the project entirely is `~/.syndicate/projects.json`, a
user-wide registry of every project syndicate has touched, so the dashboard can
find them all without you listing them.

Run `syndicate init` inside a project to create `config` and `roles/` and the
git exclusion. Every file below is optional: with no `.syndicate/` at all, the
project runs the global team on Claude Code.

Use `syndicate status -C <project>` at any time to see the resolved team: each
role's CLI and whether its definition is global, overridden, appended to, or
project-only.

## Files to create in a project

### `.syndicate/config`: which CLI runs each role

One `key = value` per line. `#` starts a comment.

| Key | Values | Meaning |
|---|---|---|
| `handoff` | `on` \| `off` | Managed quota monitoring and automatic handoff (default `on`). |
| `handoff-reserve` | number between 0 and 100, exclusive | Account usage remaining at which to stop (default `10`). |
| `default` | `claude` \| `codex` | CLI for every role not listed individually. |
| `orchestrator` | `claude` \| `codex` | CLI that **hosts the session**. Every other role is delegated to from here. |
| `<role name>` | `claude` \| `codex` | CLI for that one role: any global role, or a project-only role you defined. |
| `review-gate` | `on` (default) \| `off` | Block finishing until `code-reviewer` has reviewed delegated work. |

```
# Everything on Claude, but reviews come from Codex:
default = claude
code-reviewer = codex
```

An unknown role name or CLI is an error, not a silent fallback. Precedence for
the host CLI: `-x` flag > `orchestrator` in the config > `default` >
`$SYNDICATE_RUNTIME` > `claude`. For any other role: its own key > `default` >
`$SYNDICATE_RUNTIME` > `claude`.

### `.syndicate/roles/<role>.md`: replace a role, or add a new one

The file has the same format as the files in `roles/`: optional frontmatter,
then the body.

```
---
description: Use this agent for backend work in this Django monolith.
tools: Read, Edit, Write, Bash, Grep, Glob
---

# Backend Engineer
...whole replacement identity...
```

- If a global role with the same name exists, the **body is replaced
  entirely**. Frontmatter fields you leave out (`description`, `tools`) are
  **inherited from the global role**; fields you set win.
- If no global role has that name, this **adds a project-only role**. The
  orchestrator can delegate to it like any other, and you can give it a CLI in
  `config`. Names are lowercase letters, digits and dashes.
- `tools` is a comma-separated list of Claude Code tool names (`Read, Edit,
  Write, Bash, Grep, Glob, WebFetch, WebSearch, Agent`). It limits the role
  under Claude, and decides its Codex sandbox: any of `Edit`/`Write` gives
  `workspace-write`, otherwise `read-only`. Omit it and the role gets full
  tools / `workspace-write`.

### `.syndicate/roles/<role>.append.md`: add guidance without forking the role

Plain markdown, no frontmatter needed. It is appended to the role's body under
a "Project-Specific Guidance" heading, on top of the global role **or** your
override. Prefer this over a full replacement: you keep receiving improvements
made to the global role.

```
This project is a Django 5 monolith. Use pytest (never unittest), keep views
thin, and put business logic in services/.
```

## Precedence at a glance

| Layer | Where | Effect |
|---|---|---|
| Global role | `<syndicate repo>/roles/<role>.md` | The default definition for every project. |
| Project override | `.syndicate/roles/<role>.md` | Replaces the body; merges frontmatter. |
| Project append | `.syndicate/roles/<role>.append.md` | Adds guidance to whatever is below it. |
| CLI per role | `.syndicate/config` | Chooses claude or codex for that role. |

## Mixed-CLI teams

The orchestrator runs in one CLI (the host) and can only spawn that CLI's own
agents. A role configured for the *other* CLI appears in the host as a thin
**relay agent** with the same name and description. When the orchestrator
delegates to it, the relay runs `syndicate run-role <role>`, which executes the
real role headlessly on its own CLI and hands the output back. Because the relay
keeps the role's name, delegation and the review gate work identically.

Things to know:

- **Claude host, Codex role.** The generated settings allow the relay's exact
  command (`Bash(<syndicate> run-role *)`) so it doesn't prompt every time.
- **Codex host, Claude role.** Codex's sandbox blocks network by default, so the
  relayed `claude` couldn't reach its API. When a Claude-backed role exists under
  a Codex host, syndicate launches Codex with
  `sandbox_workspace_write.network_access=true`. That grants network access to
  every `workspace-write` agent in the session, not just the relay.
- **Relayed roles start with only their brief.** They can't see the
  conversation, so the relay writes a self-contained brief.
- **Headless Claude roles** run with `--permission-mode acceptEdits` if the role
  can write, otherwise default permissions, plus whatever your own Claude
  settings allow. A read-only reviewer that wants to run a command your settings
  don't allow will be denied rather than prompting.

## Keeping it local-only

- The project footprint is `.syndicate/` and nothing else. Syndicate injects
  agents at launch (Claude via `--agents`/`--settings`, Codex via `-c`), so
  `.claude/` and `.codex/` are never touched and can't collide with another
  team's setup.
- `.syndicate/` is excluded through the repository's **local-only**
  `.git/info/exclude` (correct for worktrees and for projects in a repo
  subdirectory), never `.gitignore`, so nothing is added to a tracked file.
  `syndicate init` and every run make sure the entry exists.
- If `.syndicate/` files are already tracked, syndicate warns and tells you to
  `git rm -r --cached .syndicate`.
- Outside a git repository there is nothing to exclude and syndicate says so.
- To remove everything: delete `.syndicate/` (and, if you like, the
  `# syndicate` lines in `.git/info/exclude`).

## Codex review gate (one-time, per machine)

Codex only runs hooks from a reviewed `hooks.json`, and it can't take them
inline. So the Codex gate is a single **global** hook rather than a project file:

```sh
syndicate setup-codex-gate    # merges one Stop entry into ~/.codex/hooks.json
```

Then open `codex` once and accept the hook review prompt. The hook only acts
inside a syndicate project (a `.syndicate/` directory at or above the session's
working directory) and honors `review-gate = off`. Remove it with
`syndicate setup-codex-gate --remove`. Claude needs no setup; its hook is passed
at launch.

## Migrating from the old `install.sh` layout

Earlier versions copied agents into `.claude/agents/`, `.codex/agents/`, added
hooks under `.claude/` and `.codex/`, and merged a Stop entry into
`.claude/settings.json` / `.codex/hooks.json`. None of that is used any more.
In a project you installed into, delete the copied files (`.claude/agents/*.md`,
`.claude/hooks/require-review.sh`, `.codex/agents/*.toml`,
`.codex/hooks/require-review.sh`) and remove the `require-review.sh` Stop entry
from `.claude/settings.json` and `.codex/hooks.json`, then run `syndicate init`.
Check `git status` first so you only remove files syndicate created.

## Troubleshooting

- `syndicate status -C <project>`: shows the resolved team and any warnings
  (dangling `.append.md` files, invalid role names).
- `unknown role 'x'` in `config`: the role doesn't exist globally or under
  `.syndicate/roles/`. Check spelling with `status`.
- Generated files are in `.syndicate/.generated/`: `claude-settings.json` and
  `codex/*.toml`. They are rewritten on every run, so edit the sources instead.

## Usage-aware handoffs

Managed runs inject the bundled handoff skill and maintain a durable checkpoint
and session-output log. `--reserve` overrides `handoff-reserve` for one run.
`--handoff <directory>` loads an interrupted task; pair `-x codex` with
`--all-runtime codex` (or Claude equivalents) to move all roles together.

Account observations and a single background reset monitor live under
`~/.syndicate/limits/`. See [Usage-aware handoffs](handoff.md) for the lifecycle,
telemetry limitations, notification behavior, and monitor shutdown command.
