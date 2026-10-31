#!/bin/sh
# HX installer.
#
#   curl -fsSL https://raw.githubusercontent.com/aletisunil/hx/main/install.sh | sh
#
# Checks for Node.js, then installs hx globally with npm. Idempotent:
# re-running updates in place.

set -eu

# The npm package is @sunilaleti/hx; the command it installs is `hx`.
HX_PACKAGE="${HX_PACKAGE:-@sunilaleti/hx}"
HX_NODE_MAJOR=22
HX_NODE_MINOR=15
HX_SUPPORT_EMAIL="iam@sunilaleti.dev"

log()  { printf '\033[0;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[0;33mwarning:\033[0m %s\n' "$1" >&2; }
die()  {
    printf '\033[0;31merror:\033[0m %s\n' "$1" >&2
    printf 'Need help? Contact \033[1;33m%s\033[0m\n' "$HX_SUPPORT_EMAIL" >&2
    exit 1
}

have() { command -v "$1" >/dev/null 2>&1; }

should_animate() {
    [ -t 1 ] && [ -z "${HX_NO_ANIMATION:-}" ] && [ -z "${CI:-}" ]
}

paint_logo_row() {
    row=$1
    text=$2
    visible=$3
    highlight=$4
    clear_prefix=$5

    if [ "$visible" -ne 0 ] && [ "$row" -gt "$visible" ]; then
        text=
    fi

    if [ "$highlight" -eq 0 ]; then
        colour='\033[0;36m'
    elif [ "$row" -eq "$highlight" ]; then
        colour='\033[1;96m'
    else
        colour='\033[2;36m'
    fi

    printf '%b%b%s\033[0m\n' "$clear_prefix" "$colour" "$text"
}

render_logo() {
    # visible=0 renders the whole logo. highlight=0 uses the settled colour.
    visible=${1:-0}
    highlight=${2:-0}
    clear_prefix=
    if [ "${3:-}" = clear ]; then
        clear_prefix='\033[2K\r'
    fi

    paint_logo_row 1 '    __  __  _  __' "$visible" "$highlight" "$clear_prefix"
    paint_logo_row 2 '   / / / / | |/ /' "$visible" "$highlight" "$clear_prefix"
    paint_logo_row 3 '  / /_/ /  |   /' "$visible" "$highlight" "$clear_prefix"
    paint_logo_row 4 ' / __  /  /   |' "$visible" "$highlight" "$clear_prefix"
    paint_logo_row 5 '/_/ /_/  /_/|_|' "$visible" "$highlight" "$clear_prefix"
}

render_boot_prompt() {
    typed=$1
    clear_prefix='\033[2K\r'

    printf '%b\n' "$clear_prefix"
    printf '%b\n' "$clear_prefix"
    printf '%b\033[2;36m  > \033[1;96m%s\033[0;36m_\033[0m\n' "$clear_prefix" "$typed"
    printf '%b\n' "$clear_prefix"
    printf '%b\n' "$clear_prefix"
}

rewind_logo() {
    printf '\033[5A'
}

animate_logo() {
    if ! should_animate; then
        render_logo
        return
    fi

    # Type the command, draw the mark one scanline at a time, then bounce a
    # highlight back through it. Every frame is five rows so redraws never jump.
    for typed in '' h hx; do
        render_boot_prompt "$typed"
        sleep 0.055
        rewind_logo
    done

    for row in 1 2 3 4 5; do
        render_logo "$row" "$row" clear
        sleep 0.045
        rewind_logo
    done

    for row in 4 3 2 1; do
        render_logo 0 "$row" clear
        sleep 0.035
        rewind_logo
    done

    render_logo 0 0 clear
}

stream_text() {
    message=$1
    printf '  '
    if should_animate; then
        # Word-by-word output keeps the effect portable across macOS and Linux.
        # shellcheck disable=SC2086  # Intentional word splitting for animation.
        for word in $message; do
            printf '%s ' "$word"
            sleep 0.03
        done
        printf '\n'
    else
        printf '%s\n' "$message"
    fi
}

banner() {
    animate_logo
    printf '\n'
    stream_text 'HX is an agent harness that works inside your project.'
    stream_text 'It reads and edits files, runs commands in a sandbox, and handles larger tasks with persistent sessions, skills, tools, and subagents.'
    stream_text 'Connect to models through OpenRouter and see context, cost, and usage for every turn.'
    printf '  Questions or issues? Contact \033[1;33m%s\033[0m\n\n' "$HX_SUPPORT_EMAIL"
}

ensure_node() {
    if ! have node || ! have npm; then
        die "HX needs Node.js $HX_NODE_MAJOR.$HX_NODE_MINOR or newer, with npm. Install it from https://nodejs.org/ and re-run."
    fi
    version=$(node --version)
    major=$(printf '%s' "$version" | sed 's/^v\([0-9]*\)\..*/\1/')
    minor=$(printf '%s' "$version" | sed 's/^v[0-9]*\.\([0-9]*\)\..*/\1/')
    if [ "$major" -lt "$HX_NODE_MAJOR" ] ||
        { [ "$major" -eq "$HX_NODE_MAJOR" ] && [ "$minor" -lt "$HX_NODE_MINOR" ]; }; then
        die "HX needs Node.js $HX_NODE_MAJOR.$HX_NODE_MINOR or newer; this is $version. Update it from https://nodejs.org/ and re-run."
    fi
    log "Node.js found: $version"
}

install_hx() {
    log "Installing $HX_PACKAGE"
    npm install --global "$HX_PACKAGE"
}

check_path() {
    have hx && return
    bindir="$(npm prefix --global)/bin"
    warn "hx is not on your PATH yet."
    # shellcheck disable=SC2016  # $PATH is literal text for the user to copy.
    printf '\n  Add this to your shell profile:\n\n    export PATH="%s:$PATH"\n\n' "$bindir"
}

main() {
    banner
    ensure_node
    install_hx
    check_path
    log "Done. Run 'hx' inside a project directory."
}

main "$@"
