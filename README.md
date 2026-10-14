# mactools

Small, dependency-free macOS daemons that replace AltTab, Rectangle, Shottr, and parts of
Logitech Options+ with a few hundred lines of Python each. No app bundle, no code signing, no
Homebrew: every package is one script running under Apple's `/usr/bin/python3` through a
LaunchAgent.

| package | replaces | what it does | permissions |
|---|---|---|---|
| [`alttab/`](alttab/) | AltTab | Opt+Tab window switcher scoped to the current Space, live thumbnails, frozen MRU order | Accessibility, Screen Recording |
| [`snap/`](snap/) | Rectangle | Opt+arrows halves, drag-to-edge, companion auto-snap / picker, linked seam resize | Accessibility |
| [`shot/`](shot/) | Shottr | Opt+1 area → clipboard, Opt+2 area → OCR text, Opt+` area → annotate (arrow, box, pen, text, pixelate) | Accessibility, Screen Recording |
| [`spaceswitch/`](spaceswitch/) | — | Instant Spaces switching: removes the slide animation from your shortcuts and swipes, instant Mission Control | Accessibility |
| [`logimouse/`](logimouse/) | Logi Options+ | Logitech MX gesture button and thumb wheel as keystrokes, optional main-wheel inversion — HID++ directly, no Logi software | Input Monitoring, Accessibility |

Packages are independent: install one, or all five. The only shared code is `util/`.

## Install

```sh
git clone https://github.com/Rohansguliani/rohanMacUtils ~/mactools
~/mactools/install.sh                  # everything
~/mactools/install.sh alttab snap      # or just some
```

Each package also has its own `<name>/install.sh`, and `install.sh --uninstall [names]`
removes them again.

Requirements: an Apple Silicon Mac (the ObjC bridge assumes the arm64 calling convention)
with the Command Line Tools (`xcode-select --install`; that is what makes
`/usr/bin/python3` real). Developed and tested on macOS 26.x. Nothing else.

`<name>/install.sh` writes `~/Library/LaunchAgents/com.mactools.<name>.plist` pointing at the
checkout (so `git pull` + re-running `install.sh` is an upgrade), creates
`~/.config/mactools/<name>.json` with the defaults, and starts the daemon. Logs go to
`~/Library/Logs/mactools-<name>.log`. `<name>/install.sh --uninstall` stops it and removes the
LaunchAgent.

### Granting permissions

The daemons run as `/usr/bin/python3`, so that is the binary you grant. For each
permission the package lists: System Settings → Privacy & Security → *(permission)* →
**+** → press **⌘⇧G**, type `/usr/bin/python3`, **Add**, enable it. The daemon waits and
logs until the grant appears; no restart needed.

One grant covers all packages. If a daemon later logs that it lost its permission while
System Settings still shows it enabled, that is a known macOS quirk with hardlinked
binaries (`/usr/bin/python3` shares its inode with ~78 other tools); the daemons detect it
and restart themselves.

## Configuration

Every package reads `~/.config/mactools/<name>.json`. Keys and defaults are documented in
each package's README and in the `DEFAULTS` table at the top of each script. Unknown keys
or wrong types fail loudly at start (`<name>.py --check-config` to test). Hotkeys are
strings like `"opt+tab"`, `"opt+shift+="`, `"ctrl+cmd+right"`; `""` disables a binding.

## What makes this fragile

These tools do things Apple does not expose: instant Space switching posts the same
private Dock-swipe records the trackpad driver posts; window thumbnails come from
SkyLight's `CGSHWCaptureWindowList`; raising a window in another app uses
`_SLPSSetFrontProcessWithOptions`. The symbols and event field numbers used are the same
ones AltTab, yabai, and Rectangle have shipped against for years, and each script lists
them at the top as macOS-version-specific constants. If a macOS update breaks one, the
daemon logs which call failed and leaves the system behaviour alone rather than
half-working.

## Layout

```
install.sh    install / uninstall any set of packages
util/         shared code only — nothing here is a daemon
  macbridge.py   ctypes bindings: CoreFoundation / CoreGraphics / HIServices / SkyLight
  objc.py        objc_msgSend bridge + the SkyLight focus SPI (AppKit users only)
  config.py      ~/.config/mactools/<name>.json loader + hotkey parser
  daemon.py      event-tap skeleton: Accessibility wait, tap, health timer, TCC respawn
  windows.py     current-Space window list, MRU, GPU thumbnails, cross-app focus (alttab, snap)
  cards.py       the thumbnail-card HUD (alttab, snap's picker)
  imaging.py     screen capture, PNG/TIFF, pasteboard, Vision OCR, pixelate, layer flatten (shot)
  install-common.sh
<name>/       <name>.py, install.sh, README.md
```

## License

MIT.
