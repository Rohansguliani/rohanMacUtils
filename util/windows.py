"""windows: the current-Space window list, MRU order, live thumbnails, cross-app focus.

Shared by alttab (the switcher) and snap (the companion picker); neither depends on the other.

All of it is SkyLight SPI: SLSWindowQuery* lists the windows of the active Space in ~0.2 ms
with no Accessibility round-trips, CGSHWCaptureWindowList hands back a GPU IOSurface
thumbnail (~6 ms, no CPU copy), and _SLPSSetFrontProcessWithOptions plus a synthesized
key-window record raise ONE window of another app without dragging its siblings along.
These are the same calls AltTab and yabai ship against. Tested on macOS 26.x.
"""

from __future__ import annotations

import ctypes as C
import os
import threading
import time

from macbridge import (AX, AX_GET_WINDOW, CF, CG, CID, SL, SLS_NOTIFY_CB, CGPoint, CGRect, cf_to_str, cfstr, i32,
                       i64, kCFNumberSInt64Type, kCFTypeArrayCallBacks, u32, u64, vp)
from objc import SL_SET_FRONT, ProcessSerialNumber, cls, send

if SL is None or not CID:
    raise RuntimeError("SkyLight.framework unavailable: the window list and thumbnails need it")

OWN_PID = os.getpid()
mru_frozen = False  # a client sets this while cycling so the focus changes it causes don't reorder the MRU


# ---------------------------------------------------------------------------
# Fast App Metadata & Live GPU Thumbnail Cache (Leak-Free CGImageRef Pool)
# ---------------------------------------------------------------------------
_APP_META = {}  # pid -> (app_name: str, ns_image: vp, skip: bool)
_SKIP_BUNDLES = {
    "com.apple.dock",
    "com.apple.WindowManager",
    "com.apple.controlcenter",
    "com.apple.notificationcenterui",
    "com.apple.systemuiserver",
    "com.apple.loginwindow",
    "com.apple.Spotlight",
}


def app_meta(pid: int):
    cached = _APP_META.get(pid)
    if cached is not None:
        return cached
    app = send(cls("NSRunningApplication"), "runningApplicationWithProcessIdentifier:", vp, (i32,), pid)
    if not app:
        res = (f"pid:{pid}", None, True)
        _APP_META[pid] = res
        return res
    bid_ns = send(app, "bundleIdentifier")
    bid = cf_to_str(bid_ns) if bid_ns else ""
    if bid in _SKIP_BUNDLES:
        res = (bid, None, True)
        _APP_META[pid] = res
        return res
    policy = send(app, "activationPolicy", i64)
    if policy == 2:
        res = (bid, None, True)
        _APP_META[pid] = res
        return res
    name_ns = send(app, "localizedName")
    name = cf_to_str(name_ns) if name_ns else (bid or f"pid:{pid}")
    icon = send(app, "icon")
    if icon:
        send(icon, "retain")
    res = (name, icon, False)
    _APP_META[pid] = res
    return res


_THUMB_LOCK = threading.Lock()
_THUMB_CACHE = {}  # wid -> (cg_image_ptr: int, captured_monotonic: float)


def get_cached_thumb(wid: int):
    with _THUMB_LOCK:
        entry = _THUMB_CACHE.get(wid)
        return entry[0] if entry else None


def capture_window_cgimage(wid: int):
    """Hardware-accelerated IOSurface capture (~5.9 ms on Apple Silicon). Retains CGImageRef."""
    w = u32(wid)
    arr = SL.CGSHWCaptureWindowList(CID, C.byref(w), 1, 0)
    if not arr:
        return None
    img = None
    if CF.CFArrayGetCount(arr) > 0:
        raw = CF.CFArrayGetValueAtIndex(arr, 0)
        if raw and CG.CGImageGetWidth(raw) > 8:
            img = CF.CFRetain(raw)
    CF.CFRelease(arr)
    return img


def update_thumb(wid: int, max_age: float = 1.2) -> bool:
    """Capture a fresh thumbnail if missing or older than max_age. Returns True if updated."""
    now = time.monotonic()
    with _THUMB_LOCK:
        old = _THUMB_CACHE.get(wid)
        if old and (now - old[1]) < max_age:
            return False
    img = capture_window_cgimage(wid)
    if not img:
        return False
    with _THUMB_LOCK:
        prev = _THUMB_CACHE.get(wid)
        _THUMB_CACHE[wid] = (img, time.monotonic())
        if prev and prev[0]:
            CF.CFRelease(prev[0])
    return True


def prune_thumbs(live_wids: set[int]):
    with _THUMB_LOCK:
        dead = [w for w in _THUMB_CACHE if w not in live_wids]
        for w in dead:
            img, _ = _THUMB_CACHE.pop(w)
            if img:
                CF.CFRelease(img)


# ---------------------------------------------------------------------------
# Per-Desktop / Space Window Query (`CGSGetActiveSpace` only — ~0.18 ms!)
# ---------------------------------------------------------------------------
def snapshot_current_space_windows():
    """Return (windows, cur_sid) strictly for the CURRENT Desktop / Space."""
    cur_sid = SL.CGSGetActiveSpace(CID)
    if not cur_sid:
        return [], 0

    nr = CF.CFNumberCreate(None, kCFNumberSInt64Type, C.byref(i64(cur_sid)))
    sp_arr = CF.CFArrayCreate(None, (vp * 1)(nr), 1, C.addressof(kCFTypeArrayCallBacks))
    st, ct = u64(0), u64(0)
    wids_arr = SL.CGSCopyWindowsWithOptionsAndTags(CID, 0, sp_arr, 2, C.byref(st), C.byref(ct))
    CF.CFRelease(sp_arr)
    CF.CFRelease(nr)

    if not wids_arr:
        return [], cur_sid

    q = SL.SLSWindowQueryWindows(CID, wids_arr, 1)  # bit 0 = include window titles
    CF.CFRelease(wids_arr)
    if not q:
        return [], cur_sid

    it = SL.SLSWindowQueryResultCopyWindows(q)
    CF.CFRelease(q)
    if not it:
        return [], cur_sid

    seen = set()
    out = []
    while SL.SLSWindowIteratorAdvance(it):
        wid = SL.SLSWindowIteratorGetWindowID(it)
        if wid in seen:
            continue
        lvl = SL.SLSWindowIteratorGetLevel(it)
        pwid = SL.SLSWindowIteratorGetParentID(it)
        alpha = SL.SLSWindowIteratorGetAlpha(it)
        pid = SL.SLSWindowIteratorGetPID(it)
        title_ref = SL.SLSWindowIteratorCopyTitle(it)
        title = cf_to_str(title_ref, release=True)
        if lvl != 0 or pwid != 0 or alpha <= 0.01 or pid == OWN_PID:
            continue
        rect = CGRect()
        SL.SLSGetWindowBounds(CID, wid, C.byref(rect))
        if rect.size.w < 120 or rect.size.h < 90:
            continue
        app_name, icon, skip = app_meta(pid)
        if skip:
            continue
        if not title and app_name in ("Google Chrome", "Safari", "Finder"):
            continue
        seen.add(wid)
        out.append({
            "wid": wid,
            "pid": pid,
            "app": app_name,
            "title": title or app_name,
            "icon": icon,
        })

    CF.CFRelease(it)
    return out, cur_sid


# ---------------------------------------------------------------------------
# Strict Per-Space MRU Order Engine (Never drops or scrambles cycling order)
# ---------------------------------------------------------------------------
_MRU_LOCK = threading.Lock()
_MRU = []  # global recency rank of wids; filtering against current Space gives per-Space MRU!
_IGNORE_EXTERNAL_MRU_UNTIL = 0.0
_WATCHED_WIDS = set()


def bump_mru(wid: int, force: bool = False):
    if not wid:
        return
    with _MRU_LOCK:
        if not force and time.monotonic() < _IGNORE_EXTERNAL_MRU_UNTIL:
            return
        if _MRU and _MRU[0] == wid:
            return
        if wid in _MRU:
            _MRU.remove(wid)
        _MRU.insert(0, wid)


def frontmost_space_wid(windows):
    ws = send(cls("NSWorkspace"), "sharedWorkspace")
    front_app = send(ws, "frontmostApplication") if ws else None
    front_pid = send(front_app, "processIdentifier", i32) if front_app else 0
    space_wids = {w["wid"] for w in windows}
    if front_pid:
        app_el = AX.AXUIElementCreateApplication(front_pid)
        if app_el:
            AX.AXUIElementSetMessagingTimeout(app_el, 0.04)
            fw = vp()
            if AX.AXUIElementCopyAttributeValue(app_el, cfstr("AXFocusedWindow"), C.byref(fw)) == 0 and fw.value:
                w = u32(0)
                if AX_GET_WINDOW(fw.value, C.byref(w)) == 0 and w.value in space_wids:
                    CF.CFRelease(fw.value)
                    CF.CFRelease(app_el)
                    return w.value
                CF.CFRelease(fw.value)
            CF.CFRelease(app_el)
    return windows[0]["wid"] if windows else 0


def ordered_windows_for_session(app_only: bool = False):
    windows, _ = snapshot_current_space_windows()
    if not windows:
        return []

    live_map = {w["wid"]: w for w in windows}
    active_wid = frontmost_space_wid(windows)
    if active_wid:
        bump_mru(active_wid, force=True)

    with _MRU_LOCK:
        ordered = [live_map[wid] for wid in _MRU if wid in live_map]
        seen = {w["wid"] for w in ordered}
        for w in windows:
            if w["wid"] not in seen:
                _MRU.append(w["wid"])
                ordered.append(w)

    global _WATCHED_WIDS
    live_set = set(live_map.keys())
    if live_set and not live_set.issubset(_WATCHED_WIDS):
        _WATCHED_WIDS |= live_set
        arr = (u32 * len(_WATCHED_WIDS))(*_WATCHED_WIDS)
        SL.SLSRequestNotificationsForWindows(CID, arr, len(_WATCHED_WIDS))

    if app_only and ordered:
        target_pid = ordered[0]["pid"]
        ordered = [w for w in ordered if w["pid"] == target_pid]

    return ordered


# ---------------------------------------------------------------------------
# Instant Same-Space Window Focus (Zero Space Jump, Zero Clicks on UI)
# ---------------------------------------------------------------------------
_FOCUS_GEN = 0
_FOCUS_LOCK = threading.Lock()


def make_key_window_record(psn: ProcessSerialNumber, wid: int):
    buf = (C.c_uint8 * 0x100)()
    buf[0x04] = 0xF8
    buf[0x08] = 0x01  # kCGEventLeftMouseDown (down only, off-content point)
    buf[0x3A] = 0x10
    C.memmove(C.addressof(buf) + 0x3C, C.byref(u32(wid)), 4)
    pt = CGPoint(300_000.0, 300_000.0)
    C.memmove(C.addressof(buf) + 0x20, C.byref(pt), C.sizeof(CGPoint))
    SL.SLPSPostEventRecordTo(C.byref(psn), buf)


def ax_raise_or_close(pid: int, target_wid: int, action: str = "AXRaise"):
    app = AX.AXUIElementCreateApplication(pid)
    if not app:
        return
    AX.AXUIElementSetMessagingTimeout(app, 0.25)
    for attr in ("AXWindows", "AXFocusedWindow", "AXMainWindow"):
        val = vp()
        if AX.AXUIElementCopyAttributeValue(app, cfstr(attr), C.byref(val)) != 0 or not val.value:
            continue
        elems = [CF.CFArrayGetValueAtIndex(val.value, i) for i in range(CF.CFArrayGetCount(val.value))] if attr == "AXWindows" else [val.value]
        matched = None
        for el in elems:
            w = u32(0)
            if AX_GET_WINDOW(el, C.byref(w)) == 0 and w.value == target_wid:
                matched = el
                break
        if matched:
            if action == "AXClose":
                close_btn = vp()
                if AX.AXUIElementCopyAttributeValue(matched, cfstr("AXCloseButton"), C.byref(close_btn)) == 0 and close_btn.value:
                    AX.AXUIElementPerformAction(close_btn.value, cfstr("AXPress"))
                    CF.CFRelease(close_btn.value)
            else:
                AX.AXUIElementPerformAction(matched, cfstr(action))
            CF.CFRelease(val.value)
            break
        CF.CFRelease(val.value)
    CF.CFRelease(app)


def focus_window_async(win):
    global _FOCUS_GEN, _IGNORE_EXTERNAL_MRU_UNTIL
    wid = win["wid"]
    pid = win["pid"]

    # Synchronously advance MRU before returning so rapid Opt+Tab ping-ponging is 100% immune to IPC delay
    bump_mru(wid, force=True)
    with _MRU_LOCK:
        _IGNORE_EXTERNAL_MRU_UNTIL = time.monotonic() + 0.30

    with _FOCUS_LOCK:
        _FOCUS_GEN += 1
        my_gen = _FOCUS_GEN

    def worker():
        with _FOCUS_LOCK:
            if my_gen != _FOCUS_GEN:
                return
        wcid = i32(0)
        psn = ProcessSerialNumber()
        if SL.SLSGetWindowOwner(CID, wid, C.byref(wcid)) == 0 and SL.SLSGetConnectionPSN(wcid.value, C.byref(psn)) == 0:
            SL_SET_FRONT(C.byref(psn), wid, 0x200)
            make_key_window_record(psn, wid)
        ax_raise_or_close(pid, wid, "AXRaise")
        # Refresh thumbnail of the newly focused window once it settles
        time.sleep(0.08)
        update_thumb(wid, max_age=0.0)

    threading.Thread(target=worker, daemon=True).start()



# ---------------------------------------------------------------------------
# Background thumbnail refresh + focus tracking
# ---------------------------------------------------------------------------
def warm_thumbnails_async(wins, on_changed=None, max_age=0.9):
    """Refresh thumbnails older than `max_age` on a background thread. `on_changed()` is
    called from that thread if any changed, so the owner can repaint on its main loop."""
    def bg():
        changed = False
        for w in wins:
            if update_thumb(w["wid"], max_age=max_age):
                changed = True
        if changed and on_changed:
            on_changed()

    threading.Thread(target=bg, daemon=True).start()


def recent_mru(n=24):
    with _MRU_LOCK:
        return list(_MRU[:n])


def _on_sls_notify(event_type, data, data_len, ctx, cid):
    if mru_frozen:
        return
    if event_type == 808 and data and data_len >= 4:
        wid = C.cast(data, C.POINTER(u32))[0]
        if wid and wid in _WATCHED_WIDS:
            bump_mru(wid, force=False)


_SLS_CB_REF = SLS_NOTIFY_CB(_on_sls_notify)


def start_focus_tracking():
    """Keep the MRU order current from WindowServer focus notifications (event 808) for the
    windows ordered_windows_for_session() has registered."""
    SL.SLSRegisterConnectionNotifyProc(CID, _SLS_CB_REF, 808, None)
