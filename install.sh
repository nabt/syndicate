#!/usr/bin/env bash
# Install this team's agent identities for Claude Code (.claude/agents/*.md,
# used as-is) and/or Codex (.codex/agents/*.toml, generated at install time
# from roles/*.md so there is nothing derived to keep in sync).
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  -t, --target <dir>    Project directory to install into (default: current directory)
  -g, --global           Install user-wide (~/.claude/agents, ~/.codex/agents)
  -x, --runtime <name>  claude (default), codex, or both
  -s, --symlink           Symlink instead of copying (claude only; codex files are generated)
  -r, --roles <list>     Comma-separated role names to install (default: all)
  -R, --review-gate       Also install the Stop hook that blocks finishing until
                          code-reviewer has reviewed delegated work (needs jq).
                          Codex requires a one-time approval of the hook (see README).
  -h, --help              Show this help
EOF
}

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROLES_SRC="$REPO_DIR/roles"

TARGET=""
GLOBAL=0
SYMLINK=0
ROLES=""
GATE=0
RUNTIME="claude"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--target) TARGET="$2"; shift 2 ;;
    -g|--global) GLOBAL=1; shift ;;
    -x|--runtime) RUNTIME="$2"; shift 2 ;;
    -s|--symlink) SYMLINK=1; shift ;;
    -r|--roles) ROLES="$2"; shift 2 ;;
    -R|--review-gate) GATE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

case "$RUNTIME" in claude|codex|both) ;; *) echo "Unknown runtime: $RUNTIME" >&2; exit 1 ;; esac
if [[ -n "$ROLES" ]]; then
  IFS=',' read -ra ROLE_LIST <<< "$ROLES"
else
  ROLE_LIST=()
  for f in "$ROLES_SRC"/*.md; do
    ROLE_LIST+=("$(basename "$f" .md)")
  done
fi

# fm <file> <key>: value of a single-line frontmatter field.
fm() {
  awk -v k="$2" 'c==1 && index($0,k": ")==1 {print substr($0,length(k)+3); exit} /^---$/{c++}' "$1"
}

# json_str <text>: JSON string literal, which is also a valid TOML basic string.
json_str() { jq -Rn --arg v "$1" '$v'; }

# install_gate <config-dir> <hook-command>: copy the hook script into
# <config-dir>/hooks and merge a Stop entry into <config-dir>/<settings-file>.
install_gate() {
  local base="$1" hook_cmd="$2" settings_file="$3" settings tmp
  command -v jq >/dev/null || { echo "jq is required for --review-gate" >&2; exit 1; }
  mkdir -p "$base/hooks"
  cp "$REPO_DIR/hooks/require-review.sh" "$base/hooks/require-review.sh"
  settings="$base/$settings_file"
  [[ -f "$settings" ]] || echo '{}' > "$settings"
  tmp="$(mktemp)"
  jq --arg cmd "$hook_cmd" '
    .hooks.Stop = ((.hooks.Stop // []) | map(select(any(.hooks[]?; .command == $cmd) | not)))
      + [{hooks: [{type: "command", command: $cmd}]}]' "$settings" > "$tmp" && mv "$tmp" "$settings"
  echo "Installed review-gate Stop hook (config: $settings)"
}

install_claude() {
  local base dest role src count=0
  if [[ $GLOBAL -eq 1 ]]; then base="$HOME/.claude"; else base="${TARGET:-$(pwd)}/.claude"; fi
  dest="$base/agents"
  mkdir -p "$dest"
  for role in "${ROLE_LIST[@]}"; do
    src="$ROLES_SRC/$role.md"
    [[ -f "$src" ]] || { echo "Skipping unknown role: $role" >&2; continue; }
    if [[ $SYMLINK -eq 1 ]]; then
      ln -sf "$src" "$dest/$role.md"; echo "Linked  $role -> $dest/$role.md"
    else
      cp "$src" "$dest/$role.md"; echo "Copied  $role -> $dest/$role.md"
    fi
    count=$((count + 1))
  done
  echo "Installed $count Claude Code agent(s) into $dest"

  if [[ $GATE -eq 1 ]]; then
    if [[ $GLOBAL -eq 1 ]]; then
      install_gate "$base" "$base/hooks/require-review.sh" settings.json
    else
      install_gate "$base" '"$CLAUDE_PROJECT_DIR"/.claude/hooks/require-review.sh' settings.json
    fi
  fi
}

install_codex() {
  command -v jq >/dev/null || { echo "jq is required for the codex runtime" >&2; exit 1; }
  local base dest role src count=0 sandbox tools
  if [[ $GLOBAL -eq 1 ]]; then base="$HOME/.codex"; else base="$(cd "${TARGET:-$(pwd)}" && pwd)/.codex"; fi
  dest="$base/agents"
  mkdir -p "$dest"
  for role in "${ROLE_LIST[@]}"; do
    src="$ROLES_SRC/$role.md"
    [[ -f "$src" ]] || { echo "Skipping unknown role: $role" >&2; continue; }
    [[ "$role" == "orchestrator" ]] && continue  # run via bin/syndicate, not spawned as a subagent
    tools="$(fm "$src" tools)"
    if [[ "$tools" == *Write* || "$tools" == *Edit* ]]; then sandbox="workspace-write"; else sandbox="read-only"; fi
    {
      echo "name = $(json_str "$(fm "$src" name)")"
      echo "description = $(json_str "$(fm "$src" description)")"
      echo "sandbox_mode = \"$sandbox\""
      echo "developer_instructions = $(json_str "$(awk 'c>=2{print} /^---$/{c++}' "$src")")"
    } > "$dest/$role.toml"
    echo "Wrote   $role -> $dest/$role.toml ($sandbox)"
    count=$((count + 1))
  done
  echo "Installed $count Codex agent(s) into $dest"

  if [[ $GATE -eq 1 ]]; then
    install_gate "$base" "$base/hooks/require-review.sh" hooks.json
    echo "NOTE: Codex won't run this hook until you approve it once. Open 'codex' in the"
    echo "      project and accept the hook review prompt (or use its /hooks menu)."
  fi
}

[[ "$RUNTIME" == "claude" || "$RUNTIME" == "both" ]] && install_claude
[[ "$RUNTIME" == "codex" || "$RUNTIME" == "both" ]] && install_codex
exit 0
