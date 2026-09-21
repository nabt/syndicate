#!/usr/bin/env bash
# Stop hook (Claude Code and Codex): if the orchestrator delegated work to
# specialist roles during this session but never invoked code-reviewer, block
# the stop and say so. Reads delegations from whichever transcript format it
# is given: Claude Code Agent/Task tool calls, or Codex spawn_agent calls.
set -uo pipefail

input="$(cat)"
[[ "$(jq -r '.stop_hook_active // false' <<<"$input")" == "true" ]] && exit 0
transcript="$(jq -r '.transcript_path // empty' <<<"$input")"
[[ -f "$transcript" ]] || exit 0

roles="$(
  {
    jq -rs '.[] | .message.content? | arrays | .[]
      | select(.type == "tool_use" and (.name == "Agent" or .name == "Task"))
      | .input.subagent_type // empty' "$transcript" 2>/dev/null
    jq -r 'select(.payload.type? == "function_call" and .payload.name? == "spawn_agent")
      | .payload.arguments | try (fromjson | .agent_type // empty) catch empty' "$transcript" 2>/dev/null
  } | sort -u | paste -sd, -
)"

delegated=0
for r in backend-engineer frontend-engineer devops-engineer qa-engineer security-engineer technical-writer; do
  [[ ",$roles," == *",$r,"* ]] && delegated=1
done
[[ $delegated -eq 1 && ",$roles," != *",code-reviewer,"* ]] || exit 0

jq -n '{decision: "block", reason: "Work was delegated to specialist roles but code-reviewer has not reviewed it. Delegate a review to code-reviewer (and security-engineer if it touches auth, input, or infra access) before finishing."}'
