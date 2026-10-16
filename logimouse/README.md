# logimouse

Logitech MX mice without Logi Options+: the **gesture button** and the **thumb wheel**
become keystrokes you choose, and the main wheel can be inverted independently of the
trackpad. Talks HID++ 2.0 to the mouse directly over the Bolt/Unifying receiver or
Bluetooth; nothing from Logitech needs to be installed, and the daemon undoes its
remapping on exit so the mouse is never left in a strange state.

Defaults: gesture button → Mission Control, thumb wheel → Ctrl+←/→ (move a Space; pairs
well with `spaceswitch`), main wheel untouched.

## Config — `~/.config/mactools/logimouse.json`

```json
{
  "gesture_button": "missioncontrol",
  "thumb_wheel_left": "ctrl+left",
  "thumb_wheel_right": "ctrl+right",
  "thumb_wheel_cooldown_ms": 120,
  "invert_main_wheel": false,
  "main_wheel_lines_per_detent": 1,
  "debug": false
}
```

Any hotkey may be `""` to leave that control alone (the mouse's default behaviour
returns). `invert_main_wheel` flips only mouse scrolling — trackpad direction is
unchanged, which the system "natural scrolling" checkbox cannot do.

## Install

```sh
./install.sh            # install / restart
./install.sh --uninstall
```

Permissions for `/usr/bin/python3`: **Input Monitoring** (to open the HID++ interface)
and **Accessibility** (to post the keystrokes). See the top-level README.

## Supported hardware

Any HID++ 2.0 mouse exposing feature `0x1B04` (Reprogrammable Controls v4). Developed
against an MX Master 2S; the other MX / Lift / M720-class mice expose the same feature. The
daemon reads the device's own control table at start and diverts exactly one control, the
gesture button (CID `0x00C3`) — never middle/back/forward/SmartShift, which are divertable
too and would silently stop working. The table is logged on attach (`*` = divertable).
Receiver, Bluetooth, and USB-C cable paths are handled (a mouse on Bluetooth is preferred
over an idle receiver), the daemon re-attaches when the mouse sleeps and wakes, and it gives
the button back to the mouse when it is stopped.
