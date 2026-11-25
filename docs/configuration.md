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
isolated Chief of Staff workspace. `worktrees/` holds locked task worktrees and
their `registry.json`; set `$SYNDICATE_WORKTREE_ROOT` to an absolute, durable
directory to place worktrees elsewhere (temporary filesystems are refused; the
registry stays in `~/.syndicate/worktrees/`). An invalid `registry.json` is
never overwritten; worktree commands stop until it is repaired or moved aside.
See [Task worktrees](handoff.md#task-worktrees). See the [Chief of Staff guide](chief-of-staff.md)
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
| `review-gate` | `on` (default) \| `off` | Enable the transcript Stop hook for a build delegation without a `code-reviewer` invocation. Managed closeout requires role attribution with either setting. |
| `inbox-interval` | minutes dividing 60 evenly (default `5`) | Scheduled inbox cadence. Examples: `5`, `10`, `15`, `30`, `60`. |
| `inbox-transport` | `auto` (default) \| `poll` \| `channel` | `auto`: Codex native queue, Claude native scheduled polling. `channel`: explicit Claude preview channel. |

Set `tmux = off` in `~/.config/syndicate/syndicate.conf` (or
`$XDG_CONFIG_HOME/syndicate/syndicate.conf`) to disable tmux for every execution
task. With tmux enabled, every launch uses one session, `the-syndicate`, and
creates it only when it is absent. Chief of Staff is the window `chief-of-staff`
(exactly one window, with no `-work` suffix). Each project team is its own
window, named after the project. Each window keeps the single pane that runs
its CLI. List and show those windows with `syndicate list workers`,
`syndicate show`, `syndicate capture`, `syndicate message`, `syndicate attach`,
`syndicate detach`, and `syndicate close` instead of driving tmux yourself. `syndicate show` of one worker attaches to
that window. `syndicate show` of two workers places their panes side by side
for that view, and three or more are tiled. Detaching, switching away, or
`syndicate detach` puts each worker back in its own window. Press F12 to leave a
view without stopping the worker; Ctrl-b often does not reach tmux from Cursor
or VS Code. Interactive launches
attach that window automatically from a terminal outside tmux; launches from
inside tmux remain detached and print a `syndicate show` command. Headless runs wait and return the CLI's
stdout, stderr, and exit code, then close only their window. Specialists already
inside a syndicate window run there; standalone specialist runs use a temporary
window in `the-syndicate`. Missing tmux produces an error with instructions to
install it or disable the setting. Dry runs start nothing.

Interactive Codex launches suppress the default welcome-screen onboarding tips
with the built-in `codex-tui.show_tooltips = false` setting, for both project
teams and Chief of Staff. Set it to `true` to restore tips.

### CLI-specific options

Use `<cli>-<option> = value` in either settings file. These keys follow the
same per-key merge rules: project values replace user values, while omitted
keys keep their user or built-in values. Only options for the CLI being
launched are passed. Runtime overrides and mixed-provider specialist launches
use the options for the selected runtime. Chief of Staff reads user settings
only, independent of the current project.

```ini
codex-model_verbosity = low
codex-tui.show_tooltips = false
claude-model = sonnet
claude-verbose = false
```

Codex keys become `--config` arguments: `codex-model_verbosity = low` produces
`--config 'model_verbosity="low"'`. Bare strings are quoted as TOML strings;
explicitly quoted strings, booleans, numbers, arrays, and inline tables retain
their TOML types. Nested Codex keys use dots. These are Codex configuration
keys, rather than top-level flags such as `--no-alt-screen`.

Claude keys become long options: `claude-model = sonnet` produces
`--model sonnet`. Boolean `true` passes a bare flag; `false` omits that flag.
Quote `"false"` to pass it as a literal value. Claude values must be scalar.
Options retain their spelling, including hyphens and underscores.

Values are passed as individual arguments without shell expansion. Quoted `#`
characters are preserved; unquoted `#` begins a comment. CLI options are added
after Syndicate's generated options and before task text; explicit trailing
CLI arguments come later. Use options supported by the selected provider and
execution mode; Syndicate validates names and value shapes but leaves provider
option support to that CLI. These settings affect Syndicate launches only.

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

At launch, a command-line CLI (`-x`, `run-role --runtime`, or `--all-runtime`)
is used as given. A CLI named in the user or project file, or by
`$SYNDICATE_RUNTIME`, is used only when its remaining usage is above
`handoff-reserve`. Otherwise Syndicate starts the installed CLI with the most
remaining usage that is still above that reserve, compared monthly, then
weekly, then the 5-hour window. With no CLI named in either
file or the environment, it starts the one with the most remaining usage,
using that same order.
The built-in `claude` fallback is not a named CLI. See
[Usage-aware handoffs](handoff.md).

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
  `workspace-write`. Roles without them run through an isolated Codex relay:
  the child has a disposable scratch workspace outside the source repository,
  so tests can write temporary files while source files stay read-only. Omit
  `tools` and the role gets full tools / `workspace-write`.

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
When that CLI reports an account usage limit, the runner can retry the role
on another installed CLI using the [latest usage readings](handoff.md).

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
syndicate setup-codex-gate    # merges one Stop entry into $CODEX_HOME/hooks.json (default ~/.codex)
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
- Generated files are in `.syndicate/.generated/<run>/`: `claude-settings.json`,
  `codex/*.toml` and `owner.json` (launcher PID and release). Each launch writes
  its own directory, so a newer release never replaces files an older live
  session still reads. Directories are kept after exit; a later launch removes
  up to 32 that are over a day old and whose launcher has exited. Edit the
  sources, not these files.

## Release identity and pinned execution

`syndicate --version` (or `syndicate version`, `--json` for machine output)
prints the running mode, commit, interpreter and root, and writes nothing:

- `release`: the root has a `RELEASE.json` manifest. Shows its commit and, when
  `$XDG_DATA_HOME/syndicate/activations.jsonl` has a record for that commit,
  the latest tag and activation time.
- `checkout`: a Git checkout. Shows `HEAD`, the branch and whether the tree is dirty.
- `dev`: a snapshot started by `syndicate dev` (below), with the source checkout's commit.

A launch resolves the real path of `bin/syndicate` once, and every roles,
skills, hooks, web and portfolio asset comes from that root, whatever the
current directory, `PATH` or a later move of an installed `current` symlink.
Everything Syndicate starts again (Chief launch context, team inbox and
backoff commands, relays and their Claude allow rule, Codex relay agents, tmux
windows, the limits monitor, the Claude status line and the channel MCP
server) uses the same prefix: the absolute interpreter, `-B`, and that real
launcher path. Bundled role and skill commands such as `syndicate tasks close`
are rendered with that prefix when the prompt is assembled, so a Chief's teams
and a team's specialists stay on the Chief's release until it restarts.
`run.json` and the limits monitor's `monitor.json` record the release (`mode`,
`commit`, `root`, `interpreter`); older records without it are still read.

Python never writes bytecode for Syndicate: the launcher disables it before
importing its modules, every Python child runs with `-B`, and children inherit
`PYTHONDONTWRITEBYTECODE=1`.

## Development runs

`syndicate dev --checkout PATH [--home DIR] [--keep] [--share-provider-config] -- ARGS...`
runs an unmerged checkout without touching your installed release or its state:

1. Validates that PATH is the top of a Syndicate Git checkout and reports its
   branch, commit and dirty state.
2. Copies its tracked and untracked, not ignored, files (never `.git` or
   bytecode) into a temporary, read-only snapshot. Later edits to the checkout
   cannot change the running run.
3. Runs `python3 -B <snapshot>/bin/syndicate ARGS...` from a disposable working
   directory with a disposable `HOME` (or `--home DIR`), `XDG_*` directories,
   `TMPDIR`, `TMUX_TMPDIR` (its own tmux server) and a namespaced tmux session
   `syndicate-dev-<id>`. All Syndicate state (registry, portfolio, limits
   monitor and its lock, worktree registry, user config) lives under `HOME`
   and the XDG directories, so it lands in the disposable home; no separate
   state override exists. Inherited `SYNDICATE_*` and `TMUX` variables are
   dropped. `CODEX_HOME` and `CLAUDE_CONFIG_DIR` are disposable too, so
   providers start logged out, unless you pass `--share-provider-config`.
4. Puts `<devroot>/bin` first on `PATH`. Its `syndicate` runs the snapshot's
   pinned prefix (interpreter, `-B`, snapshot launcher), so a bare `syndicate`
   inside the dev run, its tmux windows or its agents never reaches the
   installed release.
5. Removes the temporary directories at exit unless `--keep` is given, a
   dev tmux session is still running, or the dev limits monitor does not stop.

Arguments for the snapshot launcher go after `--`; `syndicate dev --checkout
PATH --version` is an error that says so (use `-- --version`).

The snapshot refuses to run if its state, user config or tmux server would
resolve to production (`~/.syndicate`, `~/.config/syndicate`,
`~/.local/share/syndicate`, the `the-syndicate` session or the default tmux
server). Every project, source, repository, worktree and handoff argument
(`--project`, `-C`/`--dir` including its `.` default, `--repo`, `--source`,
`--worktree`, `--handoff`) is checked before the command runs. A dev run
refuses a target that is, or is inside, a project or generic task group in the
production registry, that shares a Git repository with one (a linked task
worktree or any subdirectory of it, matched by `git rev-parse
--git-common-dir`), or that is under production Syndicate state. If the
production registry cannot be read or Git cannot run, the run refuses rather
than guess. Use a separate target project, given by absolute path, for team runs:

```sh
syndicate dev --checkout ~/src/syndicate -- team task --project /path/to/scratch-repo "try the change"
```

For a quick local check without the snapshot, run the checkout directly with
bytecode disabled: `python3 -B bin/syndicate --version`.

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


Codex specialists do not register the project again: `team run-role` reads the
existing project configuration without taking the user-wide registry lock.
Registry-changing commands still require host approval if the sandbox denies
them; no write access to all of `~/.syndicate` is granted. Generated native
writing agent configs explicitly retain the portfolio writable root. Read-only
Codex roles use a read-only relay even when both CLIs are Codex, without granting
portfolio writes or enabling network access. If the sandbox blocks scratch
creation or the child CLI's API connection, the relay requests normal approval
to run the exact `run-role` command outside the sandbox; if approval is unavailable,
it reports the failure to the orchestrator. Cross-provider relays retain their
existing workspace and network settings.

A standalone Codex specialist gets a unique temporary workspace under
`~/.syndicate/portfolio/scratch/`, removed when it exits. `TMPDIR` points there;
bytecode writes are disabled, and implicit `/tmp` and inherited `TMPDIR` sandbox
write roots are disabled. A reviewer child has only its scratch workspace
writable, not the repository or the rest of the portfolio. Run source checks
against the source path in its instructions and direct outputs to scratch.
Because its working directory is outside the source project, the child is
explicitly instructed to read applicable ancestor, root and nested `AGENTS.md`
and `AGENTS.override.md` files before review. Source project `.codex` configuration
is not discovered from scratch; configured Syndicate CLI options still apply.
Source-changing tests need an isolated fixture/copy or explicit host approval.
Writing specialist children retain the project and portfolio roots.
