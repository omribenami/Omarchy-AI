#!/usr/bin/env bash
# Source this from an interactive bash or zsh session.  It records a compact
# command/cwd/status trail for terminals the user opens directly.  Full PTY
# output remains available for terminals launched by Omarchy AI itself.

case $- in
  *i*) ;;
  *) return 0 2>/dev/null || exit 0 ;;
esac

if [ -n "${OMARCHY_AI_TERMINAL_CONTEXT_LOADED:-}" ]; then
  return 0 2>/dev/null || exit 0
fi
export OMARCHY_AI_TERMINAL_CONTEXT_LOADED=1

__omarchy_ai_context_dir="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy-ai/terminal_context"
mkdir -p "$__omarchy_ai_context_dir" 2>/dev/null || true
__omarchy_ai_context_tty="$(tty 2>/dev/null | tr '/ ' '__')"
[ -n "$__omarchy_ai_context_tty" ] || __omarchy_ai_context_tty="unknown-$$"
__omarchy_ai_context_file="$__omarchy_ai_context_dir/${__omarchy_ai_context_tty#_}.log"

__omarchy_ai_context_emit() {
  __omarchy_ai_context_status=$?
  [ -w "$__omarchy_ai_context_dir" ] || return 0
  __omarchy_ai_context_command="$(fc -ln -1 2>/dev/null)"
  __omarchy_ai_context_command="${__omarchy_ai_context_command//$'\n'/ }"
  # Do not copy likely credentials into a file intended for assistant
  # context. The terminal still behaves normally; only its context record
  # is redacted.
  __omarchy_ai_context_lower="$(printf '%s' "$__omarchy_ai_context_command" | tr '[:upper:]' '[:lower:]')"
  case "$__omarchy_ai_context_lower" in
    *password*|*passphrase*|*authorization:*|*api_key*|*apikey*|*secret*|*token=*)
      __omarchy_ai_context_command="[redacted command containing a credential]" ;;
  esac
  printf '[%s] shell=%s pid=%s cwd=%s status=%s command=%s\n' \
    "$(date -Is)" "${ZSH_VERSION:+zsh}${BASH_VERSION:+bash}" "$$" "$PWD" \
    "$__omarchy_ai_context_status" "$__omarchy_ai_context_command" >>"$__omarchy_ai_context_file"
  # Keep each context file bounded. The tail contains the useful current
  # task and avoids an ever-growing shell-history copy.
  if [ "$(wc -c <"$__omarchy_ai_context_file" 2>/dev/null || echo 0)" -gt 131072 ]; then
    tail -n 1000 "$__omarchy_ai_context_file" >"$__omarchy_ai_context_file.tmp" && mv "$__omarchy_ai_context_file.tmp" "$__omarchy_ai_context_file"
  fi
}

if [ -n "${ZSH_VERSION:-}" ]; then
  autoload -Uz add-zsh-hook
  add-zsh-hook precmd __omarchy_ai_context_emit
elif [ -n "${BASH_VERSION:-}" ]; then
  __omarchy_ai_context_previous_prompt_command="${PROMPT_COMMAND:-}"
  if [ -n "$__omarchy_ai_context_previous_prompt_command" ]; then
    PROMPT_COMMAND="__omarchy_ai_context_emit; $__omarchy_ai_context_previous_prompt_command"
  else
    PROMPT_COMMAND="__omarchy_ai_context_emit"
  fi
fi
