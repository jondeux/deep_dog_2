#!/usr/bin/env bash
# SessionStart hook: install this repo's Python dependencies in cloud sessions.
# Pattern from code.claude.com/docs/en/cloud-environments ("Install dependencies with a
# SessionStart hook"): cloud only (CLAUDE_CODE_REMOTE=true), skipped while the inputs'
# sha256 is unchanged, never blocks or fails the session. The install runs in the
# background; its state is in $INSTALL_DEPS_STATE/status (running | done | failed).
#
# Register in .claude/settings.json:
#   "SessionStart": [{"matcher": "startup|resume", "hooks": [{"type": "command",
#     "command": "bash", "args": ["${CLAUDE_PROJECT_DIR}/scripts/install_deps.sh"], "timeout": 15}]}]
set -uo pipefail
cat >/dev/null 2>&1 || true            # drain the hook payload

[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
root="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$root" 2>/dev/null || exit 0

PIP="${INSTALL_DEPS_PIP:-python3 -m pip}"
state="${INSTALL_DEPS_STATE:-${HOME}/.cache/install-deps/$(printf '%s' "$root" | sha256sum | cut -c1-12)}"
mkdir -p "$state" 2>/dev/null || exit 0
stamp="$state/inputs.sha256"

inputs=(scripts/install_deps.sh pyproject.toml)
args=(install --quiet --break-system-packages -e ".[dev]")
if [ -f webui/requirements-ui.txt ]; then
  inputs+=(webui/requirements-ui.txt)
  args+=(-r webui/requirements-ui.txt)
fi

if sha256sum --check --status "$stamp" 2>/dev/null; then
  echo "Python dependencies are installed and unchanged since the last install (${inputs[*]})."
  exit 0
fi

install() {
  echo running >"$state/status"
  rm -f "$stamp"                       # a failed install runs again next session
  # shellcheck disable=SC2086          # PIP may be "python3 -m pip"
  if $PIP "${args[@]}" >"$state/install.log" 2>&1; then
    sha256sum "${inputs[@]}" >"$stamp" 2>/dev/null || rm -f "$stamp"
    echo done >"$state/status"
  else
    echo failed >"$state/status"
  fi
}

if [ "${INSTALL_DEPS_FOREGROUND:-0}" = 1 ]; then
  install
else
  setsid bash -c "$(declare -p PIP args inputs state stamp); $(declare -f install); install" \
    >/dev/null 2>&1 </dev/null &
  echo "Python dependencies are installing in the background (pip install -e '.[dev]'). Status: $state/status (running, done or failed); log: $state/install.log. Tests that import project packages need it to read done."
fi
exit 0
