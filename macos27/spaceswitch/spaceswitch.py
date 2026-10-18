#!/usr/bin/python3
"""spaceswitch (macOS 27+): Space switching and Mission Control with no animation.

How: the Dock listens for private "DockSwipe" gesture events. On macOS 26 the Dock decoded
the CGEvent fields directly, so we could post a forged gesture that claimed the finger had
already travelled the full distance at terminal velocity and the Dock committed instantly.
On macOS 27 the Dock hands the event to WindowManager.SystemGestureManager, which reads the
IOHIDEvent attached to the CGEvent (field 4205) instead: a FluidTouchGesture record plus a
VendorDefined child (usage page 0xFF00, usage 0x0070) carrying a Gestures.framework stream.

That stream is undocumented, so we do not synthesise it. We record it: the first real swipe
of each kind after start gives us the Began / Changed / Ended payloads verbatim (one per
motion, cached per Dock pid in ~/.cache/mactools). To switch instantly we replay those three
payloads back to back with only the numbers patched in place (progress, velocity, timestamp,
same byte lengths), so whatever framing the Dock expects is preserved. The first swipe of
each kind after a Dock restart is therefore native/animated; everything after is instant.

Permissions: Accessibility (granted to /usr/bin/python3).
"""

from __future__ import annotations

import ctypes as C
import json
import os
import plistlib
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "util"))
import config  # noqa: E402
import daemon  # noqa: E402
from macbridge import (CF, CG, CS, LIBSYS, SL, CID, FLAG_CTRL, MODIFIER_MASK, cf_to_i64, cf_to_str, cfstr,  # noqa: E402
                       kCFPreferencesAnyApplication, kCGEventKeyDown, kCGEventKeyUp, kCGEventSourceUnixProcessID,
                       kCGEventSourceUserData, kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput,
                       kCGKeyboardEventAutorepeat, kCGKeyboardEventKeycode, kCGSessionEventTap,
                       kCGWindowListOptionOnScreenOnly, K_WIN_LAYER, K_WIN_OWNER, log, vp)

IOK = C.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
CG.CGEventCopyIOHIDEvent.restype = vp
CG.CGEventCopyIOHIDEvent.argtypes = [vp]
IOK.IOHIDEventCreateData.restype = vp
IOK.IOHIDEventCreateData.argtypes = [vp, vp]

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

# --- private CGEvent fields (WindowServer/Dock ABI; reverse-engineered, macOS 12-27) ----
F_CGS_EVENT_TYPE, F_GESTURE_HID_TYPE, F_SWIPE_MOTION = 55, 110, 123
F_SWIPE_PROGRESS, F_SWIPE_VEL_X, F_SWIPE_VEL_Y, F_GESTURE_PHASE = 124, 129, 130, 132
CGS_EVENT_GESTURE, CGS_EVENT_DOCK_CONTROL = 29, 30
HID_DOCK_SWIPE = 23
MOTION_HORIZONTAL, MOTION_VERTICAL = 1, 2
PHASE_BEGAN, PHASE_CHANGED, PHASE_ENDED, PHASE_CANCELLED = 1, 2, 4, 8
FIELD_IOHID_PAYLOAD = 4205
# IOHIDEvent serialisation (IOHIDSystemQueueElement + flat IOHIDEventData records)
QUEUE_HDR = struct.Struct("<QQIII")          # timeStamp, senderID, options, attributeLength, eventCount
EV_HDR = struct.Struct("<IIIB3x")            # size, type, options, depth
IOHID_TYPE_VENDOR, IOHID_TYPE_VELOCITY, IOHID_TYPE_FLUID_TOUCH_GESTURE = 1, 9, 23
VENDOR_USAGE_PAGE, VENDOR_USAGE = 0xFF00, 0x0070
# Gestures.framework stream: IOHIDEvent field keys as zig-zag varints, tag 2 = little-endian double
GF_PROGRESS, GF_VEL_X, GF_VEL_Y = 0x170002, 0x090000, 0x090001
DOCK_OVERVIEW_LAYER = 18             # Dock owns a layer-18 window for the life of any overview
CURRENT_OS_SPACES_MASK = (1 << 0) | (1 << 3)
SYNTHETIC_MARKER = 0x53505357        # 'SPSW' in source user data: events we posted
KEY_LEFT, KEY_RIGHT, KEY_UP, KEY_DOWN, KEY_MISSION_CONTROL = 123, 124, 126, 125, 160

# --- tuning (all measured on macOS 26; see docstrings) -----------------------------------
INSTANT_VELOCITY = 2000.0            # anything well above the Dock's flick threshold
EPSILON_PROGRESS = 1e-4              # Began progress; FLT_TRUE_MIN flushes to zero on Apple Silicon
SHORT_TRAVEL = 0.1                   # Changed/Ended progress of a forged switch (see switch())
SWIPE_DIRECTION_THRESHOLD = 0.05     # |progress| before a real swipe's direction is trusted
MIN_SWITCH_GAP = 0.08                # s between two forged switches (WindowServer re-layering)
MC_EPSILON = 1.0 / 65536.0           # smallest signed 16.16 step
MC_VELOCITY = 9999.0                 # removes Mission Control's final zoom frames on dismissal
MC_GESTURE_TIMEOUT = 0.5             # s before an unresolved held gesture is replayed
PREDICTION_TTL = 0.5                 # s to trust our own "we just moved to index N" over CGS
MC_OWNED_DIRECTION: dict[str | None, int] = {"desktop": 1, "mission-control": -1, "app-expose": 1}
CACHE_PATH = os.path.expanduser("~/.cache/mactools/spaceswitch27.json")

K_DISPLAY_ID, K_SPACES, K_CURRENT, K_ID64 = cfstr("Display Identifier"), cfstr("Spaces"), cfstr("Current Space"), cfstr("id64")
K_SWIPE_SCROLL_DIR = cfstr("com.apple.swipescrolldirection")
CF_NUMBER_TYPE_ID = CF.CFNumberGetTypeID()
CF_BOOL_FALSE = vp.in_dll(CF, "kCFBooleanFalse").value
HAVE_SPACE_NAMES = SL is not None and all(getattr(SL, n, None) for n in ("SLSCopySpaces", "SLSSpaceCopyName"))


def trace(msg):
    if TRACE:
        log(msg)


# --- recorded gesture payloads ----------------------------------------------------------
def zleb128(n: int) -> bytes:
    n = (n << 1) ^ (n >> 63)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


DOUBLE_KEYS = {k: zleb128(k) + b"\x02" for k in (GF_PROGRESS, GF_VEL_X, GF_VEL_Y)}


def fixed1616(v):
    f = max(-(2 ** 31), min(2 ** 31 - 1, int(v * 65536.0)))
    if f == 0 and v != 0:
        return 1 if v > 0 else -1
    return f


def iter_records(blob):
    """(offset, size, type, depth) for each IOHIDEventData record in a serialised IOHIDEvent."""
    if len(blob) < QUEUE_HDR.size:
        return
    _, _, _, attr_len, count = QUEUE_HDR.unpack_from(blob, 0)
    pos = QUEUE_HDR.size + attr_len
    for _ in range(count):
        if pos + EV_HDR.size > len(blob):
            return
        size, typ, _, depth = EV_HDR.unpack_from(blob, pos)
        if size < EV_HDR.size or pos + size > len(blob):
            return
        yield pos, size, typ, depth
        pos += size


def describe_payload(blob):
    parts = []
    for pos, size, typ, depth in iter_records(blob):
        if typ == IOHID_TYPE_FLUID_TOUCH_GESTURE and size >= 40:
            opts, motion, flavor, prog = struct.unpack_from("<I", blob, pos + 8)[0], *struct.unpack_from("<HHi", blob, pos + 32)
            parts.append("fluid(opts=%#x motion=%d flavor=%d progress=%.3f)" % (opts, motion, flavor, prog / 65536.0))
        elif typ == IOHID_TYPE_VELOCITY and size >= 28:
            vx, vy = struct.unpack_from("<ii", blob, pos + 16)
            parts.append("velocity(%.1f, %.1f)" % (vx / 65536.0, vy / 65536.0))
        elif typ == IOHID_TYPE_VENDOR and size >= 28:
            page, usage, _, length = struct.unpack_from("<HHII", blob, pos + 16)
            parts.append("vendor(%#x/%#x %dB)" % (page, usage, length))
        else:
            parts.append("type%d(%dB d%d)" % (typ, size, depth))
    return " ".join(parts) or "unparsed"


def patch_payload(tmpl, progress, vx, vy):
    """Copy of a recorded payload with progress / velocity / timestamp rewritten in place."""
    b = bytearray(tmpl)
    struct.pack_into("<Q", b, 0, LIBSYS.mach_absolute_time())
    for pos, size, typ, _ in iter_records(b):
        if typ == IOHID_TYPE_FLUID_TOUCH_GESTURE and size >= 40:
            struct.pack_into("<i", b, pos + 36, fixed1616(progress))
        elif typ == IOHID_TYPE_VELOCITY and size >= 28:
            struct.pack_into("<ii", b, pos + 16, fixed1616(vx), fixed1616(vy))
        elif typ == IOHID_TYPE_VENDOR and size >= 28:
            page, usage, _, length = struct.unpack_from("<HHII", b, pos + 16)
            if page != VENDOR_USAGE_PAGE or usage != VENDOR_USAGE:
                continue
            start, end = pos + 28, min(pos + 28 + length, len(b))
            seg = bytes(b[start:end])
            for key, val in ((GF_PROGRESS, progress), (GF_VEL_X, vx), (GF_VEL_Y, vy)):
                pat = DOUBLE_KEYS[key]
                i = seg.find(pat)
                while i != -1 and start + i + len(pat) + 8 <= end:
                    struct.pack_into("<d", b, start + i + len(pat), float(val))
                    i = seg.find(pat, i + 1)
    return bytes(b)


class Recordings:
    """Real Began / Changed / Ended payloads per motion, from the first complete swipe seen.

    The payloads are plain trackpad IOHID DockSwipe records (sender = the trackpad's registry
    id, nothing Dock-specific), so they stay valid across Dock restarts and are cached on disk.
    """
    PHASES = (PHASE_BEGAN, PHASE_CHANGED, PHASE_ENDED)

    def __init__(self):
        self.payloads: dict[tuple[int, int], bytes] = {}
        self.complete: set[int] = set()
        self.load()

    def load(self):
        if not os.path.exists(CACHE_PATH):
            return
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            for key, raw_hex in d["payloads"].items():
                motion, phase = (int(x) for x in key.split(":"))
                self.payloads[(motion, phase)] = bytes.fromhex(raw_hex)
            self.complete = {m for m in (MOTION_HORIZONTAL, MOTION_VERTICAL) if self.has(m)}
            log("gesture cache loaded: %s" % ", ".join(self.name(m) for m in sorted(self.complete)))
        except (OSError, ValueError, KeyError, TypeError) as e:
            log(f"gesture cache unreadable ({e}); re-recording")

    def save(self):
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            with open(CACHE_PATH, "w", encoding="utf-8") as fh:
                json.dump({"payloads": {"%d:%d" % k: v.hex() for k, v in self.payloads.items()}}, fh)
        except OSError as e:
            log(f"gesture cache not saved: {e}")

    @staticmethod
    def name(motion):
        return "horizontal" if motion == MOTION_HORIZONTAL else "vertical"

    def has(self, motion):
        return all((motion, p) in self.payloads for p in self.PHASES)

    def record(self, event, motion, phase):
        """Began restarts the recording; Changed keeps the latest frame; Ended completes it."""
        if motion in self.complete or phase not in self.PHASES:
            return
        if phase == PHASE_CHANGED and (motion, PHASE_BEGAN) not in self.payloads:
            return
        if phase == PHASE_ENDED and not all((motion, p) in self.payloads for p in (PHASE_BEGAN, PHASE_CHANGED)):
            return
        hid = CG.CGEventCopyIOHIDEvent(event)
        if not hid:
            return
        data = IOK.IOHIDEventCreateData(None, hid)
        CF.CFRelease(hid)
        if not data:
            return
        blob = C.string_at(CF.CFDataGetBytePtr(data), CF.CFDataGetLength(data))
        CF.CFRelease(data)
        if phase == PHASE_BEGAN:
            self.payloads.pop((motion, PHASE_CHANGED), None)
            self.payloads.pop((motion, PHASE_ENDED), None)
        self.payloads[(motion, phase)] = blob
        if phase != PHASE_CHANGED:
            log("recorded %s phase %d: %d B %s" % (self.name(motion), phase, len(blob), describe_payload(blob)))
            log("payload hex: " + blob.hex())
        if self.has(motion):
            self.complete.add(motion)
            log("%s gesture recorded; switches are instant from now on" % self.name(motion))
            self.save()

    def get(self, motion, phase):
        return self.payloads.get((motion, phase)) if motion in self.complete else None


RECORDINGS = Recordings()


def make_event(motion, phase, progress, vx, vy):
    payload = RECORDINGS.get(motion, phase)
    if payload is None:
        return None
    ev = CG.CGEventCreate(None)
    if not ev:
        return None
    CG.CGEventSetIntegerValueField(ev, F_CGS_EVENT_TYPE, CGS_EVENT_DOCK_CONTROL)
    CG.CGEventSetIntegerValueField(ev, F_GESTURE_HID_TYPE, HID_DOCK_SWIPE)
    CG.CGEventSetIntegerValueField(ev, F_GESTURE_PHASE, phase)
    CG.CGEventSetIntegerValueField(ev, F_SWIPE_MOTION, motion)
    CG.CGEventSetDoubleValueField(ev, F_SWIPE_PROGRESS, progress)
    CG.CGEventSetDoubleValueField(ev, F_SWIPE_VEL_X, vx)
    CG.CGEventSetDoubleValueField(ev, F_SWIPE_VEL_Y, vy)
    data = CG.CGEventCreateData(None, ev)
    CF.CFRelease(ev)
    if not data:
        return None
    blob = C.string_at(CF.CFDataGetBytePtr(data), CF.CFDataGetLength(data))
    CF.CFRelease(data)
    if blob[:4] != b"\x00\x00\x00\x02":
        return None  # serialization format changed; do not post garbage
    hid = patch_payload(payload, progress, vx, vy)
    blob += struct.pack(">HH", len(hid), FIELD_IOHID_PAYLOAD) + hid
    new_data = CF.CFDataCreate(None, blob, len(blob))
    if not new_data:
        return None
    out = CG.CGEventCreateFromData(None, new_data)
    CF.CFRelease(new_data)
    if out:
        CG.CGEventSetIntegerValueField(out, kCGEventSourceUserData, SYNTHETIC_MARKER)
    return out


def post_sequence(proxy, motion, direction, steps):
    """Post (phase, progress, velocity) steps built from the recordings; False if any is missing."""
    sign = float(direction)
    events = [make_event(motion, phase, sign * progress, sign * velocity, sign * velocity)
              for phase, progress, velocity in steps]
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


# --- system shortcuts -------------------------------------------------------------------
class Shortcuts:
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


# --- Space geometry ---------------------------------------------------------------------
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


_predicted = {}


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
        return None if (mc or "show-desktop" in names) else "desktop"
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


# --- forged transitions ---------------------------------------------------------------
_last_switch_done = 0.0


def hid_progress(event):
    """Progress as carried in the event's own IOHID FluidTouchGesture record (what the macOS 27
    Dock reads), or None."""
    hid = CG.CGEventCopyIOHIDEvent(event)
    if not hid:
        return None
    data = IOK.IOHIDEventCreateData(None, hid)
    CF.CFRelease(hid)
    if not data:
        return None
    blob = C.string_at(CF.CFDataGetBytePtr(data), CF.CFDataGetLength(data))
    CF.CFRelease(data)
    for pos, size, typ, _ in iter_records(blob):
        if typ == IOHID_TYPE_FLUID_TOUCH_GESTURE and size >= 40:
            return struct.unpack_from("<i", blob, pos + 36)[0] / 65536.0
    return None


def switch(right):
    """One Space in the given direction. False at the edge (nothing posted), None if we have
    no recording yet for this Dock (caller lets the native switch happen).

    macOS 27 recipe (sstest2.py, 12/12 clean one-Space commits in 31-53 ms):
    Began(~0) -> Changed(0.1) -> Ended(0.1, terminal velocity), back to back. The short
    travel matters: a zero-travel flick (Began -> Ended) is a race and gives no-ops, bounces
    and double steps about a third of the time. Progress 1.0 anywhere makes the Dock commit
    on its own and the velocity then projects further Spaces, and velocities in the real
    events' range (~1-5) give the stock 500 ms animation. Positive progress moves to the
    previous Space (the opposite of the macOS 26 CGEvent field)."""
    global _last_switch_done
    if not RECORDINGS.has(MOTION_HORIZONTAL):
        return None
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
    if not post_sequence(None, MOTION_HORIZONTAL, -1 if right else 1,
                         [(PHASE_BEGAN, EPSILON_PROGRESS, 0.0), (PHASE_CHANGED, SHORT_TRAVEL, 0.0),
                          (PHASE_ENDED, SHORT_TRAVEL, INSTANT_VELOCITY)]):
        return False
    _last_switch_done = time.monotonic()
    if info:
        _predicted[info[0]] = (target, _last_switch_done)
    if TRACE:
        threading.Thread(target=_trace_landing, args=(info[1] if info else None, _last_switch_done), daemon=True).start()
    return True


def _trace_landing(start_index, t0):
    """debug: log how the CGS space index moves during the second after a forged switch."""
    trail, last = [], start_index
    while time.monotonic() - t0 < 1.2:
        info = space_info()
        cur = info[1] if info else None
        if cur != last:
            trail.append("%d@%dms" % (cur, (time.monotonic() - t0) * 1000))
            last = cur
        time.sleep(0.005)
    log("landing: from %s -> %s" % (start_index, " -> ".join(trail) or "no change"))


# No forged vertical recipe works on macOS 27 yet (sstest/mctest: 2- and 3-event sequences at
# every velocity are ignored by the Dock), so Mission Control stays native until one is found.
MC_SUPPORTED = False


def post_mc_transition(proxy, direction):
    if not MC_SUPPORTED:
        return False
    return post_sequence(proxy, MOTION_VERTICAL, direction,
                         [(PHASE_BEGAN, MC_EPSILON, 0.0), (PHASE_CHANGED, 1.0, 0.0), (PHASE_ENDED, 1.0, MC_VELOCITY)])


# --- gesture state machine --------------------------------------------------------------
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
    RECORDINGS.record(event, MOTION_VERTICAL, phase)
    if phase == PHASE_BEGAN:
        mc_replay_pending(proxy)
        if not RECORDINGS.has(MOTION_VERTICAL):
            return event  # still recording this kind of swipe: native
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
    RECORDINGS.record(event, MOTION_HORIZONTAL, phase)
    if phase == PHASE_BEGAN:
        if overview_up() or not RECORDINGS.has(MOTION_HORIZONTAL):
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
                # macOS 27: positive progress = previous Space; trust the IOHID value the Dock reads
                hp = hid_progress(event)
                right = (hp if hp is not None else progress) < 0
                did = switch(right)
                log("swipe %s%s" % ("right" if right else "left", "" if did else " (edge, ignored)"))
                trace("swipe cg_progress=%+.3f hid_progress=%s" % (progress, "%+.3f" % hp if hp is not None else None))
        return None
    if phase == PHASE_ENDED:
        if not G.swipe_tracking:
            return event
        if not G.swipe_fired:
            vel = CG.CGEventGetDoubleValueField(event, F_SWIPE_VEL_X)
            if vel != 0.0:
                did = switch(vel < 0)
                log("swipe %s on release%s" % ("right" if vel < 0 else "left", "" if did else " (edge, ignored)"))
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
        if CFG["trackpad_vertical"] and MC_SUPPORTED:
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
    if MC_SUPPORTED and (SHORTCUTS.is_mc(key, flags) or SHORTCUTS.is_expose(key, flags)):
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
            return event
        did = post_mc_transition(None, direction)
        if not did:
            return event  # nothing recorded yet for this Dock: native
        G.mc_key_claimed = key == KEY_MISSION_CONTROL
        log("key mission control %s (%s)" % ("up" if direction > 0 else "down", state))
        return None
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
    if did is None:
        return event  # nothing recorded yet for this Dock: native
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
    recorded = ", ".join(RECORDINGS.name(m) for m in sorted(RECORDINGS.complete)) or "none yet: first swipe of each kind is native"
    daemon.run("spaceswitch", mask, on_event, on_tick=tick,
               ready_msg="spaceswitch (macOS 27) ready: keyboard=%s swipe=%s/%s recorded=%s (%s; %s)" % (
                   CFG["keyboard"], CFG["trackpad_horizontal"], CFG["trackpad_vertical"], recorded, where, CFG_PATH))


if __name__ == "__main__":
    main()
