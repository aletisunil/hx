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
UV_INSTALLER="https://astral.sh/uv/install.sh"

log()  { printf '\033[0;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[0;33mwarning:\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[0;31merror:\033[0m %s\n' "$1" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

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
    printf '\n  Add this to your shell profile:\n\n    export PATH="%s:$PATH"\n\n' "$bindir"
}

main() {
    ensure_uv
    install_hx
    check_path
    log "Done. Run 'hx' inside a project directory."
}

main "$@"
