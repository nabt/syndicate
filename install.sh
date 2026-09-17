#!/usr/bin/env bash
# Install this team's agent identities into a project's .claude/agents/
# directory (or the user-level ~/.claude/agents/), so Claude Code can use
# them as subagents. roles/*.md are used as-is — no build step.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  -t, --target <dir>   Project directory to install into (default: current directory)
  -g, --global          Install into ~/.claude/agents instead of a project
  -s, --symlink          Symlink instead of copying (agents stay in sync with this repo)
  -r, --roles <list>    Comma-separated role names to install (default: all)
  -h, --help             Show this help
EOF
}

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROLES_SRC="$REPO_DIR/roles"

TARGET=""
GLOBAL=0
SYMLINK=0
ROLES=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--target) TARGET="$2"; shift 2 ;;
    -g|--global) GLOBAL=1; shift ;;
    -s|--symlink) SYMLINK=1; shift ;;
    -r|--roles) ROLES="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ $GLOBAL -eq 1 ]]; then
  DEST="$HOME/.claude/agents"
else
  DEST="${TARGET:-$(pwd)}/.claude/agents"
fi

mkdir -p "$DEST"

if [[ -n "$ROLES" ]]; then
  IFS=',' read -ra ROLE_LIST <<< "$ROLES"
else
  ROLE_LIST=()
  for f in "$ROLES_SRC"/*.md; do
    ROLE_LIST+=("$(basename "$f" .md)")
  done
fi

count=0
for role in "${ROLE_LIST[@]}"; do
  src="$ROLES_SRC/$role.md"
  if [[ ! -f "$src" ]]; then
    echo "Skipping unknown role: $role" >&2
    continue
  fi
  if [[ $SYMLINK -eq 1 ]]; then
    ln -sf "$src" "$DEST/$role.md"
    echo "Linked  $role -> $DEST/$role.md"
  else
    cp "$src" "$DEST/$role.md"
    echo "Copied  $role -> $DEST/$role.md"
  fi
  count=$((count + 1))
done

echo
echo "Installed $count agent(s) into $DEST"
