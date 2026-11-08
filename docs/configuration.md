# Configuration

Syndicate has one **global team** (the roles in this repo's `roles/`) and any
number of **projects**. Settings merge from built-in defaults, then the
user-wide file, then the project file. A project customizes the team through a
single directory, `<project>/.syndicate/`, which is kept out of git. Nothing
else in the project is created or modified.

```
<project>/
└── .syndicate/                 local-only, git-excluded, safe to delete
    ├── syndicate.conf          which CLI runs each role, plus options
    ├── roles/
    │   ├── <role>.md           replaces (or adds) a role for this project
    │   └── <role>.append.md    adds project-specific guidance to a role
    ├── status.json             current state + history, for dashboard/web; never edit
    └── .generated/             written by syndicate on every run; never edit
```

`status.json` is written by `bin/syndicate` (an initial stub, before it launches
the CLI) and then kept current by the orchestrator role itself as it works. It's
how `syndicate dashboard`/`syndicate web` know what's happening — see the
[README's Monitoring section](../README.md#monitoring). Shared state also lives under `~/.syndicate/`: `projects.json` discovers
registered projects, `task-edits.json` stores task-management overrides, and
`portfolio/` holds decisions, project briefs, ownership, team requests, and the
isolated Chief of Staff workspace. See the [Chief of Staff guide](chief-of-staff.md)
for its separate launch path; project role/runtime settings do not configure it.

Run `syndicate team init` inside a project to create `syndicate.conf` and `roles/` and the
git exclusion. Every file below is optional: with no `.syndicate/` at all, the
project runs the global team on Claude Code, plus any user-wide settings.

`syndicate config` prints the merged settings for the project whose
`.syndicate/status.json` was updated last. `-C <project>` selects a project.
Winning keys are grouped under the file or layer that supplied them.
`syndicate config --local` prints every layer that was read, including keys a
later layer replaced, and marks `<project>/.syndicate/syndicate.conf` with `*`.
`syndicate config --global` prints only the user-wide `syndicate.conf`.

## Settings files

Both files use the same `key = value` syntax. `#` starts a comment. Syndicate
reads them on every run and does not copy one onto the other.

| Layer | Where | Effect |
|---|---|---|
| Built-in default | in `bin/syndicate` | `default = claude`, `handoff = on`, `handoff-reserve = 10`, `review-gate = on`, `inbox-interval = 5`, `inbox-transport = auto`. |
| Environment | `$SYNDICATE_RUNTIME` | Sets `default` only, when the variable is set. |
| User | `$XDG_CONFIG_HOME/syndicate/syndicate.conf`, or `~/.config/syndicate/syndicate.conf` when `XDG_CONFIG_HOME` is unset or empty | Applies to every project. |
| Project | `<project>/.syndicate/syndicate.conf` | Replaces the same key from the layers above and leaves every other key alone. |
| One run | `-x`, `--reserve` | Host CLI and reserve for this launch. `--all-runtime` assigns every specialist. `--inbox-interval`, `--inbox-transport`, and `--no-live` control inbox delivery. |

```
# ~/.config/syndicate/syndicate.conf
handoff-reserve = 25
default = claude
```

```
# <project>/.syndicate/syndicate.conf
# Reviews come from Codex here; the reserve still comes from the user file.
code-reviewer = codex
```

A role named in the user file must exist on the project being launched: a
global role, or a role defined in that project's `.syndicate/roles/`. An
unknown name or CLI is an error in either file.

`syndicate team init` writes a commented `.syndicate/syndicate.conf` when the project does
not already have one. It does not copy values out of the user file, so a later
edit there still reaches every project that left the key unset.

### `.syndicate/syndicate.conf`: which CLI runs each role

One `key = value` per line. `#` starts a comment. The same keys are valid in
the user file.

| Key | Values | Meaning |
|---|---|---|
| `tmux` | `on` (default) \| `off` | Global-only setting: run Chief of Staff, team tasks, and specialist CLIs in tmux, or execute directly. Project overrides are rejected. |
| `handoff` | `on` \| `off` | Managed quota monitoring and automatic handoff (default `on`). |
| `handoff-reserve` | number between 0 and 100, exclusive | Account usage remaining at which to stop (default `10`). |
| `default` | `claude` \| `codex` | CLI for every role not listed individually. |
| `orchestrator` | `claude` \| `codex` | CLI that **hosts the session**. Every other role is delegated to from here. |
| `<role name>` | `claude` \| `codex` | CLI for that one role: any global role, or a project-only role you defined. |
| `review-gate` | `on` (default) \| `off` | Block finishing until `code-reviewer` has reviewed delegated work. |
| `inbox-interval` | minutes dividing 60 evenly (default `5`) | Scheduled inbox cadence. Examples: `5`, `10`, `15`, `30`, `60`. |
| `inbox-transport` | `auto` (default) \| `poll` \| `channel` | `auto`: Codex native queue, Claude native scheduled polling. `channel`: explicit Claude preview channel. |

Set `tmux = off` in `~/.config/syndicate/syndicate.conf` (or
`$XDG_CONFIG_HOME/syndicate/syndicate.conf`) to disable tmux for every execution
task. With tmux enabled, Chief of Staff uses `chief-of-staff-work` and project
tasks use `<project_name>-work`. Interactive launches attach automatically from
a terminal outside tmux; launches from inside tmux remain detached and print an
attach command. Headless runs wait and return the CLI's stdout, stderr, and exit
code. Specialists already inside a team tmux session run there; standalone
specialist runs use a temporary named session. Missing tmux produces an error
with instructions to install it or disable the setting. Dry runs start nothing.

Chief of Staff inherits the two inbox settings from the user file, without
loading project settings or role assignments. Both launch paths accept
`--inbox-interval MINUTES` and `--inbox-transport MODE` overrides. Polling defaults
to five minutes and checks messages, requests, and decisions only; ordinary
project-status refreshes remain manual. See [live inbox delivery](chief-of-staff.md#live-inbox-delivery)
for scheduling, standardized queue envelopes, readiness, and acknowledgments.

```
# Everything on Claude, but reviews come from Codex:
default = claude
code-reviewer = codex
```

Precedence for the host CLI: `-x` > the `orchestrator` key (project, otherwise
user) > the `default` key (project, otherwise user, otherwise
`$SYNDICATE_RUNTIME`) > `claude`. For any other role: its own key (project,
otherwise user) > `default` by that same chain.

## Project role files

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
  `syndicate.conf`. Names are lowercase letters, digits and dashes.
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
| CLI per role | user `syndicate.conf`, then `.syndicate/syndicate.conf` | Chooses claude or codex for that role. A project key wins. |

## Mixed-CLI teams

The orchestrator runs in one CLI (the host) and can only spawn that CLI's own
agents. A role configured for the *other* CLI appears in the host as a thin
**relay agent** with the same name and description. When the orchestrator
delegates to it, the relay runs `syndicate team run-role <role>`, which executes the
real role headlessly on its own CLI and hands the output back. Because the relay
keeps the role's name, delegation and the review gate work identically.

Things to know:

- **Claude host, Codex role.** The generated settings allow the relay's exact
  command (`Bash(<syndicate> team run-role *)`) so it doesn't prompt every time.
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
  `syndicate team init` and every run make sure the entry exists.
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
from `.claude/settings.json` and `.codex/hooks.json`, then run `syndicate team init`.
Check `git status` first so you only remove files syndicate created.

## Troubleshooting

- `syndicate config -C <project>`: shows the merged settings and any warnings
  (dangling `.append.md` files, invalid role names). `--local` also shows
  overridden keys.
- `unknown role 'x'` in `syndicate.conf`: the role doesn't exist globally or under
  `.syndicate/roles/`. Check spelling with `syndicate config`.
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

Project Codex launches add `~/.syndicate/portfolio` as a writable directory so
orchestrators can record shared decisions, briefs, and request receipts while
keeping their project workspace sandbox. Claude orchestrators include Bash for
these supported CLI workflows. Project role overrides that explicitly restrict
tools must retain Bash when using coordination commands.
