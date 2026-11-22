# Codex CLI command approvals

Interactive Codex asks before it runs a command that leaves the sandbox, unless
a prefix rule in `~/.codex/rules/` already allows that command. Codex reads
every `*.rules` file in that directory when a thread starts. An already-open
session keeps the rules it loaded at start.

On this machine the recommended set is installed at
`~/.codex/rules/common.rules`, next to the older syndicate-specific files
below. Check a command against the installed files with:

```sh
codex execpolicy check --pretty \
  --rules ~/.codex/rules/common.rules \
  --rules ~/.codex/rules/custom_exec.rules \
  --rules ~/.codex/rules/default.rules \
  --rules ~/.codex/rules/git.rules \
  -- git status --short
```

`decision` is `allow` when a rule covers the command. An empty `matchedRules`
list means Codex will still ask. A matching `prompt` rule wins over a shorter
`allow` rule.

## Rule shape

Rules are Starlark. A pattern is an exact token prefix. A nested list is a set
of alternatives for that one token. Extra arguments after the prefix are
allowed. `match` and `not_match` are examples Codex checks when it loads the
file.

```starlark
prefix_rule(
    pattern = ["git", ["status", "diff", "log"]],
    decision = "allow",
    justification = "Routine git inspection.",
    match = ["git status --short"],
    not_match = ["git reset --hard"],
)
```

`decision` is `allow`, `prompt`, or `forbidden`. `host_executable` lets an
absolute path such as `/usr/bin/git` match a rule whose first token is `git`,
and only for the paths listed there. Point those paths at the local binaries
(`command -v git`, and the same for `gh` and `cargo`).

## Recommended allows

These are the prefixes in `common.rules`. They came from the commands Codex
sessions on this machine actually run.

| Tool | Allowed prefix |
|---|---|
| git | `status`, `diff`, `log`, `show`, `fetch`, `branch`, `rev-parse`, `grep`, `ls-files`, `ls-tree`, `ls-remote`, `remote`, `worktree`, `switch`, `merge-base`, `rev-list`, `check-ignore`, `cat-file`, `for-each-ref`, `blame`, `apply`, `patch-id`, `archive`, `stash`, `merge`, `add` |
| git | `commit`, then any arguments |
| git | `push`, then any arguments |
| gh | `pr` then `view`, `list`, `diff`, `checks`, `status`, `create`, `edit`, or `review` |
| gh | `run` then `view`, `list`, or `watch` |
| gh | `repo view` |
| gh | `auth status` |
| rg | any `rg` search |
| python | `python`, `python3`, or `python3.14` (including `/usr/bin/python3`, `/bin/python3`, and the Linuxbrew 3.14 paths), then `-m unittest` or `-m pytest` |
| python | the same interpreters, then `-B -m unittest` or `-B -m pytest` |
| cargo | `test`, `check`, `fmt`, `clippy`, `build` |
| docker | `ps`, `images`, `version`, `info`, and `image ls` |
| nerdctl | `ps`, `images`, `version`, `info`, `inspect`, `logs`, `image ls`, and `namespace`/`network`/`volume` `ls` |

`python3 -B -m unittest` is the form these sessions use most often. A rule for
`python3 -m unittest` does not cover the `-B` form, because `-B` occupies the
next token.

## Commands that still ask

| Command | Why it still asks |
|---|---|
| `git commit --amend` | Rewrites history. A `prompt` rule overrides the `commit` allow when `--amend` is the next token. |
| `git push --force`, `git push -f`, `git push --force-with-lease` | A `prompt` rule overrides the `push` allow when that flag is the next token. |
| `rg --pre …` | `--pre` runs another program. |
| `git reset`, `git clean`, `git config` | Left unmatched. |
| `python -c`, `python -m pip` | Left unmatched. A bare `python` or `python -` prefix would allow arbitrary scripts. |
| `gh pr merge`, `gh api`, `gh auth login`, `gh run cancel`, `gh run rerun` | Left unmatched. |
| `cargo install`, `cargo clean` | Left unmatched. |
| `docker`/`nerdctl` `run`, `exec`, `build`, and image or volume removal | Left unmatched. |
| `rm` | Codex will not save a prefix rule for `rm`. |

Prefix rules cannot see a flag that comes later. `git push origin --force` and
`git commit -m hello --amend` match the plain `push` and `commit` allows.
Those two shapes did not appear in the session logs. Heredoc commands also
stay on a prompt; a saved prefix is the wrong tool for them.

Read-only inspection inside the workspace (`sed -n`, `head`, `tail`, `ls`,
`cat`) already runs under the `on-request` sandbox, so `common.rules` leaves
those commands unmatched.

## Syndicate rules already installed

`~/.codex/rules/custom_exec.rules` allows `syndicate` and
`python3.14 …/bin/syndicate` for `requests`, `team`, `briefs`, `sessions`,
`live`, `messages`, `briefing`, `decisions`, `tasks`, `config`, `dashboard`,
`web`, and `limits`. It also allows `gh pr review`.

`~/.codex/rules/default.rules` allows `gh pr view`, `git fetch`,
`python3 -m unittest`, one `syndicate sessions --json` invocation, and one
specific `git worktree add`.

`~/.codex/rules/git.rules` allows `gh run view`.

`common.rules` repeats a few of those prefixes so the recommended set stands
on its own. Repeating an allow is harmless.
