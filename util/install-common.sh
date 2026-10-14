#!/bin/sh
# mactools shared installer. Each package's install.sh is a one-line wrapper:
#     exec "$(dirname "$0")/../util/install-common.sh" <name> "$@"
#
# Installs ~/Library/LaunchAgents/com.mactools.<name>.plist pointing at THIS checkout
# (no copying: `git pull` updates the daemon; restart with install.sh again), writes a
# default config to ~/.config/mactools/<name>.json if absent, and (re)starts the agent.
#
#   install.sh            install or restart
#   install.sh --uninstall   stop and remove the LaunchAgent (config and logs are kept)
set -eu

name=${1:?usage: install-common.sh <name> [--uninstall]}
shift
root=$(cd "$(dirname "$0")/.." && pwd)
script="$root/$name/$name.py"
label="com.mactools.$name"
plist="$HOME/Library/LaunchAgents/$label.plist"
log="$HOME/Library/Logs/mactools-$name.log"
python=/usr/bin/python3
uid=$(id -u)

[ -f "$script" ] || { echo "no such package: $script" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
    rm -f "$plist"
    echo "removed $label (kept ~/.config/mactools/$name.json and $log)"
    exit 0
fi

# Apple's /usr/bin/python3 is a shim that needs the Command Line Tools installed.
if ! "$python" -c 'import ctypes' 2>/dev/null; then
    echo "$python is not usable: install the Command Line Tools first (xcode-select --install)" >&2
    exit 1
fi

# Fail now on a bad config rather than looping in launchd.
"$python" "$script" --check-config

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
        <string>$python</string>
        <string>$script</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    <key>StandardOutPath</key>
    <string>$log</string>
    <key>StandardErrorPath</key>
    <string>$log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$uid/$label" 2>/dev/null || true
# bootout returns before the job is gone; bootstrapping the same label too early fails.
i=0
while launchctl print "gui/$uid/$label" >/dev/null 2>&1 && [ $i -lt 50 ]; do
    sleep 0.1; i=$((i + 1))
done
launchctl bootstrap "gui/$uid" "$plist"
launchctl kickstart -k "gui/$uid/$label"
echo "$label running from $script"
echo "config: ~/.config/mactools/$name.json   log: $log"
echo "If this is the first mactools daemon: System Settings > Privacy & Security > Accessibility > + >"
echo "press Cmd+Shift+G, type /usr/bin/python3, Add, enable. The daemon waits for the grant (see the log)."
