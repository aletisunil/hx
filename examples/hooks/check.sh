#!/usr/bin/env bash
#
# PostToolUse hook: check the file the model just edited, and hand whatever the
# checker says straight back to it.
#
# Without this, HX has no idea whether an edit compiles. The model writes a
# file, sees "Applied 1 edit(s)" and a diff, and finds out something broke two
# turns later when it runs the tests - or never. This closes that loop using
# the checkers the project already has, with no language server to supervise.
#
# Wire it up in ~/.hx/settings.json (hooks are loaded from your own settings
# only - see "Extending it" in the README):
#
#   {
#     "hooks": {
#       "PostToolUse": [
#         {
#           "matcher": "Edit|Write",
#           "hooks": [{"type": "command", "command": "~/.hx/check.sh", "timeout": 30}]
#         }
#       ]
#     }
#   }
#
# Exits 0 whether or not it finds anything. By PostToolUse the write has
# already landed, so exit 2 would refuse an edit that is on disk regardless -
# a confusing answer to something that already happened. Errors here are
# context for the next turn, not a veto.

set -uo pipefail

# Resolve a checker the way the project would run it. A bare `command -v ruff`
# finds nothing on the common setup where the tool lives in ./.venv and never
# on $PATH, and the hook then does nothing on every edit without saying why.
# Order: the project's venv, then $PATH, then `uv run` if this is a uv project.
find_tool() {
	local name=$1
	if [ -x "./.venv/bin/$name" ]; then
		printf './.venv/bin/%s' "$name"
		return 0
	fi
	if command -v "$name" >/dev/null 2>&1; then
		printf '%s' "$name"
		return 0
	fi
	if [ -f ./pyproject.toml ] && command -v uv >/dev/null 2>&1; then
		printf 'uv run --quiet %s' "$name"
		return 0
	fi
	return 1
}

event=$(cat)

path=$(
	printf '%s' "$event" |
		python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))' \
			2>/dev/null
)

# No path, or a file that no longer exists: nothing to check, and a hook that
# complains about its own inability to run is noise on every single edit.
[ -n "$path" ] && [ -f "$path" ] || exit 0

# One file, not the project. `ruff check .` on every edit is slow and buries
# the error the model just introduced under whatever was already failing.
case "$path" in
*.py)
	ruff=$(find_tool ruff) || exit 0
	# shellcheck disable=SC2086 # find_tool may return "uv run --quiet ruff"
	output=$($ruff check --quiet "$path" 2>&1)
	;;
*.ts | *.tsx)
	command -v npx >/dev/null || exit 0
	# Single-file tsc ignores the project graph, so imports it cannot resolve
	# are reported as errors. Real ones still surface; treat it as a smoke
	# test rather than a substitute for `tsc -p .`.
	output=$(npx --no-install tsc --noEmit "$path" 2>&1)
	;;
*.go)
	command -v go >/dev/null || exit 0
	output=$(go vet "$(dirname "$path")" 2>&1)
	;;
*.rs)
	command -v cargo >/dev/null || exit 0
	output=$(cargo check --quiet --message-format short 2>&1)
	;;
*)
	# An extension with no checker is silence, not a complaint. A hook that
	# says something on every Markdown edit gets switched off within a day.
	exit 0
	;;
esac

[ -z "$output" ] && exit 0

printf '%s' "$output" |
	python3 -c '
import json, sys
print(json.dumps({
    "additionalContext": "Checker output for the file you just edited:\n" + sys.stdin.read().strip()
}))
'
