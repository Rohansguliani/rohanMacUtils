#!/usr/bin/python3
"""logimouse: make a Logitech mouse's extra controls do something useful on macOS.

- Gesture / thumb button (HID++ feature 0x1B04 "Reprogrammable Controls v4") is *diverted*:
  the mouse stops swallowing it and reports presses to us instead. We map a press to a
  keystroke (default: Mission Control key).
- Thumb wheel (horizontal scroll) can be mapped to keystrokes too (default: Ctrl+Left/Right,
  i.e. previous / next Space).
- Optional: flip the main wheel direction for the *mouse only* (trackpad keeps the system
  "natural" setting).

No dependency on any other mactools package. Everything is expressed as keystrokes, so it
works with stock macOS; if `spaceswitch` is installed those keystrokes are instant.

Which mice: any Logitech HID++ 2.0 device (MX Master 1/2S/3/3S/4, MX Anywhere 2/2S/3/3S,
Lift, M720, ...) over Bluetooth, Bolt, Unifying or USB. The divertable buttons are
discovered from the mouse's own control table — nothing is hardcoded per model.

Permissions: Accessibility (event tap) + Input Monitoring (IOHID open), both granted to
/usr/bin/python3 in System Settings > Privacy & Security.
"""

import ctypes as C
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "util"))
import config  # noqa: E402
import daemon  # noqa: E402
from macbridge import (CF, CG, FLAG_CTRL, cf_dict, cf_number, cf_to_i64, cfstr, kCFNumberSInt32Type,  # noqa: E402
                       kCFRunLoopCommonModes, kCGEventScrollWheel, kCGEventSourceUnixProcessID,
                       kCGEventSourceUserData, kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput,
                       kCGScrollEventUnitLine, kCGScrollWheelEventDeltaAxis1, kCGScrollWheelEventDeltaAxis2,
                       kCGScrollWheelEventIsContinuous, kCGSessionEventTap, log, post_key, vp)

DEFAULTS = {
    "gesture_button": "missioncontrol",   # hotkey string, or "" to leave the button alone
    "thumb_wheel_left": "ctrl+left",      # "" disables; mapped per detent, debounced
    "thumb_wheel_right": "ctrl+right",
    "thumb_wheel_cooldown_ms": 120,
    "invert_main_wheel": False,           # mouse only; trackpad unaffected
    "main_wheel_lines_per_detent": 1,     # macOS default is 1; 3 feels like Windows
    "debug": False,
}
CFG, CFG_PATH = config.load("logimouse", DEFAULTS)
GESTURE_KEY = config.parse_hotkey(CFG["gesture_button"]) if CFG["gesture_button"] else None
WHEEL_L = config.parse_hotkey(CFG["thumb_wheel_left"]) if CFG["thumb_wheel_left"] else None
WHEEL_R = config.parse_hotkey(CFG["thumb_wheel_right"]) if CFG["thumb_wheel_right"] else None
DEBUG = bool(CFG["debug"]) or bool(os.environ.get("HIDPP_DEBUG"))
SYNTHETIC_MARKER = 0x4C4F4749  # 'LOGI' in source user data: events we posted

# --- IOKit HID -----------------------------------------------------------------
IOK = C.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
REPORT_CB = C.CFUNCTYPE(None, vp, C.c_int, vp, C.c_int, C.c_uint32, C.POINTER(C.c_uint8), C.c_long)
DEVICE_CB = C.CFUNCTYPE(None, vp, C.c_int, vp, vp)
for name, res, args in (
    ("IOHIDManagerCreate", vp, [vp, C.c_uint32]),
    ("IOHIDManagerSetDeviceMatching", None, [vp, vp]),
    ("IOHIDManagerOpen", C.c_int, [vp, C.c_uint32]),
    ("IOHIDManagerCopyDevices", vp, [vp]),
    ("IOHIDManagerRegisterDeviceMatchingCallback", None, [vp, DEVICE_CB, vp]),
    ("IOHIDManagerRegisterDeviceRemovalCallback", None, [vp, DEVICE_CB, vp]),
    ("IOHIDManagerScheduleWithRunLoop", None, [vp, vp, vp]),
    ("IOHIDDeviceOpen", C.c_int, [vp, C.c_uint32]),
    ("IOHIDDeviceClose", C.c_int, [vp, C.c_uint32]),
    ("IOHIDDeviceGetProperty", vp, [vp, vp]),
    ("IOHIDDeviceSetReport", C.c_int, [vp, C.c_int, C.c_long, C.POINTER(C.c_uint8), C.c_long]),
    ("IOHIDDeviceRegisterInputReportCallback", None, [vp, C.POINTER(C.c_uint8), C.c_long, REPORT_CB, vp]),
    ("IOHIDCheckAccess", C.c_int, [C.c_int]),
):
    fn = getattr(IOK, name)
    fn.restype, fn.argtypes = res, args
kIOHIDReportTypeOutput = 1
K_VENDOR_ID = cfstr("VendorID")
K_PRIMARY_USAGE_PAGE = cfstr("PrimaryUsagePage")
K_DEVICE_USAGE_PAIRS = cfstr("DeviceUsagePairs")
K_DEVICE_USAGE_PAGE = cfstr("DeviceUsagePage")
K_PRODUCT = cfstr("Product")

# --- HID++ 2.0 -----------------------------------------------------------------
# Spec: Logitech "HID++ 2.0 protocol" + feature 0x1B04 "Reprogrammable Controls v4".
LOGITECH_VID = 0x046D
HIDPP_SHORT_REPORT, HIDPP_LONG_REPORT = 0x10, 0x11
HIDPP_SWID = 0x0D                         # our software id; replies carry it back
FEAT_IROOT = 0x0000                       # getFeature(featureId) -> index
FEAT_REPROG_V4 = 0x1B04
# 0x1B04 functions
FN_GET_COUNT, FN_GET_CID_INFO, FN_GET_CID_REPORTING, FN_SET_CID_REPORTING = 0, 1, 2, 3
# getCidInfo flags byte (byte 6 of the reply): bit5 = divertable, bit6 = persist, bit7 = virtual
CID_FLAG_DIVERTABLE = 0x20
# setCidReporting flags: bit0 = divert, bit1 = dvalid (divert field valid)
DIVERT_ON = 0x03
# Logitech vendor usage pages: 0xFF43 = BLE / modern HID++ 2.0, 0xFF00 = Unifying / Bolt / USB
VENDOR_USAGE_PAGES = (0xFF43, 0xFF00)
# HID++ device index: 0xFF = direct (BLE/USB) or the receiver itself; 1..6 = receiver slots
CANDIDATE_DEVIDS = (0xFF, 1, 2, 3, 4, 5, 6)
MAX_UNANSWERED = 3                        # ticks (~15 s) before giving up on a mute handle
REASSERT_S = 60.0                         # re-divert cadence; a no-op if already diverted


class State:
    """Reconciler state. The mouse forgets diverts on every power cycle / re-pair and macOS
    hands out a new IOHIDDevice each time, so we never react to events — we converge."""
    mgr = None
    dev = None
    buf = None
    dev_idx = None        # responding HID++ device index
    feat_idx = None       # 0x1B04's index on this device
    cid_count = None      # from getCount
    cids_probed = 0       # how many getCidInfo replies we have
    divertable = []       # [(cid, tid)] discovered from the control table
    diverted_at = None
    down = False
    unanswered = 0
    last_log = None


S = State()
_refs = []
_q = queue.Queue(maxsize=16)   # IOHIDDeviceSetReport blocks ~250 ms on an idle BLE mouse: never on the main thread
_last_wheel = 0.0


def log_once(msg):
    if msg != S.last_log:
        S.last_log = msg
        log("hidpp: " + msg)


def writer():
    while True:
        dev, msg = _q.get()
        out = (C.c_uint8 * 20)(*msg)
        t0 = time.monotonic()
        r = IOK.IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, HIDPP_LONG_REPORT, out, 20)
        if r != 0 or DEBUG:
            log("hidpp: tx %s -> %#x in %.0f ms" % (msg[1:7].hex(" "), r & 0xFFFFFFFF, (time.monotonic() - t0) * 1e3))
        CF.CFRelease(dev)


def send(dev_idx, feat_idx, fn, params=b""):
    if not S.dev:
        return
    msg = bytes([HIDPP_LONG_REPORT, dev_idx, feat_idx, (fn << 4) | HIDPP_SWID]) + params
    msg += bytes(20 - len(msg))
    try:
        _q.put_nowait((CF.CFRetain(S.dev), msg))
    except queue.Full:
        log_once("write queue full; mouse not accepting reports")


def fire(hotkey, what):
    if hotkey is None:
        return
    keycode, flags = hotkey
    post_key(keycode, flags)
    log("%s -> key %d flags %#x" % (what, keycode, flags))


def on_report(ctx, res, sender, rtype, rid, report, length):
    if rid not in (HIDPP_SHORT_REPORT, HIDPP_LONG_REPORT) or length < 6 or S.dev is None:
        return
    b = bytes(report[:length])
    dev_idx = b[1]
    if dev_idx not in CANDIDATE_DEVIDS:
        return
    if DEBUG:
        log("hidpp: rx %s" % b[:12].hex(" "))
    # --- IRoot.getFeature(0x1B04) reply: byte 4 = feature index (0 = unsupported)
    if b[2] == FEAT_IROOT and b[3] == HIDPP_SWID:
        if b[4] == 0:
            if dev_idx == 0xFF:
                log_once("device lacks feature 0x1B04; no divertable buttons")
        elif S.feat_idx is None:
            S.dev_idx, S.feat_idx, S.unanswered = dev_idx, b[4], 0
            reconcile()
        return
    if S.feat_idx is None or dev_idx != S.dev_idx or b[2] != S.feat_idx:
        return
    fn = b[3] >> 4
    if (b[3] & 0x0F) == HIDPP_SWID:  # one of our requests being answered
        if fn == FN_GET_COUNT:
            S.cid_count = b[4]
            S.cids_probed = 0
            S.divertable = []
            for i in range(S.cid_count):
                send(S.dev_idx, S.feat_idx, FN_GET_CID_INFO, bytes([i]))
        elif fn == FN_GET_CID_INFO:
            cid, tid, flags = (b[4] << 8) | b[5], (b[6] << 8) | b[7], b[8]
            S.cids_probed += 1
            if flags & CID_FLAG_DIVERTABLE and cid not in (c for c, _ in S.divertable):
                S.divertable.append((cid, tid))
            if S.cid_count is not None and S.cids_probed >= S.cid_count:
                log("control table: %d controls, divertable: %s" % (S.cid_count, ["%04x" % c for c, _ in S.divertable]))
                reconcile()
        elif fn == FN_SET_CID_REPORTING:
            log_once("diverted %s" % ["%04x" % c for c, _ in S.divertable])
        return
    if b[3] == 0x00:  # divertedButtonsEvent: up to 4 pressed CIDs (big-endian), 0 = none
        n = min(4, (len(b) - 4) // 2)
        pressed = {(b[4 + 2 * i] << 8) | b[5 + 2 * i] for i in range(n)} - {0}
        down = bool(pressed)
        if down and not S.down:
            fire(GESTURE_KEY, "gesture button %s" % ["%04x" % c for c in sorted(pressed)])
        S.down = down


def has_vendor_page(dev):
    pup = IOK.IOHIDDeviceGetProperty(dev, K_PRIMARY_USAGE_PAGE)
    if pup and cf_to_i64(pup) in VENDOR_USAGE_PAGES:
        return True
    pairs = IOK.IOHIDDeviceGetProperty(dev, K_DEVICE_USAGE_PAIRS)
    if pairs:
        for i in range(CF.CFArrayGetCount(pairs)):
            p = CF.CFArrayGetValueAtIndex(pairs, i)
            if cf_to_i64(CF.CFDictionaryGetValue(p, K_DEVICE_USAGE_PAGE)) in VENDOR_USAGE_PAGES:
                return True
    return False


def present_devices():
    s = IOK.IOHIDManagerCopyDevices(S.mgr) if S.mgr else None
    if not s:
        return []
    n = CF.CFSetGetCount(s)
    arr = (vp * max(n, 1))()
    if n:
        CF.CFSetGetValues(s, arr)
    devs = [arr[i] for i in range(n) if has_vendor_page(arr[i])]
    CF.CFRelease(s)

    def primary_is_vendor(d):
        p = IOK.IOHIDDeviceGetProperty(d, K_PRIMARY_USAGE_PAGE)
        return 0 if (p and cf_to_i64(p) in VENDOR_USAGE_PAGES) else 1
    devs.sort(key=primary_is_vendor)
    return devs


def drop():
    if S.dev:
        IOK.IOHIDDeviceClose(S.dev, 0)
        CF.CFRelease(S.dev)
    S.dev = S.buf = S.dev_idx = S.feat_idx = S.cid_count = S.diverted_at = None
    S.divertable, S.cids_probed, S.down, S.unanswered = [], 0, False, 0


def reconcile(*_):
    present = present_devices()
    if S.dev and not any(CF.CFEqual(S.dev, d) for d in present):
        log_once("mouse detached")
        drop()
    if not S.dev:
        if not present:
            return
        dev = present[0]
        r = IOK.IOHIDDeviceOpen(dev, 0)
        if r != 0:
            err = r & 0xFFFFFFFF
            if err == 0xE00002E2:  # kIOReturnNotPermitted: TCC. Stale attribution? (see util/daemon.py)
                daemon.respawn_if_fresh_process_is_allowed("listen")
                log_once("IOHIDDeviceOpen denied: grant Input Monitoring to /usr/bin/python3 in System Settings")
            else:
                log_once("IOHIDDeviceOpen failed %#x; retrying" % err)
            return
        S.dev = CF.CFRetain(dev)
        S.buf = (C.c_uint8 * 64)()
        IOK.IOHIDDeviceRegisterInputReportCallback(S.dev, S.buf, 64, _refs[0], None)
        log_once("mouse attached")
    if S.feat_idx is None:
        if S.unanswered >= MAX_UNANSWERED:
            log_once("no HID++ answer on this handle; dropping it")
            drop()
            return
        S.unanswered += 1
        pup = IOK.IOHIDDeviceGetProperty(S.dev, K_PRIMARY_USAGE_PAGE)
        dev_ids = CANDIDATE_DEVIDS if (pup and cf_to_i64(pup) in VENDOR_USAGE_PAGES) else (0xFF,)
        for d in dev_ids:
            send(d, FEAT_IROOT, 0, bytes([FEAT_REPROG_V4 >> 8, FEAT_REPROG_V4 & 0xFF]))
        return
    if S.cid_count is None:
        send(S.dev_idx, S.feat_idx, FN_GET_COUNT)
        return
    if S.cids_probed < S.cid_count:
        return  # table still arriving
    if not S.divertable or GESTURE_KEY is None:
        return
    now = time.monotonic()
    if S.diverted_at is None or now - S.diverted_at >= REASSERT_S:
        S.diverted_at = now
        for cid, _ in S.divertable:
            send(S.dev_idx, S.feat_idx, FN_SET_CID_REPORTING, bytes([cid >> 8, cid & 0xFF, DIVERT_ON]))


def setup_hid():
    S.mgr = IOK.IOHIDManagerCreate(None, 0)
    n_vid = cf_number(LOGITECH_VID, kCFNumberSInt32Type)
    match = cf_dict([K_VENDOR_ID], [n_vid])
    IOK.IOHIDManagerSetDeviceMatching(S.mgr, match)
    CF.CFRelease(match)
    CF.CFRelease(n_vid)
    threading.Thread(target=writer, name="hidpp-writer", daemon=True).start()
    _refs.extend([REPORT_CB(on_report), DEVICE_CB(reconcile)])
    IOK.IOHIDManagerRegisterDeviceMatchingCallback(S.mgr, _refs[1], None)
    IOK.IOHIDManagerRegisterDeviceRemovalCallback(S.mgr, _refs[1], None)
    IOK.IOHIDManagerScheduleWithRunLoop(S.mgr, CF.CFRunLoopGetCurrent(), kCFRunLoopCommonModes)
    r = IOK.IOHIDManagerOpen(S.mgr, 0)
    if r != 0:
        log("hidpp: IOHIDManagerOpen failed %#x; gesture button disabled" % (r & 0xFFFFFFFF))
        S.mgr = None
        return
    reconcile()


# --- scroll wheel (event tap) --------------------------------------------------
def is_synthetic(ev):
    return (CG.CGEventGetIntegerValueField(ev, kCGEventSourceUnixProcessID) != 0
            or CG.CGEventGetIntegerValueField(ev, kCGEventSourceUserData) == SYNTHETIC_MARKER)


def on_event(proxy, etype, event, refcon):
    global _last_wheel
    if etype in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
        daemon.reenable_tap()
        return event
    if etype != kCGEventScrollWheel or is_synthetic(event):
        return event
    a2 = CG.CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis2)
    continuous = CG.CGEventGetIntegerValueField(event, kCGScrollWheelEventIsContinuous)
    if continuous:
        return event  # trackpad: never ours
    if a2 == 0:
        if not CFG["invert_main_wheel"] and CFG["main_wheel_lines_per_detent"] == 1:
            return event
        lines = CG.CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis1)
        if not lines:
            return event
        sign = -1 if CFG["invert_main_wheel"] else 1
        ev = CG.CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 1, sign * lines * int(CFG["main_wheel_lines_per_detent"]))
        if ev:
            CG.CGEventSetIntegerValueField(ev, kCGEventSourceUserData, SYNTHETIC_MARKER)
            CG.CGEventPost(kCGSessionEventTap, ev)
            CF.CFRelease(ev)
            return None
        return event
    # horizontal, non-continuous: thumb wheel
    if WHEEL_L is None and WHEEL_R is None:
        return event
    now = time.monotonic()
    if now - _last_wheel < CFG["thumb_wheel_cooldown_ms"] / 1000.0:
        return None
    _last_wheel = now
    fire(WHEEL_R if a2 > 0 else WHEEL_L, "thumb wheel %s" % ("right" if a2 > 0 else "left"))
    return None


def main():
    if "--check-config" in sys.argv:
        print("config OK:", CFG_PATH)
        return
    setup_hid()
    daemon.run("logimouse", 1 << kCGEventScrollWheel, on_event, on_tick=reconcile,
               ready_msg="logimouse ready: gesture=%s wheel=%s/%s invert=%s (%s)" % (
                   CFG["gesture_button"] or "off", CFG["thumb_wheel_left"] or "off",
                   CFG["thumb_wheel_right"] or "off", CFG["invert_main_wheel"], CFG_PATH))


if __name__ == "__main__":
    main()
