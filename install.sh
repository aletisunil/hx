#!/bin/sh
# HX installer.
#
#   curl -fsSL https://raw.githubusercontent.com/aletisunil/hx/main/install.sh | sh
#
# Bootstraps `uv` if it is missing, then installs hx as an isolated uv tool with
# a pinned Python. Idempotent: re-running upgrades in place.

set -eu

# The PyPI distribution is hx-cli; the command it installs is `hx`.
HX_PACKAGE="${HX_PACKAGE:-hx-cli}"
HX_PYTHON="${HX_PYTHON:-3.12}"
HX_SUPPORT_EMAIL="iam@sunilaleti.dev"
UV_INSTALLER="https://astral.sh/uv/install.sh"

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

render_logo() {
    padding=$1
    clear_prefix=
    [ "${2:-}" = clear ] && clear_prefix='\033[2K\r'

    printf '\033[0;36m'
    printf '%b%s%s\n' "$clear_prefix" "$padding" ' _   _  __  __'
    printf '%b%s%s\n' "$clear_prefix" "$padding" '| | | | \ \/ /'
    printf '%b%s%s\n' "$clear_prefix" "$padding" '| |_| |  >  <'
    printf '%b%s%s\n' "$clear_prefix" "$padding" "|  _  | / /\\ \\"
    printf '%b%s%s\n' "$clear_prefix" "$padding" "|_| |_|/_/  \\_\\"
    printf '\033[0m'
}

animate_logo() {
    if ! should_animate; then
        render_logo ''
        return
    fi

    # Slide in, overshoot slightly, then settle into place.
    render_logo '            ' clear
    sleep 0.07
    printf '\033[5A'
    render_logo '        ' clear
    sleep 0.07
    printf '\033[5A'
    render_logo '    ' clear
    sleep 0.07
    printf '\033[5A'
    render_logo '' clear
    sleep 0.07
    printf '\033[5A'
    render_logo '  ' clear
    sleep 0.07
    printf '\033[5A'
    render_logo '' clear
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
    stream_text 'HX is a terminal coding agent that works inside your project.'
    stream_text 'It reads and edits files, runs commands in a sandbox, and handles larger tasks with persistent sessions, skills, tools, and subagents.'
    stream_text 'Connect to models through OpenRouter and see context, cost, and usage for every turn.'
    printf '  Questions or issues? Contact \033[1;33m%s\033[0m\n\n' "$HX_SUPPORT_EMAIL"
}

ensure_uv() {
    if have uv; then
        log "uv found: $(uv --version)"
        return
    fi
    log "Installing uv"
    have curl || have wget || die "need curl or wget to bootstrap uv"
    if have curl; then
        curl -LsSf "$UV_INSTALLER" | sh
    else
        wget -qO- "$UV_INSTALLER" | sh
    fi
    # The uv installer drops binaries here but does not touch the current shell.
    for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        [ -x "$candidate/uv" ] && PATH="$candidate:$PATH"
    done
    export PATH
    have uv || die "uv installed but not on PATH; open a new shell and re-run"
}

install_hx() {
    log "Installing $HX_PACKAGE (Python $HX_PYTHON)"
    uv tool install --python "$HX_PYTHON" --force "$HX_PACKAGE"
}

check_path() {
    have hx && return
    bindir="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
    warn "hx is not on your PATH yet."
    # shellcheck disable=SC2016  # $PATH is literal text for the user to copy.
    printf '\n  Add this to your shell profile:\n\n    export PATH="%s:$PATH"\n\n' "$bindir"
}

main() {
    banner
    ensure_uv
    install_hx
    check_path
    log "Done. Run 'hx' inside a project directory."
}

main "$@"
