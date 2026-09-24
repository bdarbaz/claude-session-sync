#!/usr/bin/env bash
# claude-session-sync installer for macOS and Linux.
#
#   Install:    bash install.sh
#               curl -fsSL https://raw.githubusercontent.com/bdarbaz/claude-session-sync/main/install.sh | bash
#   Uninstall:  bash ~/.claude-session-sync/install.sh --uninstall
#
# Puts claude_session_sync.py in ~/.claude-session-sync and runs it in the background
# at login: a LaunchAgent on macOS, a systemd user service on Linux (or an XDG
# autostart entry when systemd --user is not available).
set -euo pipefail

REPO_RAW="${CLAUDE_SESSION_SYNC_RAW:-https://raw.githubusercontent.com/bdarbaz/claude-session-sync/main}"
NAME="claude-session-sync"
LABEL="local.claude-session-sync"
DIR="$HOME/.claude-session-sync"
SCRIPT="$DIR/claude_session_sync.py"
OS="$(uname -s)"

PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$NAME.service"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$NAME.desktop"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

have_systemd_user() {
  command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

stop_loose_processes() {
  pkill -f "claude_session_sync.py --loop" >/dev/null 2>&1 || true
}

stop_launch_agent() {
  local domain="gui/$(id -u)"
  launchctl bootout "$domain/$LABEL" >/dev/null 2>&1 || true
  # bootout returns before the job is gone; bootstrap fails while it still exists.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    launchctl print "$domain/$LABEL" >/dev/null 2>&1 || return 0
    sleep 0.5
  done
}

uninstall() {
  case "$OS" in
    Darwin)
      stop_launch_agent
      rm -f "$PLIST"
      ;;
    Linux)
      if have_systemd_user; then
        systemctl --user disable --now "$NAME.service" >/dev/null 2>&1 || true
      fi
      rm -f "$UNIT" "$AUTOSTART"
      have_systemd_user && systemctl --user daemon-reload >/dev/null 2>&1 || true
      ;;
  esac
  stop_loose_processes
  say "Uninstalled. Backups, trash and logs are still in $DIR (delete it if you don't need them)."
}

find_python() {
  # Absolute, stable paths first: the service runs without your shell's PATH.
  local c
  for c in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    c="$(command -v "$c" 2>/dev/null)" || continue
    if [ "$c" = /usr/bin/python3 ] && [ "$OS" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then
      continue  # the stub would pop up an installer dialog instead of running
    fi
    # Print the real interpreter, not a shim: if the shim's backing tools go away later,
    # the service then fails quietly instead of popping up an install dialog every few seconds.
    if c="$("$c" -c 'import sys; assert sys.version_info >= (3, 8); print(sys.executable)' 2>/dev/null)" \
        && [ -x "$c" ]; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  return 1
}

fetch_script() {
  local here=""
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  fi
  if [ -n "$here" ] && [ -f "$here/claude_session_sync.py" ]; then
    cp "$here/claude_session_sync.py" "$SCRIPT.new"
  elif command -v curl >/dev/null 2>&1; then
    curl -fsSL "$REPO_RAW/claude_session_sync.py" -o "$SCRIPT.new"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$SCRIPT.new" "$REPO_RAW/claude_session_sync.py"
  else
    die "need curl or wget to download claude_session_sync.py"
  fi
  "$PY" -m py_compile "$SCRIPT.new" || die "downloaded script does not compile"
  mv "$SCRIPT.new" "$SCRIPT"
  rm -rf "$DIR/__pycache__"
  # Keep a copy of this installer so "bash ~/.claude-session-sync/install.sh --uninstall" works.
  if [ -n "$here" ] && [ -f "$here/install.sh" ] && [ "$here" != "$DIR" ]; then
    cp "$here/install.sh" "$DIR/install.sh"
  elif [ -z "$here" ] && command -v curl >/dev/null 2>&1; then
    curl -fsSL "$REPO_RAW/install.sh" -o "$DIR/install.sh" || true
  fi
}

install_macos() {
  mkdir -p "$(dirname "$PLIST")"
  [ -w "$(dirname "$PLIST")" ] || die "$(dirname "$PLIST") is not writable; fix with: sudo chown \"\$USER\" ~/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$SCRIPT</string>
    <string>--loop</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$DIR/service.log</string>
  <key>StandardErrorPath</key><string>$DIR/service.log</string>
</dict>
</plist>
EOF
  plutil -lint "$PLIST" >/dev/null || die "generated plist is invalid"
  stop_launch_agent
  launchctl enable "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST" || die "launchctl bootstrap failed"
  say "Background service: LaunchAgent $LABEL"
}

install_linux() {
  if have_systemd_user; then
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT" <<EOF
[Unit]
Description=Mirror Claude desktop Code sessions between accounts

[Service]
ExecStart="$PY" "$SCRIPT" --loop
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable "$NAME.service" >/dev/null
    systemctl --user restart "$NAME.service"
    say "Background service: systemd user unit $NAME.service"
  else
    mkdir -p "$(dirname "$AUTOSTART")"
    cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=claude-session-sync
Comment=Mirror Claude desktop Code sessions between accounts
Exec="$PY" "$SCRIPT" --loop
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF
    nohup "$PY" "$SCRIPT" --loop >/dev/null 2>&1 &
    say "Background service: autostart entry $AUTOSTART (started now)"
  fi
}

main() {
  case "$OS" in
    Darwin|Linux) ;;
    *) die "this installer is for macOS and Linux; on Windows use install.ps1" ;;
  esac
  case "${1:-}" in
    "") ;;
    --uninstall) uninstall; return ;;
    *) die "usage: bash install.sh [--uninstall]" ;;
  esac
  [ "$(id -u)" -ne 0 ] || die "run this as your normal user, not with sudo"

  PY="$(find_python)" || {
    if [ "$OS" = Darwin ]; then
      die "Python 3.8+ not found. Run 'xcode-select --install', then run this installer again."
    fi
    die "Python 3.8+ not found. Install python3 with your package manager, then run this installer again."
  }

  mkdir -p "$DIR"
  fetch_script
  stop_loose_processes
  case "$OS" in
    Darwin) install_macos ;;
    Linux) install_linux ;;
  esac

  "$PY" "$SCRIPT" --status || true
  say ""
  say "Installed. Quit the Claude app completely once and reopen it to see the sessions."
  say "Log: $DIR/sync.log"
}

main "$@"
