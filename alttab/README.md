# alttab

An Opt+Tab window switcher that only shows the windows on the Space you are looking at.

- **Opt+Tab** — cycle windows on the *current* Space, most-recently-used first, with live
  thumbnails. Hold Opt; Tab / Shift+Tab / arrows / J / K move the highlight; release Opt to
  switch. **Esc** cancels. **W** closes the highlighted window. **Shift+Opt+Tab** starts
  from the end.
- **Opt+`** — the same, limited to the frontmost app's windows. Pressing ` while the
  switcher is open toggles between the two scopes.
- Minimized windows and other Spaces never appear. The order is frozen while the switcher
  is open, so fast Tab-Tab-Tab never reorders under you.
- Summon → painted in well under a millisecond once warm: thumbnails are pre-captured in
  the background and go straight from the GPU into the cards.

## Config — `~/.config/mactools/alttab.json`

```json
{
  "switch_windows": "opt+tab",
  "switch_app_windows": "opt+`",
  "columns": 4,
  "max_cards": 12
}
```

Hotkeys are strings like `"opt+tab"`, `"ctrl+opt+tab"`; the switcher commits when any
modifier in the hotkey is released. `switch_app_windows` may be `""` to disable it. (If you
also run `shot`, which takes Opt+` by default, set this to `"opt+shift+`"` or move one of
them.)

## Install

```sh
./install.sh            # install / restart
./install.sh --uninstall
```

Permissions for `/usr/bin/python3`: **Accessibility** (the hotkey tap and raising windows)
and **Screen Recording** (thumbnails and window titles; without it the cards show app icons
only). See the top-level README for the ⌘⇧G trick.

## Notes

- `alttab.py --test` prints enumeration and render latencies without installing a tap.
- Everything that touches other apps' windows lives in `util/windows.py` and is SkyLight
  SPI: `SLSWindowQuery*` (the per-Space window list, ~0.2 ms), `CGSHWCaptureWindowList`
  (GPU thumbnails), `_SLPSSetFrontProcessWithOptions` + `SLPSPostEventRecordTo` (raise one
  window of another app without bringing its siblings along). The same calls AltTab.app and
  yabai use. Tested on macOS 26.x.
