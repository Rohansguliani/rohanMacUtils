# snap

Rectangle-style window snapping, plus a few things Rectangle does not do.

- **Opt+←/→/↑/↓** — focused window to that half of the screen (visible frame: menu bar and
  Dock respected). **Opt+A** — fill the screen.
- **Drag to an edge** — drag any window until the cursor touches the left or right screen
  edge: a translucent footprint shows the target half; release to snap.
- **Companion snap** — after a half-snap, if exactly one other window is on the Space it
  takes the other half automatically; with two or more, a picker (thumbnail cards) appears
  in the empty half: Tab / arrows + Return, or click a card; Esc dismisses.
- **Linked resize** — drag the seam between two touching windows and the neighbour follows.
  **Opt+Shift+=** / **Opt+Shift+-** grow or shrink the focused window's width by 5 % (hold
  to repeat); a touching neighbour follows there too.

## Config — `~/.config/mactools/snap.json`

```json
{
  "snap_left": "opt+left",
  "snap_right": "opt+right",
  "snap_top": "opt+up",
  "snap_bottom": "opt+down",
  "snap_full": "opt+a",
  "width_grow": "opt+shift+=",
  "width_shrink": "opt+shift+-",
  "width_step_percent": 5,
  "drag_to_edge": true,
  "companion_snap": true,
  "linked_resize": true,
  "debug": false
}
```

Any hotkey may be `""` to disable it; the three booleans switch off the mouse features.
With `drag_to_edge` and `linked_resize` both off the daemon does not watch the mouse at all.

## Install

```sh
./install.sh            # install / restart
./install.sh --uninstall
```

Permissions for `/usr/bin/python3`: **Accessibility** (hotkeys, drag tracking, moving
windows). **Screen Recording** only matters for the picker's thumbnails. See the top-level
README for the ⌘⇧G trick.

## Notes

- Window moves and resizes are public Accessibility calls (`AXPosition` / `AXSize`), which
  is why some apps (Chrome, Electron) take 4–16 ms per change — the linked resize paces
  itself to one layout per frame so it never backlogs.
- The window list and the picker come from `util/windows.py` / `util/cards.py` (SkyLight
  SPI; see alttab's README). `SLSGetWindowBounds` is used for the ~0.3 ms neighbour search
  during a drag. Tested on macOS 26.x.
