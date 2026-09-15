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
