#!/usr/bin/python3
"""spaceswitch: Space switching and Mission Control with no animation.

How: the Dock listens for private "DockSwipe" gesture events from the trackpad. We
intercept the real ones at the session event tap (and the Spaces / Mission Control
keyboard shortcuts) and post a forged gesture that claims the finger already travelled
the full distance at terminal velocity — the Dock has nothing left to animate and commits
immediately.

What's handled
  - the Spaces shortcuts you have configured in System Settings > Keyboard > Shortcuts >
    Mission Control (read live from com.apple.symbolichotkeys; default Ctrl+Left/Right)
  - the Mission Control shortcut (default Ctrl+Up) and the dedicated Mission Control key
  - 3/4-finger horizontal trackpad swipes -> instant Space switch
  - 4-finger vertical trackpad swipes -> instant Mission Control in/out

Edge Spaces are clamped (no rubber-band bounce). Inside Mission Control / App Expose the
horizontal swipe drives the overview's own carousel and is left native.

Fragility, stated plainly: this drives undocumented WindowServer/Dock event fields
(numbers below). Tested on macOS 26.x. A macOS update can break it; when it does, the
daemon keeps running but the forged events do nothing and macOS's own animated switch
still works, because we only swallow the trigger after a successful post.

Permissions: Accessibility (granted to /usr/bin/python3).
"""

from __future__ import annotations

import ctypes as C
import os
import plistlib
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "util"))
import config  # noqa: E402
import daemon  # noqa: E402
from macbridge import (CF, CG, CS, LIBSYS, SL, CID, FLAG_CTRL, MODIFIER_MASK, cf_to_i64, cf_to_str, cfstr,  # noqa: E402
                       kCFPreferencesAnyApplication, kCGEventKeyDown, kCGEventKeyUp, kCGEventSourceUnixProcessID,
                       kCGEventSourceUserData, kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput,
                       kCGKeyboardEventAutorepeat, kCGKeyboardEventKeycode, kCGSessionEventTap,
                       kCGWindowListOptionOnScreenOnly, K_WIN_LAYER, K_WIN_OWNER, log, vp)

DEFAULTS = {
    "keyboard": True,                 # intercept the Spaces / Mission Control shortcuts
    "trackpad_horizontal": True,      # 3/4-finger left/right swipe
    "trackpad_vertical": True,        # 4-finger up/down swipe (Mission Control)
    "extra_switch_modifiers": [],     # e.g. ["cmd"]: ALSO treat Cmd+Left/Right as switch (on top of the system shortcut)
    "key_repeat_interval_ms": 120,    # min gap between switches while a key is held
    "debug": False,
}
CFG, CFG_PATH = config.load("spaceswitch", DEFAULTS)
TRACE = bool(CFG["debug"])

# --- private CGEvent fields (WindowServer/Dock ABI; reverse-engineered, macOS 12-26) ----
F_CGS_EVENT_TYPE, F_GESTURE_HID_TYPE, F_SWIPE_MOTION = 55, 110, 123
F_SWIPE_PROGRESS, F_SWIPE_VEL_X, F_SWIPE_VEL_Y, F_GESTURE_PHASE = 124, 129, 130, 132
CGS_EVENT_GESTURE, CGS_EVENT_DOCK_CONTROL = 29, 30
HID_DOCK_SWIPE = 23
MOTION_HORIZONTAL, MOTION_VERTICAL = 1, 2
PHASE_BEGAN, PHASE_CHANGED, PHASE_ENDED, PHASE_CANCELLED = 1, 2, 4, 8
FIELD_IOHID_PAYLOAD = 4205
IOHID_TYPE_VELOCITY, IOHID_TYPE_FLUID_TOUCH_GESTURE, IOHID_FLAVOR_DOCK_PRIMARY = 9, 23, 3
DOCK_OVERVIEW_LAYER = 18             # Dock owns a layer-18 window for the life of any overview
CURRENT_OS_SPACES_MASK = (1 << 0) | (1 << 3)
SYNTHETIC_MARKER = 0x53505357        # 'SPSW' in source user data: events we posted
KEY_LEFT, KEY_RIGHT, KEY_UP, KEY_DOWN, KEY_MISSION_CONTROL = 123, 124, 126, 125, 160

# --- tuning (all measured; see docstrings) ---------------------------------------------
INSTANT_VELOCITY = 2000.0            # anything well above the Dock's flick threshold
EPSILON_PROGRESS = 1e-4              # Began progress; FLT_TRUE_MIN flushes to zero on Apple Silicon
SWIPE_DIRECTION_THRESHOLD = 0.05     # |progress| before a real swipe's direction is trusted
MIN_SWITCH_GAP = 0.08                # s between two forged switches (WindowServer re-layering)
MC_EPSILON = 1.0 / 65536.0           # smallest signed 16.16 step
MC_VELOCITY = 9999.0                 # removes Mission Control's final zoom frames on dismissal
MC_GESTURE_TIMEOUT = 0.5             # s before an unresolved held gesture is replayed
PREDICTION_TTL = 0.5                 # s to trust our own "we just moved to index N" over CGS
MC_OWNED_DIRECTION: dict[str | None, int] = {"desktop": 1, "mission-control": -1, "app-expose": 1}

K_DISPLAY_ID, K_SPACES, K_CURRENT, K_ID64 = cfstr("Display Identifier"), cfstr("Spaces"), cfstr("Current Space"), cfstr("id64")
K_SWIPE_SCROLL_DIR = cfstr("com.apple.swipescrolldirection")
CF_NUMBER_TYPE_ID = CF.CFNumberGetTypeID()
CF_BOOL_FALSE = vp.in_dll(CF, "kCFBooleanFalse").value
HAVE_SPACE_NAMES = SL is not None and all(getattr(SL, n, None) for n in ("SLSCopySpaces", "SLSSpaceCopyName"))


def trace(msg):
    if TRACE:
        log(msg)


# --- system shortcuts ----------------------------------------------------------------
class Shortcuts:
    """The user's own Spaces / Mission Control bindings, re-read when the plist changes.

    symbolichotkeys ids: 79 = Move left a space, 81 = Move right a space, 32 = Mission
    Control, 33 = App Expose. Parameters are [char, keycode, modifiers] where modifiers
    use the same NX bits as CGEventFlags.
    """
    PATH = os.path.expanduser("~/Library/Preferences/com.apple.symbolichotkeys.plist")
    DEFAULT: dict[int, tuple[int, int] | None] = {
        79: (KEY_LEFT, FLAG_CTRL), 81: (KEY_RIGHT, FLAG_CTRL), 32: (KEY_UP, FLAG_CTRL), 33: (KEY_DOWN, FLAG_CTRL)}

    def __init__(self):
        self.mtime = None
        self.left = self.right = self.mc = self.expose = None
        self.extra = 0
        for m in CFG["extra_switch_modifiers"]:
            _, f = config.parse_hotkey(f"{m}+left")
            self.extra |= f
        self.refresh()

    def refresh(self):
        try:
            mt = os.stat(self.PATH).st_mtime
        except OSError:
            mt = None
        if mt == self.mtime and self.left is not None:
            return
        self.mtime = mt
        table = dict(self.DEFAULT)
        try:
            with open(self.PATH, "rb") as fh:
                hk = plistlib.load(fh).get("AppleSymbolicHotKeys", {})
            for sid in self.DEFAULT:
                e = hk.get(str(sid))
                if not e:
                    continue
                if not e.get("enabled", True):
                    table[sid] = None
                    continue
                par = e.get("value", {}).get("parameters")
                if par and len(par) >= 3:
                    table[sid] = (int(par[1]), int(par[2]) & MODIFIER_MASK)
        except (OSError, ValueError, plistlib.InvalidFileException) as e:
            log(f"symbolichotkeys unreadable ({e}); using macOS defaults")
        self.left, self.right, self.mc, self.expose = table[79], table[81], table[32], table[33]
        log("shortcuts: left=%s right=%s mission-control=%s%s" % (
            self._fmt(self.left), self._fmt(self.right), self._fmt(self.mc),
            f" (+extra modifiers {self.extra:#x})" if self.extra else ""))

    @staticmethod
    def _fmt(b):
        return "off" if b is None else "key%d/%#x" % b

    def _match(self, binding, key, flags):
        if binding is None or key != binding[0]:
            return False
        if flags == binding[1]:
            return True
        # extra modifier sets: same key, modifiers = (binding's modifiers with ctrl swapped for extra)
        return bool(self.extra) and flags == ((binding[1] & ~FLAG_CTRL) | self.extra)

    def is_left(self, key, flags):
        return self._match(self.left, key, flags)

    def is_right(self, key, flags):
        return self._match(self.right, key, flags)

    def is_mc(self, key, flags):
        return (key == KEY_MISSION_CONTROL and flags == 0) or self._match(self.mc, key, flags)

    def is_expose(self, key, flags):
        return self._match(self.expose, key, flags)


SHORTCUTS = Shortcuts()


# --- Space geometry --------------------------------------------------------------------
def cursor_display_uuid():
    ev = CG.CGEventCreate(None)
    if not ev:
        return None
    pt = CG.CGEventGetLocation(ev)
    CF.CFRelease(ev)
    did, n = C.c_uint32(0), C.c_uint32(0)
    if CG.CGGetDisplaysWithPoint(pt, 1, C.byref(did), C.byref(n)) != 0 or n.value == 0:
        return None
    uuid = CS.CGDisplayCreateUUIDFromDisplayID(did.value)
    if not uuid:
        return None
    s = CF.CFUUIDCreateString(None, uuid)
    CF.CFRelease(uuid)
    return s


def space_info():
    """(display_id, current_index, count) for the display under the cursor, or None."""
    if SL is None or not getattr(SL, "CGSCopyManagedDisplaySpaces", None):
        return None
    want = cursor_display_uuid()
    displays = SL.CGSCopyManagedDisplaySpaces(CID)
    if not displays:
        if want:
            CF.CFRelease(want)
        return None
    chosen = first = None
    for i in range(CF.CFArrayGetCount(displays)):
        d = CF.CFArrayGetValueAtIndex(displays, i)
        first = first or d
        ident = CF.CFDictionaryGetValue(d, K_DISPLAY_ID)
        if want and ident and CF.CFEqual(ident, want):
            chosen = d
            break
    d = chosen or first
    result = None
    if d:
        cur = CF.CFDictionaryGetValue(d, K_CURRENT)
        cur_id = cf_to_i64(CF.CFDictionaryGetValue(cur, K_ID64)) if cur else 0
        if not cur_id:
            cur_id = SL.CGSGetActiveSpace(CID)
        spaces = CF.CFDictionaryGetValue(d, K_SPACES)
        ids = [cf_to_i64(CF.CFDictionaryGetValue(CF.CFArrayGetValueAtIndex(spaces, j), K_ID64))
               for j in range(CF.CFArrayGetCount(spaces))] if spaces else []
        if cur_id in ids:
            result = (cf_to_str(CF.CFDictionaryGetValue(d, K_DISPLAY_ID)), ids.index(cur_id), len(ids))
    if want:
        CF.CFRelease(want)
    CF.CFRelease(displays)
    return result


_predicted = {}  # display_id -> (index, monotonic): CGS lags a switch by a few ms


def current_index(display_id, reported):
    p = _predicted.get(display_id)
    if p and time.monotonic() - p[1] < PREDICTION_TTL:
        return p[0]
    return reported


def overview_up():
    arr = CG.CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, 0)
    if not arr:
        return None
    try:
        for i in range(CF.CFArrayGetCount(arr)):
            w = CF.CFArrayGetValueAtIndex(arr, i)
            if cf_to_i64(CF.CFDictionaryGetValue(w, K_WIN_LAYER)) == DOCK_OVERVIEW_LAYER \
                    and cf_to_str(CF.CFDictionaryGetValue(w, K_WIN_OWNER)) == "Dock":
                return True
        return False
    finally:
        CF.CFRelease(arr)


def overview_state():
    """'desktop' | 'mission-control' | 'app-expose' | None (unknown: leave native)."""
    up = overview_up()
    if up is None:
        return None
    if not up:
        return "desktop"
    if not HAVE_SPACE_NAMES or SL is None:
        return None
    spaces = SL.SLSCopySpaces(CID, CURRENT_OS_SPACES_MASK)
    if not spaces:
        return None
    names = set()
    for i in range(CF.CFArrayGetCount(spaces)):
        sid = cf_to_i64(CF.CFArrayGetValueAtIndex(spaces, i))
        name = SL.SLSSpaceCopyName(CID, sid) if sid else None
        if name:
            names.add(cf_to_str(name))
            CF.CFRelease(name)
    CF.CFRelease(spaces)
    mc, expose = "mission-control" in names, "show-front" in names
    if mc == expose:
        return None
    return "mission-control" if mc else "app-expose"


_natural = (True, -60.0)


def natural_scrolling():
    global _natural
    now = time.monotonic()
    if now - _natural[1] < 10.0:
        return _natural[0]
    val = CF.CFPreferencesCopyAppValue(K_SWIPE_SCROLL_DIR, kCFPreferencesAnyApplication)
    on = True
    if val:
        on = (cf_to_i64(val) != 0) if CF.CFGetTypeID(val) == CF_NUMBER_TYPE_ID else (val != CF_BOOL_FALSE)
        CF.CFRelease(val)
    _natural = (on, now)
    return on


# --- forged events ---------------------------------------------------------------------
def post_dock_swipe(phase, right, progress):
    ev = CG.CGEventCreate(None)
    if not ev:
        return False
    sign = 1.0 if right else -1.0
    CG.CGEventSetIntegerValueField(ev, F_CGS_EVENT_TYPE, CGS_EVENT_DOCK_CONTROL)
    CG.CGEventSetIntegerValueField(ev, F_GESTURE_HID_TYPE, HID_DOCK_SWIPE)
    CG.CGEventSetIntegerValueField(ev, F_GESTURE_PHASE, phase)
    CG.CGEventSetIntegerValueField(ev, F_SWIPE_MOTION, MOTION_HORIZONTAL)
    CG.CGEventSetDoubleValueField(ev, F_SWIPE_PROGRESS, sign * progress)
    CG.CGEventSetDoubleValueField(ev, F_SWIPE_VEL_X, sign * INSTANT_VELOCITY)
    CG.CGEventSetDoubleValueField(ev, F_SWIPE_VEL_Y, sign * INSTANT_VELOCITY)
    CG.CGEventPost(kCGSessionEventTap, ev)
    CF.CFRelease(ev)
    return True


def fixed1616(v):
    f = max(-(2 ** 31), min(2 ** 31 - 1, int(v * 65536.0)))
    if f == 0 and v != 0:
        return 1 if v > 0 else -1
    return f


def iohid_payload(ev):
    """IOHIDSystemQueueElementHeader (28 B) + IOHIDFluidTouchGestureData (40 B)
    [+ IOHIDVelocityEventData (28 B)], little-endian, mirroring the event's gesture fields."""
    phase = CG.CGEventGetIntegerValueField(ev, F_GESTURE_PHASE)
    motion = CG.CGEventGetIntegerValueField(ev, F_SWIPE_MOTION)
    progress = CG.CGEventGetDoubleValueField(ev, F_SWIPE_PROGRESS)
    vel_x = CG.CGEventGetDoubleValueField(ev, F_SWIPE_VEL_X)
    vel_y = CG.CGEventGetDoubleValueField(ev, F_SWIPE_VEL_Y)
    with_vel = vel_x != 0 or vel_y != 0 or phase == PHASE_ENDED
    ts = CG.CGEventGetTimestamp(ev) or LIBSYS.mach_absolute_time()
    p = struct.pack("<QQIII", ts, 0, 0, 0, 2 if with_vel else 1)
    p += struct.pack("<IIIB3xiiiIHHi", 40, IOHID_TYPE_FLUID_TOUCH_GESTURE, (phase & 0xFF) << 24, 0, 0, 0, 0, 0,
                     motion, IOHID_FLAVOR_DOCK_PRIMARY, fixed1616(progress))
    if with_vel:
        p += struct.pack("<IIIB3xiii", 28, IOHID_TYPE_VELOCITY, 0, 1, fixed1616(vel_x), fixed1616(vel_y), 0)
    return p


def with_iohid_payload(ev):
    data = CG.CGEventCreateData(None, ev)
    if not data:
        return None
    blob = C.string_at(CF.CFDataGetBytePtr(data), CF.CFDataGetLength(data))
    CF.CFRelease(data)
    if blob[:4] != b"\x00\x00\x00\x02":
        return None  # serialization format changed; do not post garbage
    payload = iohid_payload(ev)
    blob += struct.pack(">HH", len(payload), FIELD_IOHID_PAYLOAD) + payload
    new_data = CF.CFDataCreate(None, blob, len(blob))
    if not new_data:
        return None
    out = CG.CGEventCreateFromData(None, new_data)
    CF.CFRelease(new_data)
    return out


def make_mc_event(phase, direction, progress, velocity=0.0):
    ev = CG.CGEventCreate(None)
    if not ev:
        return None
    sign = float(direction)
    CG.CGEventSetIntegerValueField(ev, F_CGS_EVENT_TYPE, CGS_EVENT_DOCK_CONTROL)
    CG.CGEventSetIntegerValueField(ev, F_GESTURE_HID_TYPE, HID_DOCK_SWIPE)
    CG.CGEventSetIntegerValueField(ev, F_GESTURE_PHASE, phase)
    CG.CGEventSetIntegerValueField(ev, F_SWIPE_MOTION, MOTION_VERTICAL)
    CG.CGEventSetDoubleValueField(ev, F_SWIPE_PROGRESS, sign * progress)
    if velocity:
        CG.CGEventSetDoubleValueField(ev, F_SWIPE_VEL_X, sign * velocity)
    out = with_iohid_payload(ev)
    CF.CFRelease(ev)
    if out:
        CG.CGEventSetIntegerValueField(out, kCGEventSourceUserData, SYNTHETIC_MARKER)
    return out


def post_mc_transition(proxy, direction):
    events = [make_mc_event(PHASE_BEGAN, direction, MC_EPSILON),
              make_mc_event(PHASE_CHANGED, direction, 1.0),
              make_mc_event(PHASE_ENDED, direction, 1.0, MC_VELOCITY)]
    if not all(events):
        for ev in events:
            if ev:
                CF.CFRelease(ev)
        return False
    for ev in events:
        if proxy:
            CG.CGEventTapPostEvent(proxy, ev)
        else:
            CG.CGEventPost(kCGSessionEventTap, ev)
        CF.CFRelease(ev)
    return True


_last_switch_done = 0.0


def switch(right):
    """One Space in the given direction. False at the edge (nothing posted)."""
    global _last_switch_done
    info = space_info()
    target = None
    if info:
        display_id, reported, count = info
        cur = current_index(display_id, reported)
        target = cur + 1 if right else cur - 1
        if target < 0 or target >= count:
            return False
    gap = MIN_SWITCH_GAP - (time.monotonic() - _last_switch_done)
    if gap > 0:
        time.sleep(gap)
    # Began(~0) -> Changed(1.0) -> Ended(1.0). The full-progress Changed is what makes
    # WindowServer build the destination surfaces before commit (0/84 wedged landings vs
    # 43/97 for zero-travel recipes, measured with a screencapture oracle).
    for phase, progress in ((PHASE_BEGAN, EPSILON_PROGRESS), (PHASE_CHANGED, 1.0), (PHASE_ENDED, 1.0)):
        if not post_dock_swipe(phase, right, progress):
            return False
    _last_switch_done = time.monotonic()
    if info:
        _predicted[info[0]] = (target, _last_switch_done)
    return True


def mc_toggle(source):
    state = overview_state()
    direction = MC_OWNED_DIRECTION.get(state)
    if direction is None:
        return False
    did = post_mc_transition(None, direction)
    log("%s mission control %s (%s)%s" % (source, "up" if direction > 0 else "down", state, "" if did else " FAILED"))
    return did


# --- gesture state machine -------------------------------------------------------------
class G:
    last_switch = 0.0
    swipe_tracking = swipe_fired = False
    swipe_since = 0.0
    mc_pending = []
    mc_pending_since = 0.0
    mc_origin: str | None = None
    mc_natural = True
    mc_claimed = False
    mc_claimed_since = 0.0
    mc_key_claimed = False


_NOT_VERTICAL = object()


def is_synthetic(event):
    return (CG.CGEventGetIntegerValueField(event, kCGEventSourceUnixProcessID) != 0
            or CG.CGEventGetIntegerValueField(event, kCGEventSourceUserData) == SYNTHETIC_MARKER)


def mc_hold(event):
    copy = CG.CGEventCreateCopy(event)
    if copy:
        if not G.mc_pending:
            G.mc_pending_since = time.monotonic()
        G.mc_pending.append(copy)
    return bool(copy)


def mc_drop_pending():
    for ev in G.mc_pending:
        CF.CFRelease(ev)
    del G.mc_pending[:]
    G.mc_origin = None


def mc_replay_pending(proxy):
    for ev in G.mc_pending:
        if proxy:
            CG.CGEventTapPostEvent(proxy, ev)
        else:
            CG.CGEventPost(kCGSessionEventTap, ev)
    mc_drop_pending()


def gestures_stale():
    now = time.monotonic()
    if G.mc_pending and now - G.mc_pending_since > MC_GESTURE_TIMEOUT:
        log("vertical gesture never resolved (%d held); replaying" % len(G.mc_pending))
        mc_replay_pending(None)
    if G.mc_claimed and now - G.mc_claimed_since > MC_GESTURE_TIMEOUT:
        G.mc_claimed = False
    if G.swipe_tracking and now - G.swipe_since > MC_GESTURE_TIMEOUT:
        G.swipe_tracking = G.swipe_fired = False


def mc_direction(sign):
    if sign == 0:
        return None
    up = sign < 0 if G.mc_natural else sign > 0
    d = 1 if up else -1
    return d if MC_OWNED_DIRECTION.get(G.mc_origin) == d else None


def mc_resolve(proxy, event, sign, claim):
    origin = G.mc_origin
    d = mc_direction(sign)
    if d and post_mc_transition(proxy, d):
        mc_drop_pending()
        G.mc_claimed, G.mc_claimed_since = claim, time.monotonic()
        log("swipe %s (%s)" % ("up" if d > 0 else "down", origin))
        return None
    mc_replay_pending(proxy)
    return event


def on_vertical(proxy, etype, event):
    gestures_stale()
    if G.mc_claimed:
        if etype == CGS_EVENT_DOCK_CONTROL and CG.CGEventGetIntegerValueField(event, F_GESTURE_PHASE) in (PHASE_ENDED, PHASE_CANCELLED):
            G.mc_claimed = False
        return None
    if G.mc_pending and etype == CGS_EVENT_GESTURE:
        if CG.CGEventGetIntegerValueField(event, F_GESTURE_HID_TYPE) not in (HID_DOCK_SWIPE, 0):
            mc_replay_pending(proxy)
            return event
        if mc_hold(event):
            return None
        mc_replay_pending(proxy)
        return event
    if (etype != CGS_EVENT_DOCK_CONTROL
            or CG.CGEventGetIntegerValueField(event, F_GESTURE_HID_TYPE) != HID_DOCK_SWIPE
            or CG.CGEventGetIntegerValueField(event, F_SWIPE_MOTION) != MOTION_VERTICAL):
        return _NOT_VERTICAL
    phase = CG.CGEventGetIntegerValueField(event, F_GESTURE_PHASE)
    if phase == PHASE_BEGAN:
        mc_replay_pending(proxy)
        state = overview_state()
        if state is None or not mc_hold(event):
            return event
        G.mc_origin, G.mc_natural = state, natural_scrolling()
        return None
    if not G.mc_pending:
        return event
    progress = CG.CGEventGetDoubleValueField(event, F_SWIPE_PROGRESS)
    if phase == PHASE_CHANGED and abs(progress) >= SWIPE_DIRECTION_THRESHOLD:
        return mc_resolve(proxy, event, progress, claim=True)
    if phase == PHASE_ENDED:
        sign = progress
        if abs(progress) < SWIPE_DIRECTION_THRESHOLD:
            sign = CG.CGEventGetDoubleValueField(event, F_SWIPE_VEL_X) or CG.CGEventGetDoubleValueField(event, F_SWIPE_VEL_Y)
        return mc_resolve(proxy, event, sign, claim=False)
    if phase == PHASE_CANCELLED:
        mc_replay_pending(proxy)
        return event
    if mc_hold(event):
        return None
    mc_replay_pending(proxy)
    return event


def on_swipe(event):
    if CG.CGEventGetIntegerValueField(event, F_GESTURE_HID_TYPE) != HID_DOCK_SWIPE:
        return event
    if CG.CGEventGetIntegerValueField(event, F_SWIPE_MOTION) != MOTION_HORIZONTAL:
        return event
    phase = CG.CGEventGetIntegerValueField(event, F_GESTURE_PHASE)
    if phase == PHASE_BEGAN:
        if overview_up():  # inside an overview the swipe drives its own carousel
            G.swipe_tracking = G.swipe_fired = False
            return event
        G.swipe_tracking, G.swipe_fired, G.swipe_since = True, False, time.monotonic()
        return None
    if phase == PHASE_CHANGED:
        if not G.swipe_tracking:
            return event
        if not G.swipe_fired:
            progress = CG.CGEventGetDoubleValueField(event, F_SWIPE_PROGRESS)
            if abs(progress) >= SWIPE_DIRECTION_THRESHOLD:
                G.swipe_fired = True
                did = switch(progress > 0)
                log("swipe %s%s" % ("right" if progress > 0 else "left", "" if did else " (edge, ignored)"))
        return None
    if phase == PHASE_ENDED:
        if not G.swipe_tracking:
            return event
        if not G.swipe_fired:
            vel = CG.CGEventGetDoubleValueField(event, F_SWIPE_VEL_X)
            if vel != 0.0:
                did = switch(vel > 0)
                log("swipe %s on release%s" % ("right" if vel > 0 else "left", "" if did else " (edge, ignored)"))
        G.swipe_tracking = G.swipe_fired = False
        return None
    if phase == PHASE_CANCELLED:
        was = G.swipe_tracking
        G.swipe_tracking = G.swipe_fired = False
        return None if was else event
    return None if G.swipe_tracking else event


def on_event(proxy, etype, event, refcon):
    if etype in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
        mc_replay_pending(proxy)
        G.mc_claimed = False
        daemon.reenable_tap()
        return event
    if etype in (CGS_EVENT_DOCK_CONTROL, CGS_EVENT_GESTURE):
        if is_synthetic(event):
            return event
        if CFG["trackpad_vertical"]:
            result = on_vertical(proxy, etype, event)
            if result is not _NOT_VERTICAL:
                return result
        if not CFG["trackpad_horizontal"]:
            return event
        if etype == CGS_EVENT_DOCK_CONTROL:
            return on_swipe(event)
        if G.swipe_tracking and CG.CGEventGetIntegerValueField(event, F_GESTURE_HID_TYPE) in (HID_DOCK_SWIPE, 0):
            return None
        return event
    if not CFG["keyboard"]:
        return event
    if etype == kCGEventKeyUp:
        if G.mc_key_claimed and CG.CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) == KEY_MISSION_CONTROL:
            G.mc_key_claimed = False
            return None
        return event
    if etype != kCGEventKeyDown:
        return event
    key = CG.CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
    flags = CG.CGEventGetFlags(event) & MODIFIER_MASK
    repeat = CG.CGEventGetIntegerValueField(event, kCGKeyboardEventAutorepeat)
    if SHORTCUTS.is_mc(key, flags) or SHORTCUTS.is_expose(key, flags):
        if repeat:
            return None
        state = overview_state()
        is_expose = SHORTCUTS.is_expose(key, flags) and not SHORTCUTS.is_mc(key, flags)
        if state == "desktop" and not is_expose:
            direction = 1
        elif state == "mission-control":
            direction = -1
        elif state == "app-expose" and key == KEY_MISSION_CONTROL:
            direction = 1
        else:
            return event  # App Expose from desktop etc.: native
        did = post_mc_transition(None, direction)
        G.mc_key_claimed = did and key == KEY_MISSION_CONTROL
        log("key mission control %s (%s)%s" % ("up" if direction > 0 else "down", state, "" if did else " FAILED"))
        return None if did else event
    left, right = SHORTCUTS.is_left(key, flags), SHORTCUTS.is_right(key, flags)
    if not (left or right):
        return event
    if overview_up():
        return event
    now = time.monotonic()
    if repeat and now - G.last_switch < CFG["key_repeat_interval_ms"] / 1000.0:
        return None
    G.last_switch = now
    did = switch(right)
    log("key %s%s" % ("right" if right else "left", "" if did else " (edge, ignored)"))
    return None if did else event


def main():
    if "--check-config" in sys.argv:
        print("config OK:", CFG_PATH)
        return
    mask = (1 << kCGEventKeyDown) | (1 << kCGEventKeyUp) | (1 << CGS_EVENT_GESTURE) | (1 << CGS_EVENT_DOCK_CONTROL)

    def tick():
        gestures_stale()
        SHORTCUTS.refresh()

    info = space_info()
    where = "display %s, space %d of %d" % (info[0][:8], info[1] + 1, info[2]) if info else "no CGS space info; edge clamping off"
    daemon.run("spaceswitch", mask, on_event, on_tick=tick,
               ready_msg="spaceswitch ready: keyboard=%s swipe=%s/%s (%s; %s)" % (
                   CFG["keyboard"], CFG["trackpad_horizontal"], CFG["trackpad_vertical"], where, CFG_PATH))


if __name__ == "__main__":
    main()
