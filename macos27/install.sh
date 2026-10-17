#!/bin/sh
# Install every package, or the ones you name.
#   ./install.sh                     all four
#   ./install.sh alttab snap         just those
#   ./install.sh --uninstall [names] stop and remove them (configs and logs are kept)
set -eu
root=$(cd "$(dirname "$0")" && pwd)
all="alttab snap shot spaceswitch logimouse"
flag=
[ "${1:-}" = "--uninstall" ] && { flag=--uninstall; shift; }
names=${*:-$all}
for n in $names; do
    [ -x "$root/$n/install.sh" ] || { echo "no such package: $n (have: $all)" >&2; exit 1; }
done
for n in $names; do
    echo "== $n"
    "$root/$n/install.sh" $flag
done
