#!/usr/bin/python3
"""spaceswitch (macOS 27+): Space switching and Mission Control with zero animation.

How (macOS 27 architecture):
In macOS 27, Apple replaced Dock's legacy C DockSwipe decoder with
WindowManager.SystemGestureManager.handleEvent(CGEventRef), which extracts a VendorDefined
child IOHIDEvent (UsagePage = 0xFF00, Usage = 0x0070) containing a Gestures.BinaryEncoder
stream of [GestureNodeID : [OpaqueDataEvent]].

When any horizontal (3/4-finger Space switch) or vertical (4-finger Mission Control) trackpad
gesture begins, WindowServer attaches Dock's live GestureNodeID header in that VendorDefined
payload. We capture that live GestureNodeID header on frame 0 (and cache it per Dock PID across
restarts for keyboard/mouse shortcuts), then synthesize a 3-event Began(eps) -> Active(1.0) ->
Ended(1.0, terminal velocity) sequence encoded in Gestures.BinaryEncoder wire format wrapped in
IOHIDEventCreateVendorDefinedEvent(0xFF00, 0x0070) + field 4205.

Permissions: Accessibility (granted to /usr/bin/python3).
"""

from __future__ import annotations

import ctypes as C
import json
import os
import plistlib
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "util"))
import config  # noqa: E402
import daemon  # noqa: E402
from macbridge import (CF, CG, CS, LIBSYS, SL, CID, FLAG_CTRL, MODIFIER_MASK, cf_to_i64, cf_to_str, cfstr,  # noqa: E402
                       kCFPreferencesAnyApplication, kCGEventKeyDown, kCGEventKeyUp, kCGEventSourceUnixProcessID,
                       kCGEventSourceUserData, kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput,
                       kCGKeyboardEventAutorepeat, kCGKeyboardEventKeycode, kCGSessionEventTap,
                       kCGWindowListOptionOnScreenOnly, K_WIN_LAYER, K_WIN_OWNER, log, vp, u32, i32)

IOK = C.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
CG.CGEventCopyIOHIDEvent.restype = vp
CG.CGEventCopyIOHIDEvent.argtypes = [vp]
IOK.IOHIDEventCreateFluidTouchGestureEvent.restype = vp
IOK.IOHIDEventCreateFluidTouchGestureEvent.argtypes = [vp, C.c_uint64, u32, C.c_double, C.c_double, C.c_double, u32]
IOK.IOHIDEventCreateVendorDefinedEvent.restype = vp
IOK.IOHIDEventCreateVendorDefinedEvent.argtypes = [vp, C.c_uint64, u32, u32, u32, C.c_char_p, C.c_long, u32]
IOK.IOHIDEventAppendEvent.restype = None
IOK.IOHIDEventAppendEvent.argtypes = [vp, vp, u32]
IOK.IOHIDEventCreateData.restype = vp
IOK.IOHIDEventCreateData.argtypes = [vp, vp]
IOK.IOHIDEventGetChildren.restype = vp
IOK.IOHIDEventGetChildren.argtypes = [vp]
IOK.IOHIDEventGetIntegerValue.restype = C.c_int64
IOK.IOHIDEventGetIntegerValue.argtypes = [vp, u32]
IOK.IOHIDEventGetDataValue.restype = vp
IOK.IOHIDEventGetDataValue.argtypes = [vp, u32]

DEFAULTS = {
    "keyboard": True,                 # intercept the Spaces / Mission Control shortcuts
    "trackpad_horizontal": True,      # 3/4-finger left/right swipe
    "trackpad_vertical": True,        # 4-finger up/down swipe (Mission Control)
    "extra_switch_modifiers": [],     # e.g. ["cmd"]: ALSO treat Cmd+Left/Right as switch
    "key_repeat_interval_ms": 120,    # min gap between switches while a key is held
    "debug": False,
}
CFG, CFG_PATH = config.load("spaceswitch", DEFAULTS)
TRACE = bool(CFG["debug"])

# --- CGS & Gestures.BinaryEncoder constants (macOS 27) -----------------------------------
F_CGS_EVENT_TYPE, F_GESTURE_HID_TYPE, F_SWIPE_MOTION = 55, 110, 123
F_SWIPE_PROGRESS, F_SWIPE_VEL_X, F_SWIPE_VEL_Y, F_GESTURE_PHASE = 124, 129, 130, 132
CGS_EVENT_GESTURE, CGS_EVENT_DOCK_CONTROL = 29, 30
HID_DOCK_SWIPE = 23
MOTION_HORIZONTAL, MOTION_VERTICAL = 1, 2
PHASE_BEGAN, PHASE_CHANGED, PHASE_ENDED, PHASE_CANCELLED = 1, 2, 4, 8
GPHASE_BEGAN, GPHASE_ACTIVE, GPHASE_ENDED, GPHASE_FAILED = 0, 1, 2, 3
FIELD_IOHID_PAYLOAD = 4205
VENDOR_USAGE_PAGE, VENDOR_USAGE = 0xFF00, 0x0070
HID_F_VENDOR_USAGE_PAGE, HID_F_VENDOR_USAGE, HID_F_VENDOR_LEN, HID_F_VENDOR_DATA = 0x10000, 0x10001, 0x10003, 0x10004
GF_MOTION, GF_PROGRESS, GF_FLAVOR = 0x170001, 0x170002, 0x170005
GF_VEL_X, GF_VEL_Y = 0x090000, 0x090001
DOCK_OVERVIEW_LAYER = 18
CURRENT_OS_SPACES_MASK = (1 << 0) | (1 << 3)
SYNTHETIC_MARKER = 0x53505357        # 'SPSW'
KEY_LEFT, KEY_RIGHT, KEY_UP, KEY_DOWN, KEY_MISSION_CONTROL = 123, 124, 126, 125, 160

INSTANT_VELOCITY = 2000.0
EPSILON_PROGRESS = 1e-4
SWIPE_DIRECTION_THRESHOLD = 0.05
MIN_SWITCH_GAP = 0.08
MC_EPSILON = 1.0 / 65536.0
MC_VELOCITY = 9999.0
MC_GESTURE_TIMEOUT = 0.5
PREDICTION_TTL = 0.5
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


# --- Gestures.BinaryEncoder codec & Dock GestureNodeID template cache -------------------
def uleb128(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def zleb128(n: int) -> bytes:
    return uleb128((n << 1) ^ (n >> 63))


def read_uleb128(buf: bytes, pos: int) -> tuple[int, int] | None:
    val = shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
        if shift > 70:
            return None
    return None


def read_zleb128(buf: bytes, pos: int) -> tuple[int, int] | None:
    r = read_uleb128(buf, pos)
    if r is None:
        return None
    zz, pos = r
    return (zz >> 1) ^ -(zz & 1), pos


def encode_opaque_event(ev_id: int, gphase: int, ts: int,
                        fields: list[tuple[int, int, int | float]]) -> bytes:
    b = bytearray()
    b += zleb128(ev_id)
    b.append(gphase)
    b += zleb128(ts)
    b += uleb128(len(fields))
    for k, tag, val in fields:
        b += zleb128(k)
        b.append(tag)
        if tag == 0:
            b.append(1 if val else 0)
        elif tag == 1:
            b += zleb128(int(val))
        else:
            b += struct.pack("<d", float(val))
    return bytes(b)


def match_opaque_at(buf: bytes, pos: int) -> tuple[int, int, int, list[tuple[int, int, int | float]]] | None:
    r_id = read_zleb128(buf, pos)
    if r_id is None or not (0 <= r_id[0] <= 0xFFFFFFFF):
        return None
    ev_id, p = r_id
    if p >= len(buf) or buf[p] not in (0, 1, 2, 3):
        return None
    phase = buf[p]
    p += 1
    r_ts = read_zleb128(buf, p)
    if r_ts is None or r_ts[0] < 1_000_000:
        return None
    _, p = r_ts
    r_cnt = read_uleb128(buf, p)
    if r_cnt is None or not (1 <= r_cnt[0] <= 24):
        return None
    cnt, p = r_cnt
    fields: list[tuple[int, int, int | float]] = []
    for _ in range(cnt):
        rk = read_zleb128(buf, p)
        if rk is None or rk[1] >= len(buf):
            return None
        k, p = rk
        tag = buf[p]
        p += 1
        if tag == 0:
            if p >= len(buf) or buf[p] not in (0, 1):
                return None
            fields.append((k, 0, int(buf[p])))
            p += 1
        elif tag == 1:
            rv = read_zleb128(buf, p)
            if rv is None:
                return None
            fields.append((k, 1, rv[0]))
            p = rv[1]
        elif tag == 2:
            if p + 8 > len(buf):
                return None
            fields.append((k, 2, struct.unpack("<d", buf[p:p + 8])[0]))
            p += 8
        else:
            return None
    if not any(k == GF_MOTION for k, _, _ in fields):
        return None
    return p, ev_id, phase, fields


def rewrite_vendor_stream(buf: bytes, motion: int, gphase: int, ts: int,
                          progress: float, vx: float, vy: float) -> bytes | None:
    """Rewrite every OpaqueDataEvent across all registered GestureNodeIDs in a 0xFF00/0x0070 packet."""
    out = bytearray()
    pos = n = 0
    while pos < len(buf):
        m = match_opaque_at(buf, pos)
        if m is None:
            out.append(buf[pos])
            pos += 1
        else:
            end, ev_id, _, fields = m
            fmap = {k: (t, v) for k, t, v in fields}
            fmap[GF_MOTION] = (1, motion)
            fmap[GF_FLAVOR] = (1, 3)
            fmap[GF_PROGRESS] = (2, progress)
            fmap[GF_VEL_X] = (2, vx)
            fmap[GF_VEL_Y] = (2, vy)
            out += encode_opaque_event(ev_id, gphase, ts, [(k, t, v) for k, (t, v) in fmap.items()])
            pos = end
            n += 1
    return bytes(out) if n > 0 else None


def dock_pid() -> int:
    try:
        return int(subprocess.check_output(["pgrep", "-x", "Dock"], text=True).strip().splitlines()[0])
    except Exception:
        return 0


class TemplateCache:
    """Stores raw 0xFF00/0x0070 multi-node packets for horizontal (1) & vertical (2) per Dock PID."""
    def __init__(self):
        self.pid = dock_pid()
        self.by_motion: dict[int, bytes] = {}
        self.load()

    def load(self):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            if d.get("dock_pid") != self.pid:
                return
            for m_str, raw_hex in d.get("packets", {}).items():
                self.by_motion[int(m_str)] = bytes.fromhex(raw_hex)
        except Exception:
            pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            data = {
                "dock_pid": self.pid,
                "packets": {str(m): raw.hex() for m, raw in self.by_motion.items()},
            }
            with open(CACHE_PATH, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except Exception as e:
            trace(f"cache save error: {e}")

    def check_pid(self):
        cur = dock_pid()
        if cur and cur != self.pid:
            log(f"Dock restarted ({self.pid} -> {cur}); clearing gesture node cache")
            self.pid = cur
            self.by_motion.clear()

    def capture(self, event, motion: int) -> None:
        self.check_pid()
        hid = CG.CGEventCopyIOHIDEvent(event)
        if not hid:
            return
        try:
            children = IOK.IOHIDEventGetChildren(hid)
            if not children:
                return
            for i in range(CF.CFArrayGetCount(children)):
                c = CF.CFArrayGetValueAtIndex(children, i)
                if (IOK.IOHIDEventGetIntegerValue(c, HID_F_VENDOR_USAGE_PAGE) == VENDOR_USAGE_PAGE
                        and IOK.IOHIDEventGetIntegerValue(c, HID_F_VENDOR_USAGE) == VENDOR_USAGE):
                    n = IOK.IOHIDEventGetIntegerValue(c, HID_F_VENDOR_LEN)
                    ptr = IOK.IOHIDEventGetDataValue(c, HID_F_VENDOR_DATA)
                    if n <= 0 or not ptr:
                        continue
                    raw = C.string_at(ptr, n)
                    if rewrite_vendor_stream(raw, motion, GPHASE_BEGAN, 1_000_001, 0.0, 0.0, 0.0) is not None:
                        was_missing = motion not in self.by_motion
                        self.by_motion[motion] = raw
                        if was_missing:
                            log(f"learned macOS 27 GestureNodeID packet for motion={motion} ({n} B)")
                            self.save()
        finally:
            CF.CFRelease(hid)

    def get(self, motion: int) -> bytes | None:
        self.check_pid()
        return self.by_motion.get(motion)


TEMPLATES = TemplateCache()


def make_cgevent_27(motion: int, cgs_phase: int, gphase: int, progress: float, vel_x: float, vel_y: float):
    raw_tmpl = TEMPLATES.get(motion)
    if not raw_tmpl:
        return None
    ts = LIBSYS.mach_absolute_time()
    vendor_bytes = rewrite_vendor_stream(raw_tmpl, motion, gphase, ts, progress, vel_x, vel_y)
    if not vendor_bytes:
        return None

    parent = IOK.IOHIDEventCreateFluidTouchGestureEvent(
        None, ts, motion, 0.0, 0.0, progress, (cgs_phase & 0xFF) << 24)
    if not parent:
        return None
    child = IOK.IOHIDEventCreateVendorDefinedEvent(
        None, ts, VENDOR_USAGE_PAGE, VENDOR_USAGE, 0, vendor_bytes, len(vendor_bytes), 0)
    if not child:
        CF.CFRelease(parent)
        return None
    IOK.IOHIDEventAppendEvent(parent, child, 0)
    hid_data = IOK.IOHIDEventCreateData(None, parent)
    CF.CFRelease(child)
    CF.CFRelease(parent)
    if not hid_data:
        return None
    hid_bytes = C.string_at(CF.CFDataGetBytePtr(hid_data), CF.CFDataGetLength(hid_data))
    CF.CFRelease(hid_data)

    ev = CG.CGEventCreate(None)
    if not ev:
        return None
    CG.CGEventSetIntegerValueField(ev, F_CGS_EVENT_TYPE, CGS_EVENT_DOCK_CONTROL)
    CG.CGEventSetIntegerValueField(ev, F_GESTURE_HID_TYPE, HID_DOCK_SWIPE)
    CG.CGEventSetIntegerValueField(ev, F_GESTURE_PHASE, cgs_phase)
    CG.CGEventSetIntegerValueField(ev, F_SWIPE_MOTION, motion)
    CG.CGEventSetDoubleValueField(ev, F_SWIPE_PROGRESS, progress)
    CG.CGEventSetDoubleValueField(ev, F_SWIPE_VEL_X, vel_x)
    CG.CGEventSetDoubleValueField(ev, F_SWIPE_VEL_Y, vel_y)

    data = CG.CGEventCreateData(None, ev)
    CF.CFRelease(ev)
    if not data:
        return None
    blob = C.string_at(CF.CFDataGetBytePtr(data), CF.CFDataGetLength(data))
    CF.CFRelease(data)
    blob += struct.pack(">HH", len(hid_bytes), FIELD_IOHID_PAYLOAD) + hid_bytes
    new_data = CF.CFDataCreate(None, blob, len(blob))
    if not new_data:
        return None
    out = CG.CGEventCreateFromData(None, new_data)
    CF.CFRelease(new_data)
    if out:
        CG.CGEventSetIntegerValueField(out, kCGEventSourceUserData, SYNTHETIC_MARKER)
    return out


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


# --- macOS 27 instant forged transitions ------------------------------------------------
def post_dock_swipe_27(cgs_phase, gphase, right, progress):
    sign = 1.0 if right else -1.0
    ev = make_cgevent_27(MOTION_HORIZONTAL, cgs_phase, gphase,
                         sign * progress, sign * INSTANT_VELOCITY, 0.0)
    if not ev:
        return False
    CG.CGEventPost(kCGSessionEventTap, ev)
    CF.CFRelease(ev)
    return True


def post_mc_transition_27(proxy, direction):
    sign = float(direction)
    events = [
        make_cgevent_27(MOTION_VERTICAL, PHASE_BEGAN, GPHASE_BEGAN, sign * MC_EPSILON, 0.0, 0.0),
        make_cgevent_27(MOTION_VERTICAL, PHASE_CHANGED, GPHASE_ACTIVE, sign * 1.0, 0.0, 0.0),
        make_cgevent_27(MOTION_VERTICAL, PHASE_ENDED, GPHASE_ENDED, sign * 1.0, sign * MC_VELOCITY, sign * MC_VELOCITY),
    ]
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
    global _last_switch_done
    if TEMPLATES.get(MOTION_HORIZONTAL) is None:
        return None  # no template yet for current Dock session; let native shortcut pass through
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
    for cgs_p, gp, prog in (
        (PHASE_BEGAN, GPHASE_BEGAN, EPSILON_PROGRESS),
        (PHASE_CHANGED, GPHASE_ACTIVE, 1.0),
        (PHASE_ENDED, GPHASE_ENDED, 1.0),
    ):
        if not post_dock_swipe_27(cgs_p, gp, right, prog):
            return False
    _last_switch_done = time.monotonic()
    if info:
        _predicted[info[0]] = (target, _last_switch_done)
    return True


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
    if d and post_mc_transition_27(proxy, d):
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
    TEMPLATES.capture(event, MOTION_VERTICAL)
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
    TEMPLATES.capture(event, MOTION_HORIZONTAL)
    phase = CG.CGEventGetIntegerValueField(event, F_GESTURE_PHASE)
    if phase == PHASE_BEGAN:
        if overview_up() or TEMPLATES.get(MOTION_HORIZONTAL) is None:
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
            return event
        did = post_mc_transition_27(None, direction)
        if not did:
            return event  # no cached vertical node yet; pass shortcut through natively
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
        return event  # no cached horizontal node yet; pass shortcut through natively
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
        TEMPLATES.check_pid()

    info = space_info()
    where = "display %s, space %d of %d" % (info[0][:8], info[1] + 1, info[2]) if info else "no CGS space info; edge clamping off"
    daemon.run("spaceswitch", mask, on_event, on_tick=tick,
               ready_msg="spaceswitch (macOS 27) ready: keyboard=%s swipe=%s/%s (%s; %s)" % (
                   CFG["keyboard"], CFG["trackpad_horizontal"], CFG["trackpad_vertical"], where, CFG_PATH))


if __name__ == "__main__":
    main()
