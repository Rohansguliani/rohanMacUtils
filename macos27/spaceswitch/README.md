# spaceswitch (macOS 27)

Makes switching Spaces instant. Your existing shortcuts and gestures keep working — this
only removes the slide animation (and the ~0.5 s you cannot type during it).

**macOS 27 differences** (the Dock now reads the IOHID event attached to the gesture, not
the CGEvent fields):

- The first horizontal swipe after every Dock start is native (animated): that swipe is
  *recorded* and its payload is replayed, with the numbers patched, for every switch after
  it. The recording is cached in `~/.cache/mactools/spaceswitch27.json` per Dock pid.
- A forged switch is a flick (`Began(~0) → Ended(~0)` at terminal velocity); it commits in
  ~40 ms and moves exactly one Space. Progress 1.0 recipes move several Spaces on 27.
- **Mission Control is left native for now.** No forged vertical sequence is accepted by the
  macOS 27 Dock yet (`trackpad_vertical` is ignored; the shortcut and the 4-finger swipe
  pass straight through).

- **Keyboard** — whatever you have bound in System Settings → Keyboard → Shortcuts →
  Mission Control for *Move left/right a space* (default Ctrl+←/→) switches instantly.
  Holding the key repeats at a controlled rate. At the first/last Space nothing happens
  (no rubber-band bounce).
- **Trackpad** — 3- or 4-finger horizontal swipes switch instantly, one Space per swipe,
  honouring your natural-scrolling setting.

spaceswitch reads the bindings from `com.apple.symbolichotkeys` and picks up changes
within a few seconds, so there is nothing to configure twice.

## Config — `~/.config/mactools/spaceswitch.json`

```json
{
  "keyboard": true,
  "trackpad_horizontal": true,
  "trackpad_vertical": true,
  "extra_switch_modifiers": [],
  "key_repeat_interval_ms": 120,
  "debug": false
}
```

`extra_switch_modifiers` adds alternative modifiers for ←/→ on top of the system
shortcut, e.g. `["cmd"]` makes Cmd+←/→ switch as well. (Think before doing that: Cmd+← is
"go back" in browsers.)

## Install

```sh
./install.sh            # install / restart
./install.sh --uninstall
```

Permission for `/usr/bin/python3`: **Accessibility**. See the top-level README.

## How it works, and why it may break

macOS switches Spaces with animation when it receives a *gesture* — a Dock-swipe record
with a begin/progress/end phase. spaceswitch swallows your key or swipe and posts the
same record with the progress already at 100 %, so WindowServer commits the switch in one
frame. The record is built from private `CGEvent` fields (55, 110, 123, 124, 129, 130,
132) and the private event types 29/30 that the trackpad driver itself uses; the values
are listed at the top of `spaceswitch.py` and were verified on macOS 26.x. If a future
macOS changes them, the daemon stops intercepting and logs why, and your shortcuts fall
back to the normal animated behaviour.

The current Space index for edge clamping comes from `SLSCopyManagedDisplaySpaces`; if
that symbol is missing the clamp is disabled and everything else still works.
