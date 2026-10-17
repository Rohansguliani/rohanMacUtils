#!/bin/sh
# Top-level mactools installer: automatically selects macos26/ or macos27/ from sw_vers.
#   ./install.sh                     install all packages for this macOS version
#   ./install.sh alttab spaceswitch  install specific packages
#   ./install.sh --uninstall [names] stop and remove LaunchAgents
set -eu
root=$(cd "$(dirname "$0")" && pwd)
major=$(sw_vers -productVersion | cut -d. -f1)
if [ "$major" -ge 27 ] 2>/dev/null; then
    target="$root/macos27"
else
    target="$root/macos26"
fi
echo "Detected macOS $(sw_vers -productVersion) -> using $(basename "$target")/"
exec "$target/install.sh" "$@"
