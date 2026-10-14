#!/usr/bin/python3
"""snap: Rectangle-style window snapping, plus a few things Rectangle does not do.

  Opt+Left/Right/Up/Down   focused window to that half of the screen (visible frame)
  Opt+A                    fill the screen
  drag to a screen edge    translucent preview of the half; release to snap
  companion snap           after a snap, exactly one other window on the Space -> it takes the
                           other half; two or more -> a picker (thumbnail cards) in the empty half
  linked resize            drag the seam between two touching windows and the neighbour follows;
                           Opt+Shift+= / Opt+Shift+- grow or shrink the width by 5 % the same way

Every hotkey and each of the three mouse features can be turned off in
~/.config/mactools/snap.json.

Window moves use public Accessibility calls (AXPosition / AXSize). The window list and the
picker's thumbnails come from SkyLight SPI through util/windows.py. Tested on macOS 26.x.

Permissions for /usr/bin/python3: Accessibility (hotkeys, drag tracking, moving windows) and,
for the picker's thumbnails, Screen Recording.
"""

from __future__ import annotations

import ctypes as C
import os
import queue
import sys
import threading
import time
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "util"))
import config  # noqa: E402
import daemon  # noqa: E402
from cards import CardHUD, cgcolor  # noqa: E402
from macbridge import (AX, AX_GET_WINDOW, CF, CG, CID, SL, TIMER_CB, CGPoint, CGRect, CGSize, FLAG_SHIFT,  # noqa: E402
                       MODIFIER_MASK, cbool, cfstr, f64, i32, i64, kAXErrorInvalidUIElement, kAXValueTypeCGPoint,
                       kAXValueTypeCGSize, kCFNumberSInt64Type, kCFRunLoopCommonModes, kCFTypeArrayCallBacks,
                       kCGEventKeyDown, kCGEventLeftMouseDown, kCGEventLeftMouseDragged, kCGEventLeftMouseUp,
                       kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput, kCGKeyboardEventAutorepeat,
                       kCGKeyboardEventKeycode, log, u32, u64, vp)
from objc import cls, send  # noqa: E402
from windows import (focus_window_async, ordered_windows_for_session, snapshot_current_space_windows,  # noqa: E402
                     update_thumb, warm_thumbnails_async)

OWN_PID = os.getpid()

DEFAULTS = {
    "snap_left": "opt+left",           # any hotkey may be "" to disable it
    "snap_right": "opt+right",
    "snap_top": "opt+up",
    "snap_bottom": "opt+down",
    "snap_full": "opt+a",
    "width_grow": "opt+shift+=",
    "width_shrink": "opt+shift+-",
    "width_step_percent": 5,
    "drag_to_edge": True,
    "companion_snap": True,
    "linked_resize": True,
    "debug": False,
}
CFG, CFG_PATH = config.load("snap", DEFAULTS)
TRACE = bool(CFG["debug"])


def trace(msg):
    if TRACE:
        log(msg)


def _hk(name):
    return config.parse_hotkey(CFG[name]) if CFG[name] else None


HK_WIDTH = {k: d for k, d in ((_hk("width_grow"), +1), (_hk("width_shrink"), -1)) if k}
HK_SNAP = {k: s for k, s in ((_hk("snap_left"), "left"), (_hk("snap_right"), "right"), (_hk("snap_top"), "top"),
                             (_hk("snap_bottom"), "bottom"), (_hk("snap_full"), "full")) if k}
if not (HK_SNAP or HK_WIDTH or CFG["drag_to_edge"] or CFG["linked_resize"]):
    raise config.ConfigError(f"{CFG_PATH}: every hotkey and mouse feature is disabled; nothing to do")
# keys that act only while the companion picker is open
KEY_TAB, KEY_ESC, KEY_RETURN, KEY_SPACE, KEY_J, KEY_K = 48, 53, 36, 49, 38, 40
KEY_LEFT, KEY_RIGHT, KEY_DOWN, KEY_UP = 123, 124, 125, 126

_HUD: Any = None  # CardHUD for the companion picker, created in main()
_PICKER_DIRTY = False


def _mark_dirty():
    global _PICKER_DIRTY
    _PICKER_DIRTY = True
    CF.CFRunLoopWakeUp(CF.CFRunLoopGetMain())


# ---------------------------------------------------------------------------
# Window Snapping (Rectangle-style): Opt+Arrows = halves, drag-to-edge = halves,
# companion auto-snap / picker for the opposite half.
# ---------------------------------------------------------------------------
SNAP_HOTKEYS = HK_SNAP           # (keycode, flags) -> side, from config
WIDTH_HOTKEYS = HK_WIDTH         # (keycode, flags) -> +1 / -1, from config
WIDTH_STEP_FRAC = max(1, min(50, int(CFG["width_step_percent"]))) / 100.0  # of the screen's visible width per press (hold to repeat)
WIDTH_MIN_PX = 320.0
SNAP_OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
SNAP_EDGE_PX = 12.0  # cursor within this many px of a screen edge during a drag = snap zone
DRAG_MIN_TRAVEL = 6.0  # px of cursor travel before we even look at what is under it
SNAP_INSET = 0.0  # gap between snapped windows; 0 = flush like Rectangle default

_SNAP_Q = queue.Queue(maxsize=16)


def screen_frames():
    """[(frame_tl, visible_tl)] per NSScreen in top-left (CG) coordinates."""
    screens = send(cls("NSScreen"), "screens")
    n = send(screens, "count", i64)
    main_h = CG.CGDisplayBounds(0).size.h
    out = []
    for i in range(n):
        sc = send(screens, "objectAtIndex:", vp, (i64,), i)
        f = send(sc, "frame", CGRect)
        v = send(sc, "visibleFrame", CGRect)
        f_tl = CGRect(CGPoint(f.origin.x, main_h - f.origin.y - f.size.h), f.size)
        v_tl = CGRect(CGPoint(v.origin.x, main_h - v.origin.y - v.size.h), v.size)
        out.append((f_tl, v_tl))
    return out


def screen_for_point(x: float, y: float):
    screens = screen_frames()
    for f, v in screens:
        if f.origin.x <= x < f.origin.x + f.size.w and f.origin.y <= y < f.origin.y + f.size.h:
            return f, v
    return screens[0] if screens else (CGRect(CGPoint(0, 0), CGSize(1920, 1080)), CGRect(CGPoint(0, 0), CGSize(1920, 1080)))


def screen_for_rect(r: CGRect):
    cx = r.origin.x + r.size.w / 2.0
    cy = r.origin.y + r.size.h / 2.0
    return screen_for_point(cx, cy)


def half_rect(visible: CGRect, side: str) -> CGRect:
    x, y, w, h = visible.origin.x, visible.origin.y, visible.size.w, visible.size.h
    g = SNAP_INSET
    if side == "full":
        return CGRect(CGPoint(x + g, y + g), CGSize(w - g * 2, h - g * 2))
    if side == "left":
        return CGRect(CGPoint(x + g, y + g), CGSize(w / 2.0 - g * 1.5, h - g * 2))
    if side == "right":
        return CGRect(CGPoint(x + w / 2.0 + g / 2.0, y + g), CGSize(w / 2.0 - g * 1.5, h - g * 2))
    if side == "top":
        return CGRect(CGPoint(x + g, y + g), CGSize(w - g * 2, h / 2.0 - g * 1.5))
    return CGRect(CGPoint(x + g, y + h / 2.0 + g / 2.0), CGSize(w - g * 2, h / 2.0 - g * 1.5))


def window_bounds(wid: int) -> CGRect:
    r = CGRect()
    SL.SLSGetWindowBounds(CID, wid, C.byref(r))
    return r


_AX_WIN_CACHE = {}  # wid -> AXUIElementRef (retained)


def ax_window_for_wid(pid: int, wid: int):
    el = _AX_WIN_CACHE.get(wid)
    if el:
        return el
    app = AX.AXUIElementCreateApplication(pid)
    if not app:
        return None
    AX.AXUIElementSetMessagingTimeout(app, 0.25)
    val = vp()
    found = None
    if AX.AXUIElementCopyAttributeValue(app, cfstr("AXWindows"), C.byref(val)) == 0 and val.value:
        for i in range(CF.CFArrayGetCount(val.value)):
            e = CF.CFArrayGetValueAtIndex(val.value, i)
            w = u32(0)
            if AX_GET_WINDOW(e, C.byref(w)) == 0 and w.value == wid:
                found = CF.CFRetain(e)
                break
        CF.CFRelease(val.value)
    CF.CFRelease(app)
    if found:
        _AX_WIN_CACHE[wid] = found
    return found


def ax_set_frame(pid: int, wid: int, rect: CGRect) -> bool:
    """size -> position -> size: survives apps that clamp either order."""
    el = ax_window_for_wid(pid, wid)
    if not el:
        return False
    pt = CGPoint(rect.origin.x, rect.origin.y)
    sz = CGSize(rect.size.w, rect.size.h)
    v_pt = AX.AXValueCreate(kAXValueTypeCGPoint, C.byref(pt))
    v_sz = AX.AXValueCreate(kAXValueTypeCGSize, C.byref(sz))
    AX.AXUIElementSetMessagingTimeout(el, 0.5)
    r1 = AX.AXUIElementSetAttributeValue(el, cfstr("AXSize"), v_sz)
    r2 = AX.AXUIElementSetAttributeValue(el, cfstr("AXPosition"), v_pt)
    r3 = AX.AXUIElementSetAttributeValue(el, cfstr("AXSize"), v_sz)
    CF.CFRelease(v_pt)
    CF.CFRelease(v_sz)
    if kAXErrorInvalidUIElement in (r1, r2, r3):
        CF.CFRelease(el)
        _AX_WIN_CACHE.pop(wid, None)
        return False
    return r2 == 0


def ax_follow_edge(pid: int, wid: int, edge: str, start: CGRect, lead) -> bool:
    """Minimal-IPC follower update: ONE AX write per pass.

    A follower only ever changes along one axis. If its origin is fixed (lead moved its
    *left*/*top* edge away from us: we shrink/grow from our far side) a single AXSize
    suffices. If its origin moves (lead's *right*/*bottom* edge pushes our near side)
    we need position+size, but we send position first and size second so the window
    never transiently overlaps the lead. Short messaging timeout: a stalled app skips a
    frame rather than stalling the whole chain.
    """
    el = ax_window_for_wid(pid, wid)
    if not el:
        return False
    r = follower_frame(edge, start, lead)
    AX.AXUIElementSetMessagingTimeout(el, FOLLOW_AX_TIMEOUT_S)
    sz = CGSize(r.size.w, r.size.h)
    v_sz = AX.AXValueCreate(kAXValueTypeCGSize, C.byref(sz))
    if edge in ("left", "top"):
        rc = AX.AXUIElementSetAttributeValue(el, cfstr("AXSize"), v_sz)
        CF.CFRelease(v_sz)
    else:
        pt = CGPoint(r.origin.x, r.origin.y)
        v_pt = AX.AXValueCreate(kAXValueTypeCGPoint, C.byref(pt))
        rc = AX.AXUIElementSetAttributeValue(el, cfstr("AXPosition"), v_pt)
        rc2 = AX.AXUIElementSetAttributeValue(el, cfstr("AXSize"), v_sz)
        CF.CFRelease(v_pt)
        CF.CFRelease(v_sz)
        rc = rc or rc2
    if rc == kAXErrorInvalidUIElement:
        CF.CFRelease(el)
        _AX_WIN_CACHE.pop(wid, None)
        return False
    return rc == 0


def snap_worker():
    while True:
        job = _SNAP_Q.get()
        pid, wid, rect, raise_after = job[:4]
        linked = len(job) > 4 and job[4] == "linked"
        t0 = time.perf_counter()
        followers = {}
        if linked and CFG["linked_resize"]:
            with _FOLLOW_LOCK:
                start = _frame_tuple(window_bounds(wid))
                edges = _changed_edges(start, _frame_tuple(rect))
                followers = {e: r for e, r in linked_followers(wid, start).items() if e in edges}
        ok = ax_set_frame(pid, wid, rect)
        if not ok:
            # One retry with a fresh AX element (window may have been recreated)
            _AX_WIN_CACHE.pop(wid, None)
            ok = ax_set_frame(pid, wid, rect)
        if ok and followers:
            with _FOLLOW_LOCK:
                # Use the frame we just *requested*, not a read-back: SLSGetWindowBounds lags the
                # AX write by ~5 ms, and reading it here returned the OLD frame (followers then
                # 'moved' to where they already were). If the app clamped our request, the
                # read-back below catches it and we re-glue to the real frame.
                apply_followers(_frame_tuple(rect), followers, final=True)
                time.sleep(0.03)
                actual = _frame_tuple(window_bounds(wid))
                if any(abs(a - r) >= 1 for a, r in zip(actual, _frame_tuple(rect))):
                    apply_followers(actual, followers, final=True)
        n_linked = sum(len(v) for v in followers.values())
        log(f"snap wid={wid} -> ({rect.origin.x:.0f},{rect.origin.y:.0f},{rect.size.w:.0f},{rect.size.h:.0f}) {'ok' if ok else 'FAILED'}"
            f"{f' +{n_linked} linked' if n_linked else ''} in {(time.perf_counter() - t0) * 1e3:.1f} ms")
        if ok and raise_after:
            focus_window_async({"wid": wid, "pid": pid})
        if ok:
            time.sleep(0.05)
            update_thumb(wid, max_age=0.0)


def snap_window(pid: int, wid: int, side: str, visible: CGRect, raise_after: bool = False):
    try:
        _SNAP_Q.put_nowait((pid, wid, half_rect(visible, side), raise_after))
    except queue.Full:
        log("snap queue full; dropped")


def frontmost_window():
    """(pid, wid) of the focused window of the frontmost app, or None."""
    ws = send(cls("NSWorkspace"), "sharedWorkspace")
    app = send(ws, "frontmostApplication") if ws else None
    pid = send(app, "processIdentifier", i32) if app else 0
    if not pid or pid == OWN_PID:
        return None
    app_el = AX.AXUIElementCreateApplication(pid)
    if not app_el:
        return None
    AX.AXUIElementSetMessagingTimeout(app_el, 0.1)
    fw = vp()
    res = None
    if AX.AXUIElementCopyAttributeValue(app_el, cfstr("AXFocusedWindow"), C.byref(fw)) == 0 and fw.value:
        w = u32(0)
        if AX_GET_WINDOW(fw.value, C.byref(w)) == 0 and w.value:
            if w.value not in _AX_WIN_CACHE:
                _AX_WIN_CACHE[w.value] = CF.CFRetain(fw.value)
            res = (pid, w.value)
        CF.CFRelease(fw.value)
    CF.CFRelease(app_el)
    return res


def snap_hotkey(side: str) -> bool:
    fw = frontmost_window()
    if not fw:
        return False
    pid, wid = fw
    _, visible = screen_for_rect(window_bounds(wid))
    snap_window(pid, wid, side, visible)
    if side in SNAP_OPPOSITE:  # halves only: 'full' leaves no other half to fill
        companion_after_snap(wid, side, visible)
    return True


def width_hotkey(direction: int) -> bool:
    """Grow (+1) / shrink (-1) the focused window's width about its centre, clamped to the screen.

    If one side is already flush with a screen edge, growth goes the other way so a
    half-snapped window widens into the free space instead of trying to leave the screen.
    Any window sharing the edge(s) that move is pushed to keep the seam closed, exactly
    as a mouse seam-drag would.
    """
    fw = frontmost_window()
    if not fw:
        return False
    pid, wid = fw
    b = window_bounds(wid)
    _, vis = screen_for_rect(b)
    vx0, vx1 = vis.origin.x, vis.origin.x + vis.size.w
    step = vis.size.w * WIDTH_STEP_FRAC * direction
    new_w = max(WIDTH_MIN_PX, min(vis.size.w, b.size.w + step))
    delta = new_w - b.size.w
    if abs(delta) < 1:
        return True  # already at the clamp; swallow the key anyway
    x0, x1 = b.origin.x, b.origin.x + b.size.w
    flush_l, flush_r = x0 <= vx0 + 1, x1 >= vx1 - 1
    if flush_l and not flush_r:
        nx0 = x0  # anchored left, grow/shrink rightwards
    elif flush_r and not flush_l:
        nx0 = x1 - new_w  # anchored right
    else:
        nx0 = x0 - delta / 2.0  # centred
    nx0 = max(vx0, min(nx0, vx1 - new_w))
    target = (nx0, b.origin.y, new_w, b.size.h)
    try:
        # followers are resolved in the worker against *live* bounds, right before the write,
        # so a burst of presses (autorepeat) can't compute neighbours from stale geometry.
        _SNAP_Q.put_nowait((pid, wid, _rect(target), False, "linked"))
    except queue.Full:
        pass
    return True


# --- Drag-to-edge + linked resize ---------------------------------------------
LINK_GAP_PX = 10.0  # edges within this many px count as a shared boundary (hand-aligned windows are rarely exact)
LINK_MIN_OVERLAP = 40.0  # followers must share at least this much span along the edge
RESIZE_FOLLOW_MIN_DELTA = 1.0  # px change on the shared edge before followers are pushed
RESIZE_HANDLE_PX = 10.0  # macOS resize grab margin extends ~8 px outside the window frame
FOLLOW_FRAME_S = 1.0 / 60.0  # pace follower updates to display refresh: one layout per frame, no backlog
FOLLOW_AX_TIMEOUT_S = 0.25  # wait for the app (backpressure) rather than time out and pile up requests


class _Drag:
    active = False
    start = CGPoint(0, 0)
    cands = []  # [(wid, pid, start_frame)] front-to-back: every window whose padded rect holds the mouse-down
    wid = 0
    pid = 0
    start_frame = (0.0, 0.0, 0.0, 0.0)
    mode: str | None = None  # None (undecided) | "move" | "resize"
    zone: str | None = None
    visible: Any = None
    # resize-link state
    edges = ()  # which edges of the dragged window are changing: subset of {"left","right","top","bottom"}
    followers = {}  # edge -> [(wid, pid, CGRect start_frame)]
    last_push = (0.0, 0.0, 0.0, 0.0)

    @property
    def resolved(self):
        return self.wid != 0

    @property
    def moved(self):
        return self.mode == "move"


_DRAG = _Drag()
_FOLLOW_WAKE = threading.Event()  # worker re-reads the lead's live bounds; no stale frames
_FOLLOW_LOCK = threading.Lock()


def _frame_tuple(b: CGRect):
    return (b.origin.x, b.origin.y, b.size.w, b.size.h)


def _rect(t):
    return CGRect(CGPoint(t[0], t[1]), CGSize(t[2], t[3]))


def linked_followers(wid: int, frame):
    """For each edge of `frame`, the on-Space windows whose opposite edge abuts it.

    Uses the raw SLS snapshot (no AX, ~0.3 ms) so it is safe on the tap thread.
    """
    x, y, w, h = frame
    out = {"left": [], "right": [], "top": [], "bottom": []}
    windows, _ = snapshot_current_space_windows()
    for o in windows:
        if o["wid"] == wid:
            continue
        ob = window_bounds(o["wid"])
        ox, oy, ow, oh = _frame_tuple(ob)
        v_overlap = min(y + h, oy + oh) - max(y, oy)
        h_overlap = min(x + w, ox + ow) - max(x, ox)
        rec = (o["wid"], o["pid"], ob)
        if v_overlap >= LINK_MIN_OVERLAP:
            if abs((ox + ow) - x) <= LINK_GAP_PX:
                out["left"].append(rec)  # neighbour's right edge touches our left edge
            if abs(ox - (x + w)) <= LINK_GAP_PX:
                out["right"].append(rec)
        if h_overlap >= LINK_MIN_OVERLAP:
            if abs((oy + oh) - y) <= LINK_GAP_PX:
                out["top"].append(rec)
            if abs(oy - (y + h)) <= LINK_GAP_PX:
                out["bottom"].append(rec)
    return {k: v for k, v in out.items() if v}


def _changed_edges(start, now):
    sx, sy, sw, sh = start
    nx, ny, nw, nh = now
    e = []
    if abs(nx - sx) >= 1:
        e.append("left")
    if abs((nx + nw) - (sx + sw)) >= 1:
        e.append("right")
    if abs(ny - sy) >= 1:
        e.append("top")
    if abs((ny + nh) - (sy + sh)) >= 1:
        e.append("bottom")
    return tuple(e)


def follower_frame(edge: str, start: CGRect, lead):
    """New frame for a follower so its edge stays glued to the lead's moving edge."""
    lx, ly, lw, lh = lead
    sx, sy, sw, sh = _frame_tuple(start)
    if edge == "left":  # lead's left edge moved -> follower's right edge = lead.x
        return CGRect(CGPoint(sx, sy), CGSize(max(1.0, lx - sx), sh))
    if edge == "right":  # lead's right edge moved -> follower's left edge = lead.x + lead.w
        nx = lx + lw
        return CGRect(CGPoint(nx, sy), CGSize(max(1.0, (sx + sw) - nx), sh))
    if edge == "top":
        return CGRect(CGPoint(sx, sy), CGSize(sw, max(1.0, ly - sy)))
    ny = ly + lh
    return CGRect(CGPoint(sx, ny), CGSize(sw, max(1.0, (sy + sh) - ny)))


def apply_followers(lead, followers, final: bool = False) -> None:
    for edge, recs in followers.items():
        for fwid, fpid, fstart in recs:
            if final:
                ax_set_frame(fpid, fwid, follower_frame(edge, fstart, lead))  # full 3-step, exact
            else:
                ax_follow_edge(fpid, fwid, edge, fstart, lead)  # 1 IPC, live


def follow_worker():
    """Glues followers to the lead window's *current* bounds, paced to the display.

    Re-reading (rather than queuing frames) means a slow AX write can never apply an
    out-of-date frame. Pacing to ~1 update per 16 ms matters more than raw IPC cost:
    the follower app (Chrome: 5-40 ms per layout) must never accumulate a backlog of
    intermediate sizes, or it visibly lags then catches up in jumps.
    """
    last = None
    while True:
        _FOLLOW_WAKE.wait()
        _FOLLOW_WAKE.clear()
        wid, followers = _DRAG.wid, _DRAG.followers
        if not wid or not followers:
            continue
        t0 = time.perf_counter()
        with _FOLLOW_LOCK:
            lead = _frame_tuple(window_bounds(wid))
            if lead == last:
                continue  # spurious wake: lead has not actually changed since our last pass
            last = lead
            apply_followers(lead, followers)
        spent = time.perf_counter() - t0
        if spent < FOLLOW_FRAME_S:
            time.sleep(FOLLOW_FRAME_S - spent)  # wakes that arrived meanwhile coalesce into one pass


def push_followers():
    _FOLLOW_WAKE.set()


def finish_followers(wid: int, followers) -> None:
    """Final exact alignment, synchronous and ordered after any in-flight worker pass."""
    with _FOLLOW_LOCK:
        apply_followers(_frame_tuple(window_bounds(wid)), followers, final=True)


def windows_at_point(x: float, y: float, slop: float = 0.0):
    """All layer-0 windows on the current Space whose rect (padded by `slop`) contains (x, y).

    Front-to-back. Each entry is (wid, pid, start_frame_tuple, inside: bool, dist_outside).
    At a seam between two touching windows, both qualify: macOS decides which one the
    resize handle belongs to, so the caller must watch all of them for size changes.
    """
    cur_sid = SL.CGSGetActiveSpace(CID)
    if not cur_sid:
        return []
    nr = CF.CFNumberCreate(None, kCFNumberSInt64Type, C.byref(i64(cur_sid)))
    sp_arr = CF.CFArrayCreate(None, (vp * 1)(nr), 1, C.addressof(kCFTypeArrayCallBacks))
    st, ct = u64(0), u64(0)
    wids_arr = SL.CGSCopyWindowsWithOptionsAndTags(CID, 0, sp_arr, 2, C.byref(st), C.byref(ct))
    CF.CFRelease(sp_arr)
    CF.CFRelease(nr)
    if not wids_arr:
        return []
    q = SL.SLSWindowQueryWindows(CID, wids_arr, 0)
    CF.CFRelease(wids_arr)
    if not q:
        return []
    it = SL.SLSWindowQueryResultCopyWindows(q)
    CF.CFRelease(q)
    if not it:
        return []
    out = []
    while SL.SLSWindowIteratorAdvance(it):  # front-to-back order
        if SL.SLSWindowIteratorGetLevel(it) != 0 or SL.SLSWindowIteratorGetParentID(it) != 0:
            continue
        if SL.SLSWindowIteratorGetAlpha(it) <= 0.01:
            continue
        pid = SL.SLSWindowIteratorGetPID(it)
        if pid == OWN_PID:
            continue
        wid = SL.SLSWindowIteratorGetWindowID(it)
        b = window_bounds(wid)
        x0, y0, x1, y1 = b.origin.x, b.origin.y, b.origin.x + b.size.w, b.origin.y + b.size.h
        if x0 - slop <= x <= x1 + slop and y0 - slop <= y <= y1 + slop:
            inside = x0 <= x <= x1 and y0 <= y <= y1
            d = 0.0 if inside else max(x0 - x, x - x1, y0 - y, y - y1)
            out.append((wid, pid, _frame_tuple(b), inside, d))
            if inside and not slop:
                break
    CF.CFRelease(it)
    return out


def window_at_point(x: float, y: float, slop: float = 0.0):
    """(wid, pid) of the best window under (x, y): frontmost one strictly inside, else nearest edge."""
    cands = windows_at_point(x, y, slop)
    if not cands:
        return None
    inside = [c for c in cands if c[3]]
    best = inside[0] if inside else min(cands, key=lambda c: c[4])
    return (best[0], best[1])


def drag_zone(x: float, y: float, visible: CGRect):
    if x <= visible.origin.x + SNAP_EDGE_PX:
        return "left"
    if x >= visible.origin.x + visible.size.w - SNAP_EDGE_PX:
        return "right"
    return None


def on_mouse_down(pt: CGPoint):
    _DRAG.active = True
    _DRAG.start = CGPoint(pt.x, pt.y)
    _DRAG.mode = None
    _DRAG.zone = None
    _DRAG.wid = 0
    _DRAG.cands = []
    _DRAG.edges = ()
    _DRAG.followers = {}


def _begin_resize(wid, pid, start_frame, now):
    _DRAG.wid, _DRAG.pid, _DRAG.start_frame = wid, pid, start_frame
    _DRAG.mode = "resize"
    _DRAG.edges = _changed_edges(start_frame, now)
    if not CFG["linked_resize"]:
        _DRAG.followers = {}
        return
    _DRAG.followers = {e: r for e, r in linked_followers(wid, start_frame).items() if e in _DRAG.edges}
    _DRAG.last_push = now
    log(f"linked resize wid={wid} edges={_DRAG.edges} followers={ {e: [r[0] for r in v] for e, v in _DRAG.followers.items()} }")


def on_mouse_dragged(pt: CGPoint):
    if not _DRAG.active:
        return
    if not _DRAG.cands:
        if abs(pt.x - _DRAG.start.x) + abs(pt.y - _DRAG.start.y) < DRAG_MIN_TRAVEL:
            return
        cands = windows_at_point(_DRAG.start.x, _DRAG.start.y, slop=RESIZE_HANDLE_PX)
        trace(f"MT resolve start=({_DRAG.start.x:.0f},{_DRAG.start.y:.0f}) cands={[(c[0], c[3], round(c[4])) for c in cands]}")
        if not cands:
            _DRAG.active = False
            return
        _DRAG.cands = [(c[0], c[1], c[2]) for c in cands]
        inside = [c for c in cands if c[3]]
        best = inside[0] if inside else min(cands, key=lambda c: c[4])
        _DRAG.wid, _DRAG.pid, _DRAG.start_frame = best[0], best[1], best[2]

    if _DRAG.mode is None:
        # Watch EVERY candidate: at a seam macOS may hand the resize to the neighbour,
        # not the window we'd guess. First one whose size changes is the lead.
        for cwid, cpid, cstart in _DRAG.cands:
            now = _frame_tuple(window_bounds(cwid))
            if abs(now[2] - cstart[2]) >= 1 or abs(now[3] - cstart[3]) >= 1:
                _begin_resize(cwid, cpid, cstart, now)
                break
        else:
            now = _frame_tuple(window_bounds(_DRAG.wid))
            sx, sy = _DRAG.start_frame[0], _DRAG.start_frame[1]
            if abs(now[0] - sx) >= 2 or abs(now[1] - sy) >= 2:
                _DRAG.mode = "move"
            else:
                return  # text selection / scrollbar / click-hold: nothing to do yet

    if _DRAG.mode == "resize":
        if _DRAG.followers:
            now = _frame_tuple(window_bounds(_DRAG.wid))
            lp = _DRAG.last_push
            if any(abs(now[i] - lp[i]) >= RESIZE_FOLLOW_MIN_DELTA for i in range(4)):
                _DRAG.last_push = now
                push_followers()
        return

    # mode == "move": edge-zone detection for snapping
    if not CFG["drag_to_edge"]:
        return
    _, visible = screen_for_point(pt.x, pt.y)
    zone = drag_zone(pt.x, pt.y, visible)
    if zone != _DRAG.zone:
        _DRAG.zone = zone
        _DRAG.visible = visible
        if zone:
            _FOOTPRINT.show(half_rect(visible, zone))
        else:
            _FOOTPRINT.hide()


def on_mouse_up(pt: CGPoint):
    if not _DRAG.active:
        return
    _DRAG.active = False
    _FOOTPRINT.hide()
    if _DRAG.mode == "resize":
        followers, wid = _DRAG.followers, _DRAG.wid
        _DRAG.followers = {}
        if followers:
            # Off the tap thread (AX writes block), but ordered after any in-flight worker pass.
            threading.Thread(target=finish_followers, args=(wid, followers), daemon=True).start()
        return
    if not (_DRAG.mode == "move" and _DRAG.zone and _DRAG.visible):
        return
    side = _DRAG.zone
    snap_window(_DRAG.pid, _DRAG.wid, side, _DRAG.visible)
    log(f"drag snap wid={_DRAG.wid} -> {side}")
    companion_after_snap(_DRAG.wid, side, _DRAG.visible)


def companion_after_snap(snapped_wid: int, side: str, visible: CGRect):
    """Exactly one other window on this Space -> snap it opposite. 2+ -> picker."""
    if not CFG["companion_snap"] or side not in SNAP_OPPOSITE:
        return
    others = [w for w in ordered_windows_for_session(app_only=False) if w["wid"] != snapped_wid]
    if not others:
        return
    opp = SNAP_OPPOSITE[side]
    if len(others) == 1:
        w = others[0]
        snap_window(w["pid"], w["wid"], opp, visible)
        log(f"auto-snap companion wid={w['wid']} -> {opp}")
        return
    _PICKER.open(others, opp, visible)


class SnapFootprint:
    """Translucent preview of the target half while the cursor is in a snap zone."""

    def __init__(self):
        panel = send(cls("NSPanel"), "alloc")
        panel = send(panel, "initWithContentRect:styleMask:backing:defer:", vp, (CGRect, u64, u64, cbool),
                     CGRect(CGPoint(0, 0), CGSize(10, 10)), 1 << 7, 2, False)
        send(panel, "setLevel:", None, (i64,), 0)  # NSNormalWindowLevel: under the dragged window
        send(panel, "setCollectionBehavior:", None, (u64,), 1 | 16 | 64 | 256)
        send(panel, "setOpaque:", None, (cbool,), False)
        send(panel, "setBackgroundColor:", None, (vp,), send(cls("NSColor"), "clearColor"))
        send(panel, "setHasShadow:", None, (cbool,), False)
        send(panel, "setIgnoresMouseEvents:", None, (cbool,), True)
        view = send(cls("NSView"), "alloc")
        view = send(view, "initWithFrame:", vp, (CGRect,), CGRect(CGPoint(0, 0), CGSize(10, 10)))
        send(view, "setWantsLayer:", None, (cbool,), True)
        layer = send(view, "layer")
        send(layer, "setBackgroundColor:", None, (vp,), cgcolor(0.24, 0.52, 0.96, 0.22))
        send(layer, "setBorderColor:", None, (vp,), cgcolor(0.36, 0.66, 1.0, 0.85))
        send(layer, "setBorderWidth:", None, (f64,), 2.0)
        send(layer, "setCornerRadius:", None, (f64,), 10.0)
        send(panel, "setContentView:", None, (vp,), view)
        self.panel, self.view = panel, view
        self.visible = False

    def show(self, rect_tl: CGRect):
        main_h = CG.CGDisplayBounds(0).size.h
        pad = 6.0
        r = CGRect(CGPoint(rect_tl.origin.x + pad, main_h - rect_tl.origin.y - rect_tl.size.h + pad),
                   CGSize(rect_tl.size.w - pad * 2, rect_tl.size.h - pad * 2))
        send(cls("CATransaction"), "begin")
        send(cls("CATransaction"), "setDisableActions:", None, (cbool,), True)
        send(self.panel, "setFrame:display:", None, (CGRect, cbool), r, False)
        send(self.view, "setFrame:", None, (CGRect,), CGRect(CGPoint(0, 0), r.size))
        if not self.visible:
            send(self.panel, "orderFrontRegardless")
            self.visible = True
        send(cls("CATransaction"), "commit")
        send(cls("CATransaction"), "flush")

    def hide(self):
        if not self.visible:
            return
        self.visible = False
        send(self.panel, "orderOut:", None, (vp,), None)


_FOOTPRINT: Any = None


class SnapPicker:
    """'Which window goes in the other half?' - reuses the switcher HUD cards."""

    def __init__(self):
        self.active = False
        self.windows = []
        self.idx = 0
        self.side = "right"
        self.visible = None

    def open(self, windows, side: str, visible: CGRect):
        self.windows = windows[:_HUD.max_cards]
        self.idx = 0
        self.side = side
        self.visible = visible
        self.active = True
        _HUD.render(self.windows, self.idx, reposition=True, anchor=half_rect(visible, side))
        warm_thumbnails_async(self.windows, on_changed=_mark_dirty)

    def close(self):
        self.active = False
        self.windows = []
        _HUD.hide()

    def confirm(self):
        if not self.windows:
            self.close()
            return
        w = self.windows[self.idx]
        side, visible = self.side, self.visible
        self.close()
        snap_window(w["pid"], w["wid"], side, visible, raise_after=True)
        log(f"picker: wid={w['wid']} -> {side}")

    def step(self, delta: int):
        n = len(self.windows)
        if n:
            self.idx = (self.idx + delta) % n
            _HUD.render(self.windows, self.idx, reposition=False)

    def click(self, pt: CGPoint) -> bool:
        """True if the click was consumed (hit a card or the panel)."""
        hit = _HUD.card_at(pt)
        if hit is None:
            self.close()
            return False
        if hit >= 0:
            self.idx = hit
            self.confirm()
        return True


_PICKER = SnapPicker()


# ---------------------------------------------------------------------------
# Event tap + main
# ---------------------------------------------------------------------------
def on_event(proxy, etype, event, refcon):
    if etype in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
        log(f"event tap disabled ({etype:#x}); re-enabling")
        daemon.reenable_tap()
        return event

    # Mouse: drag-to-edge / linked resize tracking; never swallowed except picker hits.
    if etype == kCGEventLeftMouseDown:
        pt = CG.CGEventGetLocation(event)
        if _PICKER.active:
            return None if _PICKER.click(pt) else event
        on_mouse_down(pt)
        return event
    if etype == kCGEventLeftMouseDragged:
        if _DRAG.active:
            on_mouse_dragged(CG.CGEventGetLocation(event))
        return event
    if etype == kCGEventLeftMouseUp:
        if _DRAG.active:
            on_mouse_up(CG.CGEventGetLocation(event))
        return event
    if etype != kCGEventKeyDown:
        return event

    key = CG.CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
    mods = CG.CGEventGetFlags(event) & MODIFIER_MASK

    # The companion picker owns the keyboard while it is open.
    if _PICKER.active:
        if key == KEY_ESC:
            _PICKER.close()
        elif key in (KEY_RETURN, KEY_SPACE):
            _PICKER.confirm()
        elif key in (KEY_TAB, KEY_RIGHT, KEY_J, KEY_DOWN):
            _PICKER.step(-1 if (key == KEY_TAB and mods & FLAG_SHIFT) else 1)
        elif key in (KEY_LEFT, KEY_K, KEY_UP):
            _PICKER.step(-1)
        else:
            _PICKER.close()
            return event
        return None

    combo = (key, mods)
    if combo in WIDTH_HOTKEYS:  # autorepeat allowed: hold to keep going
        return None if width_hotkey(WIDTH_HOTKEYS[combo]) else event
    if combo in SNAP_HOTKEYS:
        if CG.CGEventGetIntegerValueField(event, kCGKeyboardEventAutorepeat):
            return None
        side = SNAP_HOTKEYS[combo]
        t0 = time.perf_counter()
        ok = snap_hotkey(side)
        log(f"hotkey snap {side}: {'queued' if ok else 'no focused window'} ({(time.perf_counter() - t0) * 1e3:.1f} ms in tap)")
        return None if ok else event
    return event


def main():
    global _HUD, _FOOTPRINT
    if "--check-config" in sys.argv:
        print("config OK:", CFG_PATH)
        return
    daemon.wait_for_accessibility("snap")

    app = send(cls("NSApplication"), "sharedApplication")
    send(app, "setActivationPolicy:", cbool, (i64,), 1)
    _HUD = CardHUD(max_cards=12, columns=4)
    _FOOTPRINT = SnapFootprint()
    threading.Thread(target=snap_worker, name="snap-worker", daemon=True).start()
    threading.Thread(target=follow_worker, name="follow-worker", daemon=True).start()

    mask = 1 << kCGEventKeyDown
    if CFG["drag_to_edge"] or CFG["linked_resize"]:
        mask |= (1 << kCGEventLeftMouseDown) | (1 << kCGEventLeftMouseDragged) | (1 << kCGEventLeftMouseUp)

    def tick_cb(timer, info):  # 50 ms: repaint picker thumbnails the background refresh changed
        global _PICKER_DIRTY
        if _PICKER_DIRTY and _PICKER.active:
            _PICKER_DIRTY = False
            _HUD.refresh_thumbnails_in_place(_PICKER.windows)

    tcb = TIMER_CB(tick_cb)
    timer = CF.CFRunLoopTimerCreate(None, CF.CFAbsoluteTimeGetCurrent() + 0.05, 0.05, 0, 0, tcb, None)
    CF.CFRunLoopAddTimer(CF.CFRunLoopGetMain(), timer, kCFRunLoopCommonModes)

    feats = [n for n, on in (("hotkeys", HK_SNAP or HK_WIDTH), ("drag-to-edge", CFG["drag_to_edge"]),
                             ("companion", CFG["companion_snap"]), ("linked-resize", CFG["linked_resize"])) if on]
    daemon.install("snap", mask, on_event, ready_msg=f"snap ready: {' '.join(feats)} (config {CFG_PATH})")
    send(app, "run")


if __name__ == "__main__":
    main()
