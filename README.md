# rohanMacUtils (`mactools`)

Small, dependency-free macOS daemons that replace AltTab, Rectangle, Shottr, instant Space
switching / Mission Control, and Logitech Options+ with plain `/usr/bin/python3` + `ctypes`
scripts running through `launchd`. Zero third-party binaries, zero Homebrew, Santa-safe.

## Repository Structure by macOS Version

| Folder | Target OS | Notes |
|---|---|---|
| [`macos26/`](macos26/) | **macOS 26.x** | Original CGS `DockSwipe` / `IOHIDFluidTouchGestureData` (`type=23`) event format |
| [`macos27/`](macos27/) | **macOS 27.x+** | Updated for macOS 27 `WindowManager.SystemGestureManager` + `Gestures.BinaryEncoder` (`VendorDefined 0xFF00/0x0070` `OpaqueDataEvent`) |

## Install

```sh
git clone https://github.com/Rohansguliani/rohanMacUtils ~/mactools
~/mactools/install.sh                        # auto-detects macOS 26 vs 27+ and installs all 5 tools
~/mactools/install.sh spaceswitch logimouse  # install specific tools
~/mactools/install.sh --uninstall            # stop and remove all LaunchAgents
```

## Packages

| Package | Replaces | Description | Permissions |
|---|---|---|---|
| `alttab/` | AltTab | Opt+Tab window switcher scoped to the current Space, live thumbnails, frozen MRU order | Accessibility, Screen Recording |
| `snap/` | Rectangle | Opt+arrows halves, drag-to-edge, companion auto-snap / picker, linked seam resize | Accessibility |
| `shot/` | Shottr | Opt+1 area → clipboard, Opt+2 area → OCR text, Opt+` annotate last capture | Accessibility, Screen Recording |
| `spaceswitch/` | Space Rabbit / ISS | Instant zero-animation Space switching & Mission Control on trackpad swipes, keyboard shortcuts, and mouse buttons | Accessibility |
| `logimouse/` | Logi Options+ | Logitech MX Master gesture button, thumb wheel Space switching, and scroll inversion over HID++ | Input Monitoring, Accessibility |
