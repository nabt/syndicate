"""Shell completion for syndicate.

`syndicate completion bash` and `syndicate completion zsh` print scripts.
Those scripts ask `syndicate __complete` for matches, so the suggestions stay
aligned with the argparse tree: subcommands, flags, choices, and values the
process can read without changing anything.
"""
from __future__ import annotations

import argparse
import sys

# Bit flags printed as the last line (`:N`) for the shell script.
NO_SPACE = 1
NO_FILE = 2
DIRS = 4
FILES = 8

PROSE_DESTS = frozenset({
    "rest", "task", "brief", "message", "text", "question", "progress", "note",
    "answer", "goal", "summary", "context", "recommendation", "deadline", "key",
    "revision", "reserve", "interval", "port", "lines", "number", "event_ids",
    "job_id", "constraints", "decisions", "dependencies", "next_steps", "help",
})
PATH_DESTS = frozenset({"dir", "handoff_path", "source", "repo", "worktree"})
DYNAMIC_DESTS = {
    "project": "project",
    "role": "role",
    "worker": "worker",
    "workers": "worker",
    "task_id": "task",
    "branch": "branch",
    "base": "ref",
    "decision_id": "decision",
    "request_id": "request",
    "message_id": "message",
    "session": "session",
}


class Source:
    """Root parser plus a lookup for values argparse does not list."""

    def __init__(self, parser, values):
        self.parser = parser
        self._values = values

    def values(self, kind: str) -> list[tuple[str, str]]:
        try:
            found = self._values(kind)
        except Exception:
            return []
        items = []
        for item in found or []:
            if isinstance(item, str):
                value, desc = item, ""
            else:
                value = "" if item[0] is None else str(item[0])
                desc = "" if len(item) < 2 or item[1] is None else str(item[1])
            items.append((value, desc))
        return items


def complete(words, cword, source) -> tuple[list[tuple[str, str]], int]:
    """Return completion matches for `words[cword]` and a shell directive."""
    words = ["" if word is None else str(word) for word in words]
    if cword < 0:
        cword = 0
    if cword >= len(words):
        words.extend([""] * (cword - len(words) + 1))
    parser = source.parser
    if cword <= 1:
        return _clean(_complete_first(parser, words[1] if len(words) > 1 else ""))
    words, cword = _rewrite_chief(parser, words, cword)
    prefix = words[cword]
    return _clean(_suggest(_parse(parser, words, cword), prefix, source))


def complete_main(argv, source) -> int:
    """Entry point for `syndicate __complete`."""
    shell = "bash"
    cword = None
    if "--" in argv:
        split = argv.index("--")
        options, words = argv[:split], argv[split + 1:]
    else:
        options, words = argv, []
    index = 0
    while index < len(options):
        option = options[index]
        if option in ("--shell", "--cword") and index + 1 >= len(options):
            _usage()
            return 2
        if option == "--shell":
            shell = options[index + 1]
            index += 2
        elif option == "--cword":
            try:
                cword = int(options[index + 1])
            except ValueError:
                print("syndicate: --cword must be an integer", file=sys.stderr)
                return 2
            index += 2
        else:
            _usage()
            return 2
    if shell not in ("bash", "zsh"):
        print("syndicate: completion shell must be bash or zsh", file=sys.stderr)
        return 2
    if cword is None:
        cword = max(len(words) - 1, 0)
    items, directive = complete(words, cword, source)
    sys.stdout.write(format_completion(items, directive, shell))
    return 0


def format_completion(items, directive, shell) -> str:
    lines = []
    for value, desc in items:
        if shell == "zsh":
            lines.append(f"{value}\t{desc}")
        else:
            lines.append(value)
    lines.append(f":{int(directive)}")
    return "\n".join(lines) + "\n"


def script(shell: str) -> str:
    if shell == "bash":
        return BASH_SCRIPT
    if shell == "zsh":
        return ZSH_SCRIPT
    raise KeyError(shell)


def _usage() -> None:
    print("syndicate: usage: __complete --shell bash|zsh --cword N -- WORDS...", file=sys.stderr)


def _field(text) -> str:
    cleaned = " ".join(str(text).replace("\t", " ").split())
    if len(cleaned) > 80:
        return cleaned[:77] + "..."
    return cleaned


def _clean(result):
    items, directive = result
    cleaned = []
    seen = set()
    for value, desc in items:
        value = _field(value)
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append((value, _field(desc)))
    return cleaned, directive


def _complete_first(parser, prefix):
    if prefix.startswith("-"):
        items = _flag_items(parser, prefix, set())
        chief = _commands(parser).get("chief-of-staff")
        if chief is not None:
            items.extend(_flag_items(chief, prefix, set()))
        return _dedupe(items), NO_FILE
    return _command_items(parser, prefix), NO_FILE


def _rewrite_chief(parser, words, cword):
    """Bare flags and text launch Chief of Staff. Completion follows that."""
    commands = set(_commands(parser))
    first = words[1]
    if first in commands or first in ("-h", "--help"):
        return words, cword
    return [words[0], "chief-of-staff", *words[1:]], cword + 1


def _parse(root, words, cword):
    parser = root
    used = set()
    positional_index = 0
    pending = None
    options_done = False
    index = 1
    while index < cword:
        word = words[index]
        if pending is not None:
            action, slot, arity = pending
            release = (arity in ("many", "optional")
                       and action.nargs != argparse.REMAINDER
                       and not options_done
                       and _is_known_flag(parser, word))
            if release:
                pending = None
            elif arity == "many":
                index += 1
                continue
            elif arity == "optional":
                pending = None
                index += 1
                continue
            else:
                slot += 1
                pending = (action, slot, arity) if slot < arity else None
                index += 1
                continue
        if not options_done and word == "--":
            options_done = True
            index += 1
            continue
        if not options_done:
            action, inline = _match_flag(parser, word)
            if action is not None:
                used.add(action.dest)
                if inline is None and _takes_value(action):
                    arity = _arity(action)
                    if arity == "many":
                        pending = (action, 0, "many")
                    elif arity == "optional":
                        pending = (action, 0, "optional")
                    elif isinstance(arity, int) and arity > 0:
                        pending = (action, 0, arity)
                index += 1
                continue
        commands = _commands(parser)
        if commands and word in commands:
            parser = commands[word]
            used = set()
            positional_index = 0
            pending = None
            options_done = False
            index += 1
            continue
        positionals = _positionals(parser)
        if positional_index < len(positionals):
            action = positionals[positional_index]
            arity = _arity(action)
            if arity == "many":
                pending = (action, 1, "many")
            elif isinstance(arity, int) and arity > 1:
                pending = (action, 1, arity)
            else:
                positional_index += 1
            index += 1
            continue
        index += 1
    return {
        "parser": parser,
        "pending": pending,
        "positional_index": positional_index,
        "used": used,
        "options_done": options_done,
    }


def _suggest(state, prefix, source):
    parser = state["parser"]
    pending = state["pending"]
    if pending is not None:
        action, slot, arity = pending
        if (arity == "many" and action.nargs != argparse.REMAINDER and not state["options_done"]
                and prefix.startswith("-") and prefix != "-"):
            return _dedupe(_flag_items(parser, prefix, state["used"])), NO_FILE
        return _value_items(action, slot, prefix, source, equals=False)
    if prefix.startswith("-") and not state["options_done"]:
        inline = _inline_value(parser, prefix)
        if inline is not None:
            action, value_prefix = inline
            items, directive = _value_items(action, 0, value_prefix, source, equals=True)
            name = prefix.split("=", 1)[0]
            return [(f"{name}={value}", desc) for value, desc in items], directive & ~DIRS
        return _dedupe(_flag_items(parser, prefix, state["used"])), NO_FILE
    if _commands(parser):
        return _command_items(parser, prefix), NO_FILE
    positionals = _positionals(parser)
    if state["positional_index"] < len(positionals):
        return _value_items(positionals[state["positional_index"]], 0, prefix, source, equals=False)
    return [], NO_FILE


def _value_items(action, slot, prefix, source, equals):
    kind = _kind(action, slot)
    directive = NO_FILE
    if kind == "choice":
        items = [(str(choice), "") for choice in action.choices]
    elif kind == "dir":
        items = []
        directive = DIRS
    elif kind == "project":
        items = source.values("project")
        directive = DIRS
    elif kind == "file":
        items = []
        directive = FILES
    elif kind == "none":
        items = [("-", "read the brief from stdin")] if action.dest == "brief" else []
    else:
        items = source.values(kind)
    if equals:
        directive &= ~DIRS
    return [(value, desc) for value, desc in items if str(value).startswith(prefix)], directive


def _kind(action, slot):
    metavar = action.metavar
    if isinstance(metavar, tuple) and slot < len(metavar):
        name = str(metavar[slot]).lower()
        if name == "role":
            return "role"
        if name == "provider":
            return "runtime"
        return "none"
    if action.option_strings and action.dest == "task":
        return "task"
    if action.choices is not None:
        return "choice"
    if action.dest in PATH_DESTS:
        return "dir"
    if action.dest in DYNAMIC_DESTS:
        return DYNAMIC_DESTS[action.dest]
    return "none"


def _flag_items(parser, prefix, used):
    excluded = _excluded_dests(parser, used)
    items = []
    for action in parser._actions:
        if not action.option_strings or action.help is argparse.SUPPRESS:
            continue
        if action.dest in excluded:
            continue
        if action.dest in used and not _repeatable(action):
            continue
        desc = _help(action.help)
        for option in action.option_strings:
            if option.startswith(prefix):
                items.append((option, desc))
    return items


def _command_items(parser, prefix):
    items = []
    for name, desc in _command_help(parser):
        if name.startswith(prefix):
            items.append((name, desc))
    return items


def _command_help(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            # Commands added without help= are absent from _choices_actions on Python 3.14.
            described = {choice.dest: _help(choice.help) for choice in action._choices_actions}
            return [(name, described.get(name, "")) for name in action.choices]
    return []


def _commands(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _positionals(parser):
    found = []
    for action in parser._actions:
        if action.option_strings or isinstance(action, argparse._SubParsersAction):
            continue
        if action.dest == argparse.SUPPRESS:
            continue
        found.append(action)
    return found


def _option_map(parser):
    found = {}
    for action in parser._actions:
        for option in action.option_strings:
            found[option] = action
    return found


def _match_flag(parser, word):
    if not word.startswith("-") or word in ("-", "--"):
        return None, None
    options = _option_map(parser)
    if word in options:
        return options[word], None
    if word.startswith("--") and "=" in word:
        name, _, value = word.partition("=")
        action = options.get(name)
        if action is not None and _takes_value(action):
            return action, value
    return None, None


def _inline_value(parser, word):
    action, value = _match_flag(parser, word)
    if action is None or value is None:
        return None
    return action, value


def _is_known_flag(parser, word):
    if word == "--":
        return True
    action, _inline = _match_flag(parser, word)
    return action is not None


def _takes_value(action):
    if isinstance(action, (
        argparse._StoreTrueAction,
        argparse._StoreFalseAction,
        argparse._StoreConstAction,
        argparse._AppendConstAction,
        argparse._CountAction,
        argparse._HelpAction,
    )):
        return False
    return action.nargs != 0


def _arity(action):
    nargs = action.nargs
    if nargs is None or nargs == 1:
        return 1
    if nargs == "?":
        return "optional"
    if nargs in ("*", "+", argparse.REMAINDER):
        return "many"
    if isinstance(nargs, int):
        return nargs
    return 1


def _repeatable(action):
    return isinstance(action, (
        argparse._AppendAction, argparse._AppendConstAction, argparse._CountAction,
    ))


def _excluded_dests(parser, used):
    excluded = set()
    for group in getattr(parser, "_mutually_exclusive_groups", ()):
        dests = {action.dest for action in group._group_actions}
        if dests & used:
            excluded |= dests
    return excluded


def _help(text):
    if not text or text is argparse.SUPPRESS:
        return ""
    return str(text)


def _dedupe(items):
    seen = set()
    result = []
    for value, desc in items:
        if value in seen:
            continue
        seen.add(value)
        result.append((value, desc))
    return result


BASH_SCRIPT = r"""# syndicate bash completion
# source <(syndicate completion bash)
# syndicate completion bash > ~/.local/share/bash-completion/completions/syndicate

_syndicate() {
    local cmd cword directive line value
    local -a words
    words=("${COMP_WORDS[@]}")
    cword=${COMP_CWORD:-0}
    if (( cword >= ${#words[@]} )); then
        words+=("")
    fi
    cmd=${words[0]:-syndicate}
    local out
    out=$("$cmd" __complete --shell bash --cword "$cword" -- "${words[@]}" 2>/dev/null) || return 0
    COMPREPLY=()
    directive=2
    while IFS= read -r line || [[ -n $line ]]; do
        [[ -z $line ]] && continue
        if [[ $line =~ ^:[0-9]+$ ]]; then
            directive=${line#:}
            continue
        fi
        value=${line%%$'\t'*}
        COMPREPLY+=("$value")
    done <<< "$out"
    if (( (directive & 1) != 0 )); then
        compopt -o nospace 2>/dev/null || true
    fi
    if (( (directive & 4) != 0 )); then
        compopt -o plusdirs 2>/dev/null || true
    fi
    if (( (directive & 8) != 0 )); then
        compopt -o filenames 2>/dev/null || true
        compopt -o default 2>/dev/null || true
    fi
    return 0
}

complete -F _syndicate syndicate
"""

ZSH_SCRIPT = r"""#compdef syndicate
# syndicate zsh completion. Source this after compinit:
#   source <(syndicate completion zsh)
# Or install it on fpath before compinit:
#   syndicate completion zsh > ~/.zsh/completions/_syndicate

_syndicate_complete() {
    emulate -L zsh
    local -a values descs raw
    local cmd cword directive line value desc suffix out
    raw=("${words[@]}")
    cword=$(( CURRENT - 1 ))
    if (( cword < 0 )); then
        cword=0
    fi
    if (( cword >= ${#raw} )); then
        raw+=("")
    fi
    cmd=${raw[1]:-syndicate}
    out=$("$cmd" __complete --shell zsh --cword "$cword" -- "${raw[@]}" 2>/dev/null) || return 1
    directive=2
    while IFS= read -r line; do
        [[ -z $line ]] && continue
        if [[ $line == :(<->) ]]; then
            directive=${line#:}
            continue
        fi
        value=${line%%$'\t'*}
        desc=${line#*$'\t'}
        if [[ $desc == "$line" || -z $desc ]]; then
            desc=$value
        fi
        values+=("$value")
        descs+=("$desc")
    done <<< "$out"
    suffix=' '
    if (( directive & 1 )); then
        suffix=''
    fi
    if (( ${#values} )); then
        compadd -S "$suffix" -d descs -- "${values[@]}"
    fi
    if (( directive & 4 )); then
        _files -/
    elif (( directive & 8 )); then
        _files
    fi
}

if [[ ${funcstack[1]} == _syndicate ]]; then
    _syndicate_complete "$@"
elif (( ${+functions[compdef]} )); then
    compdef _syndicate_complete syndicate
fi
"""
