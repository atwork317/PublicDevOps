#!/usr/bin/env bash
#
# setup-claude-termux.sh
# ----------------------------------------------------------------------------
# One-shot installer for Claude Code on Android / Termux (Galaxy S24, aarch64).
#
# WHY THIS EXISTS:
#   The standalone Claude Code binary is built against GNU glibc. Android uses
#   Bionic libc, so that binary can never run natively on Termux -- symlinking
#   libc.so, patching ld-linux-aarch64.so.1, or reinstalling glibc all fail with
#   "invalid ELF header" / "version 'LIBC' not found" / ETIMEDOUT. The fix is to
#   NOT run the glibc binary. This script installs Claude Code as JavaScript
#   under a real Node runtime instead.
#
# TWO MODES:
#   --proot   (default) Installs a Debian proot (real glibc userland) and runs
#             Claude Code inside it. Removes the entire glibc/Bionic problem
#             class. Most reliable. Costs ~500MB.
#   --native  Installs Termux's Bionic-native Node and runs Claude Code under it.
#             Lighter, but needs the IPv4-first DNS workaround and the
#             auto-updater disabled so it never swaps in the broken glibc binary.
#
# USAGE:
#   bash setup-claude-termux.sh            # defaults to --proot
#   bash setup-claude-termux.sh --proot
#   bash setup-claude-termux.sh --native
#
# Safe to re-run. Verifies `claude --version` at the end and exits non-zero on
# failure, so a printed SUCCESS means Claude Code really is installed.
# ----------------------------------------------------------------------------

set -euo pipefail

# --- pretty logging ---------------------------------------------------------
c_grn=$'\033[32m'; c_red=$'\033[31m'; c_yel=$'\033[33m'; c_rst=$'\033[0m'
log()  { printf '%s[*]%s %s\n' "$c_grn" "$c_rst" "$*"; }
warn() { printf '%s[!]%s %s\n' "$c_yel" "$c_rst" "$*"; }
die()  { printf '%s[x] %s%s\n' "$c_red" "$*" "$c_rst" >&2; exit 1; }

MODE="proot"
for arg in "$@"; do
  case "$arg" in
    --proot)  MODE="proot" ;;
    --native) MODE="native" ;;
    -h|--help)
      sed -n '2,40p' "$0"; exit 0 ;;
    *) die "Unknown argument: $arg (use --proot or --native)" ;;
  esac
done

# --- sanity: are we actually in Termux? ------------------------------------
command -v pkg >/dev/null 2>&1 || die "This must be run inside Termux (no 'pkg' found)."
[ -n "${PREFIX:-}" ] || die "\$PREFIX is unset -- run this from a normal Termux shell."
case "${PREFIX}" in
  *com.termux*) : ;;
  *) warn "\$PREFIX does not look like Termux ($PREFIX) -- continuing anyway." ;;
esac

# Node's minimum for Claude Code.
NODE_MIN_MAJOR=18

node_major() { node -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/'; }

# ---------------------------------------------------------------------------
# NATIVE MODE
# ---------------------------------------------------------------------------
install_native() {
  log "Mode: native (Termux Bionic Node)"

  log "Updating Termux packages..."
  pkg update -y && pkg upgrade -y

  if ! command -v node >/dev/null 2>&1; then
    log "Installing nodejs-lts..."
    pkg install -y nodejs-lts || pkg install -y nodejs
  else
    log "Node already present: $(node -v)"
  fi
  command -v node >/dev/null 2>&1 || die "Node failed to install."

  local maj; maj="$(node_major)"
  [ -n "$maj" ] && [ "$maj" -ge "$NODE_MIN_MAJOR" ] \
    || die "Node $maj is too old; Claude Code needs >= $NODE_MIN_MAJOR. Try: pkg upgrade nodejs-lts"

  # --- DNS + auto-updater workarounds, guarded so we never corrupt .bashrc ---
  local rc="$HOME/.bashrc"
  touch "$rc"
  add_line() { grep -qF -- "$1" "$rc" || printf '%s\n' "$1" >> "$rc"; }
  log "Ensuring DNS + auto-updater settings in ~/.bashrc..."
  add_line '# --- Claude Code (Termux) settings ---'
  add_line 'export NODE_OPTIONS="--dns-result-order=ipv4first"'
  add_line 'export DISABLE_AUTOUPDATER=1'
  # Apply to THIS shell too so the verify step below can reach the network.
  export NODE_OPTIONS="--dns-result-order=ipv4first"
  export DISABLE_AUTOUPDATER=1

  log "Installing @anthropic-ai/claude-code via npm..."
  npm install -g @anthropic-ai/claude-code

  verify_native
}

verify_native() {
  log "Verifying claude..."
  if command -v claude >/dev/null 2>&1 && claude --version >/dev/null 2>&1; then
    printf '%sSUCCESS%s: %s\n' "$c_grn" "$c_rst" "$(claude --version)"
    cat <<EOF

Done. Open a fresh Termux session (so ~/.bashrc loads) and run:

    claude

If you ever see ETIMEDOUT again, confirm the env is live:
    echo \$NODE_OPTIONS   # expect: --dns-result-order=ipv4first
EOF
  else
    die "claude installed but 'claude --version' failed. Re-open Termux and retry, or run with --proot."
  fi
}

# ---------------------------------------------------------------------------
# PROOT MODE
# ---------------------------------------------------------------------------
DISTRO="debian"

run_in_distro() { proot-distro login "$DISTRO" -- bash -lc "$1"; }

install_proot() {
  log "Mode: proot (Debian glibc userland)"

  log "Updating Termux packages..."
  pkg update -y && pkg upgrade -y

  log "Installing proot-distro..."
  pkg install -y proot-distro

  if proot-distro list --installed 2>/dev/null | grep -qw "$DISTRO"; then
    log "Debian proot already installed."
  else
    log "Installing Debian rootfs (this downloads ~few hundred MB)..."
    proot-distro install "$DISTRO"
  fi

  log "Installing Node + Claude Code inside Debian..."
  run_in_distro '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y curl ca-certificates gnupg
    if ! command -v node >/dev/null 2>&1; then
      # Prefer NodeSource 20; fall back to Debian repo nodejs (>=18) if it fails.
      if curl -fsSL https://deb.nodesource.com/setup_20.x | bash - ; then
        apt-get install -y nodejs
      else
        echo "NodeSource failed; falling back to distro nodejs/npm."
        apt-get install -y nodejs npm
      fi
    fi
    node -v
    npm install -g @anthropic-ai/claude-code
  '

  install_proot_launcher
  verify_proot
}

# Convenience launcher so you can just type `claude` from Termux and land in the
# Debian proot with your CURRENT directory bound and selected.
install_proot_launcher() {
  local launcher="$PREFIX/bin/claude"
  log "Installing 'claude' launcher at $launcher ..."
  cat > "$launcher" <<'LAUNCH'
#!/usr/bin/env bash
# Runs Claude Code inside the Debian proot, in your current Termux directory.
set -euo pipefail
CWD="$(pwd)"
exec proot-distro login debian --bind "$CWD:$CWD" -- \
  bash -lc "cd \"$CWD\" && exec claude \"\$@\"" _ "$@"
LAUNCH
  chmod +x "$launcher"
}

verify_proot() {
  log "Verifying claude inside Debian..."
  if run_in_distro 'claude --version' >/tmp/claude_ver 2>/dev/null; then
    printf '%sSUCCESS%s: %s\n' "$c_grn" "$c_rst" "$(cat /tmp/claude_ver)"
    rm -f /tmp/claude_ver
    cat <<EOF

Done. From any Termux directory just run:

    claude

(That launcher drops you into the Debian glibc proot with the current folder
bound, so Claude Code sees your files.) To poke around Debian directly:

    proot-distro login debian
EOF
  else
    rm -f /tmp/claude_ver
    die "Claude Code did not verify inside Debian. Re-run this script; installs are resumable."
  fi
}

# ---------------------------------------------------------------------------
main() {
  log "Claude Code on Termux -- target: $(uname -m), mode: $MODE"
  case "$MODE" in
    proot)  install_proot ;;
    native) install_native ;;
  esac
}
main
