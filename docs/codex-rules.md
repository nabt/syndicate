# Codex CLI command approvals

Interactive Codex asks before it runs a command that leaves the sandbox, unless
a prefix rule in `~/.codex/rules/` already allows that command. Codex reads
every `*.rules` file in that directory when a thread starts. An already-open
session keeps the rules it loaded at start.

The recommended set is [`resources/codex/rules/common.rules`](../resources/codex/rules/common.rules).
Copy it into `~/.codex/rules/` and start a new thread. Check a command against
that file with:

```sh
codex execpolicy check --pretty \
  --rules resources/codex/rules/common.rules \
  -- git status --short
```

`decision` is `allow` when a rule covers the command. An empty `matchedRules`
list means Codex will still ask. A matching `prompt` rule wins over a shorter
`allow` rule.

When a tool is invoked by an absolute path, add a local `host_executable()`
entry for that path. The recommended file already lists `/usr/bin/git` and
`/bin/git`. A rule whose first token is `python3` matches `python3 -m unittest`
and does not match `/usr/local/bin/python3 -m unittest` until that path is
listed.

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

`decision` is `allow`, `prompt`, or `forbidden`.

## Recommended allows

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
| python | `python`, `python3`, or `python3.14`, then `-m unittest` or `-m pytest` |
| python | the same interpreters, then `-B -m unittest` or `-B -m pytest` |
| cargo | `test`, `check`, `fmt`, `clippy`, `build` |
| docker | `ps`, `images`, `version`, `info`, and `image ls` |
| nerdctl | `ps`, `images`, `version`, `info`, `inspect`, `logs`, `image ls`, and `namespace`/`network`/`volume` `ls` |

`python3 -B -m unittest` needs its own rule. `-B` occupies the token before
`-m`, so a rule for `python3 -m unittest` leaves the `-B` form unmatched.

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
Heredoc commands also stay on a prompt; a saved prefix is the wrong tool for
them.

Read-only inspection inside the workspace (`sed -n`, `head`, `tail`, `ls`,
`cat`) already runs under the `on-request` sandbox, so
[`common.rules`](../resources/codex/rules/common.rules) leaves those commands
unmatched.
