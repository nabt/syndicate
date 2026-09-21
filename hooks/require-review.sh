#!/usr/bin/env bash
# Stop hook: if the orchestrator delegated work to specialist roles during
# this session but never invoked code-reviewer, block the stop and say so.
set -uo pipefail

input="$(cat)"
[[ "$(jq -r '.stop_hook_active // false' <<<"$input")" == "true" ]] && exit 0
transcript="$(jq -r '.transcript_path // empty' <<<"$input")"
[[ -f "$transcript" ]] || exit 0

roles="$(jq -rs '
  [ .[] | .message.content? | arrays | .[]
    | select(.type == "tool_use" and (.name == "Agent" or .name == "Task"))
    | .input.subagent_type // empty ] | unique | join(",")' "$transcript" 2>/dev/null)"

delegated=0
for r in backend-engineer frontend-engineer devops-engineer qa-engineer security-engineer technical-writer; do
  [[ ",$roles," == *",$r,"* ]] && delegated=1
done
[[ $delegated -eq 1 && ",$roles," != *",code-reviewer,"* ]] || exit 0

jq -n '{decision: "block", reason: "Work was delegated to specialist roles but code-reviewer has not reviewed it. Delegate a review to code-reviewer (and security-engineer if it touches auth, input, or infra access) before finishing."}'
