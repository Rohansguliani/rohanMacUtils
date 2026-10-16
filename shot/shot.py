#!/usr/bin/python3
"""shot: area screenshots, OCR, and a quick annotate-and-copy editor (a Shottr replacement).

  Opt+1   drag an area -> PNG on the clipboard (and optionally a file)
  Opt+2   drag an area -> the text in it (Apple Vision OCR) on the clipboard
  Opt+`   the last Opt+1 / Opt+2 capture in the editor: arrows, boxes, pen, text, pixelate;
          Cmd+C copies the annotated image, Cmd+S saves it, Esc closes. (Nothing captured
          yet -> starts an area selection that opens straight into the editor.)

The selection overlay is a pre-built, always-resident full-screen panel: the hotkey only
orders it front, so there is nothing to construct, load, or warm when you press it. The
pixels are grabbed with CGWindowListCreateImage *before* the dimming layer appears (~1 ms)
and the selection is cut from that frozen frame, so what you copy is what was on screen
when you pressed the key, cursor and overlay excluded.

Config: ~/.config/mactools/shot.json. Permissions for /usr/bin/python3: Accessibility (the
hotkey tap) and Screen Recording (the pixels; without it you get wallpaper).
"""

from __future__ import annotations

import ctypes as C
import math
import os
import sys
import threading
import time
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "util"))
import config  # noqa: E402
import daemon  # noqa: E402
import imaging  # noqa: E402
from macbridge import (CF, CG, CID, SL, CGPoint, CGRect, CGSize, FLAG_CMD, FLAG_SHIFT, MODIFIER_MASK, TIMER_CB, cbool,  # noqa: E402
                       cf_to_i64, cfstr, f64, i64, kCFBooleanTrue, kCFRunLoopCommonModes, kCGEventFlagsChanged,
                       kCGEventKeyDown, kCGEventLeftMouseDown, kCGEventLeftMouseDragged, kCGEventLeftMouseUp,
                       kCGEventMouseMoved, kCGEventRightMouseDown, kCGEventTapDisabledByTimeout,
                       kCGEventTapDisabledByUserInput, kCGKeyboardEventKeycode, kCGWindowListOptionOnScreenOnly, K_WIN_NUMBER, log, u32, u64, vp)
from objc import cls, define_class, nsstr, send  # noqa: E402

C.CDLL("/System/Library/Frameworks/QuartzCore.framework/QuartzCore")
APPKIT = C.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")

DEFAULTS = {
    "area_to_clipboard": "opt+1",
    "ocr_to_clipboard": "opt+2",
    "edit_last": "opt+`",
    "save_dir": "",                 # also write Opt+1 shots here, e.g. "~/Desktop"; "" = clipboard only
    "ocr_languages": ["en-US"],     # Vision language codes, in priority order
    "ocr_auto_language": False,     # let Vision guess the language (worse on short snippets: a lone x becomes Japanese)
    "editor_color": "#ff3b30",
    "editor_stroke": 4,
    "editor_canvas": "#ffffff",     # the drawing margin you get by enlarging the editor window
    "debug": False,
}
CFG, CFG_PATH = config.load("shot", DEFAULTS)
HK = {}
for key, mode in (("area_to_clipboard", "copy"), ("ocr_to_clipboard", "ocr"), ("edit_last", "edit")):
    if CFG[key]:
        HK[config.parse_hotkey(CFG[key])] = mode
if not HK:
    raise config.ConfigError(f"{CFG_PATH}: every hotkey is disabled")
SAVE_DIR = os.path.expanduser(CFG["save_dir"]) if CFG["save_dir"] else ""
TRACE = bool(CFG["debug"])
KEY_ESC, KEY_RETURN, KEY_SPACE, KEY_DELETE, KEY_TAB = 53, 36, 49, 51, 48


def trace(msg):
    if TRACE:
        log(msg)


def parse_color(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


EDIT_COLOR = parse_color(CFG["editor_color"])
EDIT_STROKE = float(CFG["editor_stroke"])
CANVAS_COLOR = parse_color(CFG["editor_canvas"])

# --- AppKit / CoreAnimation helpers -------------------------------------------------
# A styleMask of exactly 0 (plain borderless) from a background agent never reaches the
# screen on 26.x (AppKit reports isVisible, WindowServer keeps it offscreen); the
# non-activating-panel bit is what the switcher HUD uses and it works.
NSWindowStyleMaskBorderless = 1 << 7  # NSWindowStyleMaskNonactivatingPanel
NSWindowStyleMaskTitled, NSWindowStyleMaskClosable, NSWindowStyleMaskResizable = 1, 2, 8
NSBackingStoreBuffered = 2
kCGScreenSaverWindowLevel = 1000
CATransaction = None  # resolved after NSApplication exists


def cgcolor(r, g, b, a=1.0):
    return send(send(cls("NSColor"), "colorWithSRGBRed:green:blue:alpha:", vp, (f64, f64, f64, f64), r, g, b, a), "CGColor")


def no_anim(fn):
    """Run fn inside a CATransaction with implicit animations off."""
    send(CATransaction, "begin")
    send(CATransaction, "setDisableActions:", None, (cbool,), True)
    try:
        return fn()
    finally:
        send(CATransaction, "commit")


def display_for_point(x, y):
    did, cnt = u32(0), u32(0)
    CG.CGGetDisplaysWithPoint(CGPoint(x, y), 1, C.byref(did), C.byref(cnt))
    return did.value if cnt.value else CG.CGMainDisplayID()


def window_on_screen(win):
    """Ask the window server, not AppKit: once, after a display change, orderFront on a
    long-lived panel did nothing while AppKit still considered it visible. Ordering is
    flushed at the end of a run-loop turn, so ask a little later, never right after."""
    wid = send(win, "windowNumber", i64)
    arr = CG.CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, 0)
    found = False
    for i in range(CF.CFArrayGetCount(arr)):
        if cf_to_i64(CF.CFDictionaryGetValue(CF.CFArrayGetValueAtIndex(arr, i), K_WIN_NUMBER)) == wid:
            found = True
            break
    CF.CFRelease(arr)
    return found


_PENDING: dict = {}  # one-shot timer callbacks, alive until they fire (ctypes callbacks are unhashable: keyed by id)


def after(delay_s, fn):
    """Run fn on the main loop after delay_s."""
    def fire(timer, info):
        _PENDING.pop(id(cb), None)
        fn()

    cb = TIMER_CB(fire)
    _PENDING[id(cb)] = cb
    t = CF.CFRunLoopTimerCreate(None, CF.CFAbsoluteTimeGetCurrent() + delay_s, 0, 0, 0, cb, None)
    CF.CFRunLoopAddTimer(CF.CFRunLoopGetMain(), t, kCFRunLoopCommonModes)
    CF.CFRelease(t)  # the run loop holds it until it fires


def cocoa_rect(tl: CGRect):
    """Top-left global rect -> Cocoa bottom-left global rect."""
    main_h = CG.CGDisplayBounds(CG.CGMainDisplayID()).size.h
    return CGRect(CGPoint(tl.origin.x, main_h - tl.origin.y - tl.size.h), tl.size)


def norm_rect(a: CGPoint, b: CGPoint):
    x0, x1 = sorted((a.x, b.x))
    y0, y1 = sorted((a.y, b.y))
    return CGRect(CGPoint(math.floor(x0), math.floor(y0)), CGSize(math.ceil(x1) - math.floor(x0), math.ceil(y1) - math.floor(y0)))


# --- The selection overlay ------------------------------------------------------------
class Overlay:
    """One borderless panel per capture, covering the display under the cursor. Built
    once, reused: showing it is setContents (the frozen frame) + setFrame + orderFront.

    The frozen frame IS the overlay's content: whatever happens underneath (video, a
    blinking caret, a notification) stays out of the picture and out of the selection."""

    def __init__(self):
        panel = send(send(cls("NSPanel"), "alloc"), "initWithContentRect:styleMask:backing:defer:", vp,
                     (CGRect, u64, u64, cbool), CGRect(CGPoint(0, 0), CGSize(100, 100)), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
        send(panel, "setLevel:", None, (i64,), kCGScreenSaverWindowLevel)
        send(panel, "setCollectionBehavior:", None, (u64,), 1 | 16 | 64 | 256)  # all Spaces, stationary, ignores cycle, full-screen aux
        send(panel, "setOpaque:", None, (cbool,), True)
        send(panel, "setBackgroundColor:", None, (vp,), send(cls("NSColor"), "blackColor"))
        send(panel, "setHasShadow:", None, (cbool,), False)
        # The panel accepts mouse events so the app underneath stops getting mouseMoved and
        # cannot reset the cursor; the event tap still sees (and swallows) everything first.
        send(panel, "setIgnoresMouseEvents:", None, (cbool,), False)
        send(panel, "setAcceptsMouseMovedEvents:", None, (cbool,), True)
        send(panel, "setAnimationBehavior:", None, (i64,), 2)  # NSWindowAnimationBehaviorNone
        self.panel = panel
        view = send(send(cls("NSView"), "alloc"), "initWithFrame:", vp, (CGRect,), CGRect(CGPoint(0, 0), CGSize(100, 100)))
        send(view, "setWantsLayer:", None, (cbool,), True)
        root = send(view, "layer")
        send(root, "setBackgroundColor:", None, (vp,), cgcolor(0, 0, 0, 1.0))
        send(panel, "setContentView:", None, (vp,), view)
        self.frozen = send(cls("CALayer"), "layer")  # the captured display
        send(self.frozen, "setMagnificationFilter:", None, (vp,), nsstr("nearest"))
        send(self.frozen, "setMinificationFilter:", None, (vp,), nsstr("nearest"))
        send(root, "addSublayer:", None, (vp,), self.frozen)
        # dim = 4 rectangles around the hole so the selection shows the frozen pixels undimmed
        self.dims = []
        for _ in range(4):
            d = send(cls("CALayer"), "layer")
            send(d, "setBackgroundColor:", None, (vp,), cgcolor(0, 0, 0, 0.42))
            send(root, "addSublayer:", None, (vp,), d)
            self.dims.append(d)
        self.border = send(cls("CAShapeLayer"), "layer")
        send(self.border, "setFillColor:", None, (vp,), None)
        send(self.border, "setStrokeColor:", None, (vp,), cgcolor(1, 1, 1, 0.95))
        send(self.border, "setLineWidth:", None, (f64,), 1.0)
        send(root, "addSublayer:", None, (vp,), self.border)
        self.label = send(cls("CATextLayer"), "layer")
        send(self.label, "setFontSize:", None, (f64,), 12.0)
        send(self.label, "setFont:", None, (vp,), nsstr("Menlo"))
        send(self.label, "setForegroundColor:", None, (vp,), cgcolor(1, 1, 1, 1))
        send(self.label, "setBackgroundColor:", None, (vp,), cgcolor(0, 0, 0, 0.7))
        send(self.label, "setCornerRadius:", None, (f64,), 4.0)
        send(self.label, "setAlignmentMode:", None, (vp,), nsstr("center"))
        send(self.label, "setContentsScale:", None, (f64,), 2.0)
        send(root, "addSublayer:", None, (vp,), self.label)
        self.root = root
        self.shown = False
        self.frame_tl: Any = None
        self.h = 0.0

    def show(self, frame_tl: CGRect, frame_img, scale: float):
        self.frame_tl = frame_tl
        self.h = frame_tl.size.h

        def build():
            send(self.panel, "setFrame:display:", None, (CGRect, cbool), cocoa_rect(frame_tl), False)
            send(self.frozen, "setFrame:", None, (CGRect,), CGRect(CGPoint(0, 0), frame_tl.size))
            send(self.frozen, "setContentsScale:", None, (f64,), scale)
            send(self.frozen, "setContents:", None, (vp,), frame_img)
            self.set_selection(None)

        no_anim(build)
        send(self.panel, "orderFrontRegardless")
        self.shown = True

    def hide(self):
        if self.shown:
            send(self.panel, "orderOut:", None, (vp,), None)
            no_anim(lambda: send(self.frozen, "setContents:", None, (vp,), None))  # drop the 35 MB frame
            self.shown = False

    def set_selection(self, sel_tl):
        """sel_tl: global top-left rect or None (full dim)."""
        W, H = self.frame_tl.size.w, self.frame_tl.size.h
        fx, fy = self.frame_tl.origin.x, self.frame_tl.origin.y

        def apply():
            if sel_tl is None or sel_tl.size.w < 1 or sel_tl.size.h < 1:
                rects = [CGRect(CGPoint(0, 0), CGSize(W, H)), CGRect(), CGRect(), CGRect()]
                send(self.border, "setHidden:", None, (cbool,), True)
                send(self.label, "setHidden:", None, (cbool,), True)
            else:
                # local, Cocoa (bottom-left) coordinates
                lx, lw = sel_tl.origin.x - fx, sel_tl.size.w
                ltop = sel_tl.origin.y - fy
                ly = H - ltop - sel_tl.size.h
                lh = sel_tl.size.h
                rects = [CGRect(CGPoint(0, 0), CGSize(W, ly)),                       # below
                         CGRect(CGPoint(0, ly + lh), CGSize(W, H - ly - lh)),        # above
                         CGRect(CGPoint(0, ly), CGSize(lx, lh)),                     # left
                         CGRect(CGPoint(lx + lw, ly), CGSize(W - lx - lw, lh))]      # right
                path = CG.CGPathCreateWithRect(CGRect(CGPoint(lx + 0.5, ly + 0.5), CGSize(lw - 1, lh - 1)), None)
                send(self.border, "setPath:", None, (vp,), path)
                CG.CGPathRelease(path)
                send(self.border, "setHidden:", None, (cbool,), False)
                text = f" {int(lw)} x {int(lh)} "
                tw = 8.0 * len(text)
                tx = min(max(0.0, lx), W - tw)
                ty = ly - 22 if ly - 22 >= 0 else ly + lh + 4
                send(self.label, "setString:", None, (vp,), nsstr(text))
                send(self.label, "setFrame:", None, (CGRect,), CGRect(CGPoint(tx, ty), CGSize(tw, 18)))
                send(self.label, "setHidden:", None, (cbool,), False)
            for layer, r in zip(self.dims, rects):
                send(layer, "setFrame:", None, (CGRect,), r)

        no_anim(apply)


# --- Capture session state --------------------------------------------------------------
class Session:
    active = False
    mode = "copy"
    frame: Any = None       # CGImageRef of the whole display, frozen at hotkey time
    frame_tl: Any = None    # that display's global top-left rect
    scale = 1.0
    anchor: Any = None
    cur: Any = None
    dragging = False
    refresh_queued = False  # one overlay refresh per main-loop turn, however many drag events arrive
    t_hotkey = 0.0


S = Session()
_OVERLAY: Any = None
_EDITOR: Any = None


def clamp_to_frame(pt):
    f = S.frame_tl
    return CGPoint(min(max(pt.x, f.origin.x), f.origin.x + f.size.w), min(max(pt.y, f.origin.y), f.origin.y + f.size.h))


def queue_refresh():
    if not S.refresh_queued:
        S.refresh_queued = True
        main_async(_refresh_overlay)


def _refresh_overlay():
    S.refresh_queued = False
    if not S.active:
        return
    set_crosshair()
    if S.dragging and S.anchor is not None:
        _OVERLAY.set_selection(norm_rect(S.anchor, S.cur))


def begin(mode):
    """Hotkey handler, tap thread -> everything UI happens on the main thread via a
    performSelector hop; the frame grab happens here first so it predates the overlay."""
    if S.active:
        return
    S.t_hotkey = time.perf_counter()
    ev = CG.CGEventCreate(None)
    pt = CG.CGEventGetLocation(ev)
    CF.CFRelease(ev)
    did = display_for_point(pt.x, pt.y)
    frame_tl = CG.CGDisplayBounds(did)
    t0 = time.perf_counter()
    img = imaging.capture(frame_tl)
    dt = (time.perf_counter() - t0) * 1e3
    if not img:
        log("capture failed (Screen Recording not granted to /usr/bin/python3?)")
        return
    S.active, S.mode, S.frame, S.frame_tl = True, mode, img, frame_tl
    S.scale = imaging.size(img)[0] / frame_tl.size.w  # backing pixels per point of this display
    S.anchor = S.cur = None
    S.dragging = S.refresh_queued = False
    trace(f"{mode}: frozen display {did} {imaging.size(img)} in {dt:.1f} ms")
    main_async(_show_overlay)


def _show_overlay():
    t0 = time.perf_counter()
    _OVERLAY.show(S.frame_tl, S.frame, S.scale)
    t1 = time.perf_counter()
    set_crosshair()
    CF.CFRunLoopTimerSetNextFireDate(_CURSOR_TIMER, CF.CFAbsoluteTimeGetCurrent() + CURSOR_TICK_S)
    trace(f"overlay shown in {(t1 - t0) * 1e3:.1f} ms (+{(time.perf_counter() - t1) * 1e3:.1f} ms cursor), {(time.perf_counter() - S.t_hotkey) * 1e3:.1f} ms after the hotkey")
    after(VERIFY_AFTER_S, _verify_overlay)


VERIFY_AFTER_S = 0.1  # window ordering is asynchronous; one run-loop turn is plenty, this is generous


def _verify_overlay(rebuilt=False):
    """The resident panel once stopped coming on screen (after a display change, in a process
    that had run for an hour): captures still worked, the user saw nothing. If the window
    server does not list the overlay, rebuild it once and show again; log it either way."""
    global _OVERLAY
    if not S.active or window_on_screen(_OVERLAY.panel):
        return
    if rebuilt:
        log("overlay still not on screen after the rebuild")
        return
    log("overlay did not come on screen; rebuilding the panel")
    old = _OVERLAY
    old.hide()
    _OVERLAY = Overlay()
    _OVERLAY.show(S.frame_tl, S.frame, S.scale)
    if S.dragging and S.anchor is not None:
        _OVERLAY.set_selection(norm_rect(S.anchor, S.cur))
    set_crosshair()
    send(old.panel, "close")
    after(VERIFY_AFTER_S, lambda: _verify_overlay(rebuilt=True))


_CROSSHAIR: Any = None
_CURSOR_TIMER: Any = None
CURSOR_TICK_S = 0.03


def set_crosshair():
    """NSCursor state is per-app and we are not frontmost: the app underneath resets its cursor
    to the arrow 5-30 ms after the overlay covers it (measured), and re-asserts it on every
    mouseMoved it gets. So: set ours after showing, after every mouse event, and from a 30 ms
    timer while selecting. `set` is seed-checked inside AppKit (a no-op unless some process
    changed the cursor since ours), so the repeats cost nothing."""
    global _CROSSHAIR
    if _CROSSHAIR is None:
        _CROSSHAIR = send(cls("NSCursor"), "crosshairCursor")
    send(_CROSSHAIR, "set")


def _cursor_tick(timer, info):
    if S.active:
        set_crosshair()
    else:
        CF.CFRunLoopTimerSetNextFireDate(timer, CF.CFAbsoluteTimeGetCurrent() + 1e9)  # park until the next capture


_CURSOR_CB = TIMER_CB(_cursor_tick)


def cancel():
    S.active = False
    main_async(_teardown)


def _teardown():
    CF.CFRunLoopTimerSetNextFireDate(_CURSOR_TIMER, CF.CFAbsoluteTimeGetCurrent() + 1e9)
    _OVERLAY.hide()
    send(send(cls("NSCursor"), "arrowCursor"), "set")
    if S.frame:
        CG.CGImageRelease(S.frame)
        S.frame = None


def finish(sel_tl: CGRect):
    """Mouse up with a real selection: cut it out of the frozen frame and dispatch."""
    S.active = False
    sc = S.scale
    px = CGRect(CGPoint((sel_tl.origin.x - S.frame_tl.origin.x) * sc, (sel_tl.origin.y - S.frame_tl.origin.y) * sc),
                CGSize(sel_tl.size.w * sc, sel_tl.size.h * sc))
    img = imaging.crop(S.frame, px)
    mode = S.mode
    main_async(_teardown)
    if not img:
        log("crop failed")
        return
    remember(img, sel_tl, sc)
    if mode == "copy":
        threading.Thread(target=_deliver_copy, args=(img, sel_tl), daemon=True).start()
    elif mode == "ocr":
        threading.Thread(target=_deliver_ocr, args=(img, sel_tl), daemon=True).start()
    else:
        main_async(lambda: _EDITOR.open(img, sel_tl, sc))


_LAST: Any = None  # (retained CGImageRef, sel_tl, scale) of the most recent capture, for Opt+`


def remember(img, sel_tl, scale):
    global _LAST
    if _LAST:
        CG.CGImageRelease(_LAST[0])
    _LAST = (CG.CGImageRetain(img), sel_tl, scale)


def open_last():
    """Opt+`: the last Opt+1 / Opt+2 capture in the editor. Already open -> just bring it
    back; nothing captured yet -> fall back to a fresh selection that opens the editor."""
    if _EDITOR.open_:
        main_async(_EDITOR.refocus)
        return
    if _LAST is None:
        begin("edit")
        return
    img, sel_tl, sc = _LAST
    main_async(lambda: _EDITOR.open(CG.CGImageRetain(img), sel_tl, sc))


def _deliver_copy(img, sel_tl):
    t0 = time.perf_counter()
    png = imaging.encode(img, "public.png")
    tiff = imaging.encode(img, "public.tiff")
    main_sync(lambda: imaging.pasteboard_set_image(png, tiff))
    msg = f"copied {int(sel_tl.size.w)}x{int(sel_tl.size.h)} ({CF.CFDataGetLength(png) // 1024} KB png) in {(time.perf_counter() - t0) * 1e3:.0f} ms"
    if SAVE_DIR:
        os.makedirs(SAVE_DIR, exist_ok=True)
        path = imaging.unique_path(SAVE_DIR, imaging.timestamp_name())
        msg += f"; saved {path}" if imaging.write_png(img, path) else f"; FAILED to save {path}"
    CF.CFRelease(png)
    CF.CFRelease(tiff)
    CG.CGImageRelease(img)
    log(msg)


def _deliver_ocr(img, sel_tl):
    t0 = time.perf_counter()
    lines = imaging.ocr(img, CFG["ocr_languages"], auto_language=CFG["ocr_auto_language"])
    CG.CGImageRelease(img)
    text = "\n".join(lines)
    if text:
        main_sync(lambda: imaging.pasteboard_set_text(text))
    log(f"ocr {int(sel_tl.size.w)}x{int(sel_tl.size.h)}: {len(lines)} lines, {len(text)} chars in {(time.perf_counter() - t0) * 1e3:.0f} ms"
        + ("" if text else " (no text found; clipboard untouched)"))


# --- main-thread hop (AppKit must run on the main thread; the tap runs on its own) --------
_MAIN_Q = []
_MAIN_LOCK = threading.Lock()
_MAIN_SRC: Any = None
_MAIN_THREAD = threading.main_thread()


def main_async(fn):
    with _MAIN_LOCK:
        _MAIN_Q.append(fn)
    CF.CFRunLoopSourceSignal(_MAIN_SRC)
    CF.CFRunLoopWakeUp(CF.CFRunLoopGetMain())


def main_sync(fn):
    if threading.current_thread() is _MAIN_THREAD:
        return fn()
    done = threading.Event()
    box = []

    def run():
        try:
            box.append(fn())
        finally:
            done.set()

    main_async(run)
    if not done.wait(2.0):
        log("main thread did not answer within 2 s")
    return box[0] if box else None


def _drain_main(info):
    while True:
        with _MAIN_LOCK:
            if not _MAIN_Q:
                return
            fn = _MAIN_Q.pop(0)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - a bad callback must not kill the run loop
            log(f"main-thread callback failed: {e!r}")


# --- Editor -----------------------------------------------------------------------------
TOOLS = ("arrow", "rect", "pen", "text", "pixelate")
TOOL_KEYS = {0: "arrow", 15: "rect", 35: "pen", 17: "text", 11: "pixelate"}  # A R P T B
# toolbar: (id, label, key hint). Buttons are layers; a click is routed by rect in mouse_down.
TOOLBAR = [("arrow", "↗", "A"), ("rect", "▭", "R"), ("pen", "✎", "P"), ("text", "T", "T"), ("pixelate", "▒", "B"),
           None, ("undo", "↶", "⌘Z"), ("copy", "Copy", "⌘C"), ("save", "Save", "⌘S")]
BAR_H = 34.0
BTN_H, BTN_PAD, BTN_GAP = 24.0, 8.0, 5.0
EDGE = 2.5  # window-edge band left to AppKit's resize grips (measured: the outer 2 pt of the frame)


class Editor:
    """A titled window showing the shot at 1:1 points with an annotation layer on top.
    Drawing is done through the event tap too (the window never needs first-responder
    plumbing), hit-tested against the window's frame. Enlarging the window adds a drawing
    margin around the shot; what you copy is the whole canvas (shot + margin)."""

    def __init__(self):
        self.win = None
        self.open_ = False
        self.tool = "pen"
        self.items = []     # CALayers in paint order (undo stack)
        self.image = None
        self.sel_tl = CGRect()
        self.scale = 1.0
        self.w = self.h = 0.0       # the shot, in points
        self.slack = 0.0            # toolbar width beyond a narrow shot (dark strips, not exported)
        self.off = (0.0, 0.0)       # shot's top-left within the canvas (margin above/left of it)
        self.cv: Any = None         # canvas (gx, gy, w, h) at the last layout, global top-left
        self.bar_min_w = 0.0
        self.buttons = []
        self.focused = False
        self.drag_start = None
        self.live = None
        self.pen_path = None
        self.text_buf = ""
        self.text_layer = None
        self.text_pos = None

    def _build(self, w, h):
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
        win = send(send(cls("NSWindow"), "alloc"), "initWithContentRect:styleMask:backing:defer:", vp,
                   (CGRect, u64, u64, cbool), CGRect(CGPoint(0, 0), CGSize(w, h + BAR_H)), style, NSBackingStoreBuffered, False)
        send(win, "setTitle:", None, (vp,), nsstr("shot"))
        send(win, "setLevel:", None, (i64,), 3)  # floating
        send(win, "setReleasedWhenClosed:", None, (cbool,), False)
        send(win, "setCollectionBehavior:", None, (u64,), 1 | 256)
        send(win, "setAnimationBehavior:", None, (i64,), 2)
        view = send(send(cls("NSView"), "alloc"), "initWithFrame:", vp, (CGRect,), CGRect(CGPoint(0, 0), CGSize(w, h + BAR_H)))
        send(view, "setWantsLayer:", None, (cbool,), True)
        root = send(view, "layer")
        send(root, "setBackgroundColor:", None, (vp,), cgcolor(0.11, 0.11, 0.12, 1.0))
        # canvas = margin + shot + annotations, sits below the toolbar; flattened together on copy
        self.canvas = send(cls("CALayer"), "layer")
        send(self.canvas, "setMasksToBounds:", None, (cbool,), True)
        send(self.canvas, "setBackgroundColor:", None, (vp,), cgcolor(*CANVAS_COLOR))
        send(root, "addSublayer:", None, (vp,), self.canvas)
        # page = the shot and its annotations, moved as one; annotations may spill into the margin
        self.page = send(cls("CALayer"), "layer")
        send(self.canvas, "addSublayer:", None, (vp,), self.page)
        self.img_layer = send(cls("CALayer"), "layer")
        send(self.page, "addSublayer:", None, (vp,), self.img_layer)
        self.ann = send(cls("CALayer"), "layer")
        send(self.page, "addSublayer:", None, (vp,), self.ann)
        # toolbar strip along the top (Cocoa coords: top = high y)
        self.bar = send(cls("CALayer"), "layer")
        send(self.bar, "setBackgroundColor:", None, (vp,), cgcolor(0.16, 0.16, 0.17, 1.0))
        send(root, "addSublayer:", None, (vp,), self.bar)
        self.buttons = []  # (id, layer, x0, x1) in bar-local coords
        x = BTN_PAD
        for entry in TOOLBAR:
            if entry is None:
                x += 14.0
                continue
            bid, label, hint = entry
            text = f"{label} {hint}"
            bw = 7.4 * len(text) + 14.0
            b = send(cls("CATextLayer"), "layer")
            send(b, "setString:", None, (vp,), nsstr(text))
            send(b, "setFontSize:", None, (f64,), 12.0)
            send(b, "setFont:", None, (vp,), nsstr("Helvetica"))
            send(b, "setAlignmentMode:", None, (vp,), nsstr("center"))
            send(b, "setContentsScale:", None, (f64,), 2.0)
            send(b, "setCornerRadius:", None, (f64,), 6.0)
            send(b, "setFrame:", None, (CGRect,), CGRect(CGPoint(x, (BAR_H - BTN_H) / 2 + 4), CGSize(bw, BTN_H - 6)))
            send(self.bar, "addSublayer:", None, (vp,), b)
            self.buttons.append((bid, b, x, x + bw))
            x += bw + BTN_GAP
        self.bar_min_w = x + BTN_PAD
        send(win, "setContentView:", None, (vp,), view)
        self.win, self.view, self.root = win, view, root
        self._paint_buttons()
        ed = self

        def relayout(_self, _cmd, _note):
            ed._layout()

        def should_close(_self, _cmd, _sender):
            ed.close()  # the red button: hide and drop the shot like Esc does, never destroy the window
            return False

        k = define_class("ShotEditorDelegate", "NSObject", {
            "windowDidResize:": (None, (vp,), relayout, "v@:@"),
            "windowDidMove:": (None, (vp,), relayout, "v@:@"),
            "windowShouldClose:": (cbool, (vp,), should_close, "B@:@"),
        })
        self.delegate = send(send(k, "alloc"), "init")
        send(win, "setDelegate:", None, (vp,), self.delegate)

    def _paint_buttons(self):
        for bid, b, _, _ in self.buttons:
            on = bid == self.tool
            send(b, "setBackgroundColor:", None, (vp,), cgcolor(EDIT_COLOR[0], EDIT_COLOR[1], EDIT_COLOR[2], 0.85) if on else cgcolor(1, 1, 1, 0.08))
            send(b, "setForegroundColor:", None, (vp,), cgcolor(1, 1, 1, 1.0) if on else cgcolor(1, 1, 1, 0.85))

    def set_tool(self, tool):
        self._end_text()
        self.tool = tool
        no_anim(self._paint_buttons)

    def open(self, img, sel_tl, scale, verify=True):
        self.close()
        w, h = sel_tl.size.w, sel_tl.size.h
        if not self.win:
            self._build(w, h)
        self.image, self.sel_tl, self.scale = img, sel_tl, scale
        self.items = []
        self.tool = "pen"
        self.text_layer = None
        self.text_buf = ""
        self.w, self.h = w, h
        self.slack = float(math.ceil(max(0.0, self.bar_min_w - w)))  # narrow shot: centred under the wider toolbar; whole points keep the canvas integral
        self.off = (0.0, 0.0)
        self.cv = None

        def setup():
            send(self.img_layer, "setFrame:", None, (CGRect,), CGRect(CGPoint(0, 0), CGSize(w, h)))
            send(self.img_layer, "setContents:", None, (vp,), img)
            send(self.img_layer, "setContentsScale:", None, (f64,), scale)
            send(self.ann, "setFrame:", None, (CGRect,), CGRect(CGPoint(0, 0), CGSize(w, h)))
            for s in list(self._sublayers(self.ann)):
                send(s, "removeFromSuperlayer")
            self._paint_buttons()
            # opens tight around the shot; any edge can be pulled out for drawing room, never in
            W, H = w + self.slack, h + BAR_H
            send(self.win, "setContentMinSize:", None, (CGSize,), CGSize(W, H))
            send(self.win, "setContentSize:", None, (CGSize,), CGSize(W, H))
            # place it where the selection was (toolbar + title bar above), clamped to the display
            r = cocoa_rect(sel_tl)
            disp = CG.CGDisplayBounds(display_for_point(sel_tl.origin.x, sel_tl.origin.y))
            dc = cocoa_rect(disp)
            ox = min(max(r.origin.x - self.slack / 2, dc.origin.x), dc.origin.x + dc.size.w - W)
            oy = min(max(r.origin.y, dc.origin.y), dc.origin.y + dc.size.h - (H + 28))
            send(self.win, "setFrameOrigin:", None, (CGPoint,), CGPoint(ox, oy))
            self._layout()

        no_anim(setup)
        self._activate()
        self.open_ = True
        self.focused = True  # owns the keyboard until a click lands outside the window
        if verify:
            after(VERIFY_AFTER_S, self._verify_shown)

    def _verify_shown(self):
        """Same guard as the overlay: a long-lived window that the window server will not
        order in gets rebuilt once, keeping the capture."""
        if not self.open_ or window_on_screen(self.win):
            return
        log("editor window did not come on screen; rebuilding it")
        img, sel_tl, scale = self.image, self.sel_tl, self.scale
        self.image = None  # close() must not release the capture we are about to reopen
        self.close()
        send(self.win, "close")
        self.win = None
        self.open(img, sel_tl, scale, verify=False)
        after(VERIFY_AFTER_S, lambda: self.open_ and not window_on_screen(self.win) and log("editor window still not on screen after the rebuild"))

    def _layout(self):
        """Fit canvas, shot and toolbar to the current window. Across a resize the shot keeps
        its place on screen, so the room appears on the side you pulled; the window cannot
        get smaller than the shot (content min size), so the clamp only trims rounding."""
        if not self.win or not self.w:
            return
        gx, gy, cw, ch = self._canvas_tl()
        if self.cv is not None and (self.cv[2], self.cv[3]) != (cw, ch):
            self.off = (self.cv[0] + self.off[0] - gx, self.cv[1] + self.off[1] - gy)
        self.off = (min(max(self.off[0], 0.0), max(0.0, cw - self.w)), min(max(self.off[1], 0.0), max(0.0, ch - self.h)))
        self.cv = (gx, gy, cw, ch)
        ox, oy = self.off

        def apply():
            send(self.canvas, "setFrame:", None, (CGRect,), CGRect(CGPoint(self.slack / 2, 0), CGSize(cw, ch)))
            send(self.page, "setFrame:", None, (CGRect,), CGRect(CGPoint(ox, ch - oy - self.h), CGSize(self.w, self.h)))
            send(self.bar, "setFrame:", None, (CGRect,), CGRect(CGPoint(0, ch), CGSize(cw + self.slack, BAR_H)))

        no_anim(apply)

    def _activate(self):
        """Best effort: a background agent cannot always take key-window status on 26.x, so
        keyboard ownership is tracked by `focused` instead of asking AppKit."""
        send(send(cls("NSApplication"), "sharedApplication"), "activateIgnoringOtherApps:", None, (cbool,), True)
        send(self.win, "makeKeyAndOrderFront:", None, (vp,), None)

    def refocus(self):
        self.focused = True
        self._activate()

    def _sublayers(self, layer):
        subs = send(layer, "sublayers")
        n = CF.CFArrayGetCount(subs) if subs else 0
        return [CF.CFArrayGetValueAtIndex(subs, i) for i in range(n)]

    def close(self):
        if not self.open_:
            return
        self.open_ = False
        self.focused = False
        self._end_text()
        send(self.win, "orderOut:", None, (vp,), None)
        if self.image:
            CG.CGImageRelease(self.image)
            self.image = None

    # -- geometry
    def _canvas_tl(self):
        """Canvas (margin + shot, below the toolbar) as (x, y, w, h) in global top-left coords."""
        f = send(self.win, "frame", CGRect)
        cr = send(self.win, "contentRectForFrameRect:", CGRect, (CGRect,), f)
        main_h = CG.CGDisplayBounds(CG.CGMainDisplayID()).size.h
        ch = cr.size.h - BAR_H
        return cr.origin.x + self.slack / 2, main_h - cr.origin.y - ch, cr.size.w - self.slack, ch

    def _window_tl(self):
        f = send(self.win, "frame", CGRect)
        main_h = CG.CGDisplayBounds(CG.CGMainDisplayID()).size.h
        return f.origin.x, main_h - f.origin.y - f.size.h, f.size.w, f.size.h

    def in_window(self, pt):
        x, y, w, h = self._window_tl()
        return x <= pt.x <= x + w and y <= pt.y <= y + h

    def on_resize_edge(self, pt):
        """Inside the window but within EDGE of its left/right/bottom edge: AppKit's resize grip."""
        x, y, w, h = self._window_tl()
        return self.in_window(pt) and (pt.x < x + EDGE or pt.x > x + w - EDGE or pt.y > y + h - EDGE)

    def bar_hit(self, pt):
        """Toolbar button id under a global top-left point, or None."""
        f = send(self.win, "frame", CGRect)
        cr = send(self.win, "contentRectForFrameRect:", CGRect, (CGRect,), f)
        main_h = CG.CGDisplayBounds(CG.CGMainDisplayID()).size.h
        top = main_h - cr.origin.y - cr.size.h  # content top, global TL
        if not (cr.origin.x <= pt.x <= cr.origin.x + cr.size.w and top <= pt.y <= top + BAR_H):
            return None
        lx = pt.x - cr.origin.x
        for bid, _, x0, x1 in self.buttons:
            if x0 <= lx <= x1:
                return bid
        return "bar"

    def button_centers_tl(self):
        """{id: (x, y)} of each toolbar button in global top-left coords (for tests)."""
        f = send(self.win, "frame", CGRect)
        cr = send(self.win, "contentRectForFrameRect:", CGRect, (CGRect,), f)
        main_h = CG.CGDisplayBounds(CG.CGMainDisplayID()).size.h
        top = main_h - cr.origin.y - cr.size.h
        return {bid: (cr.origin.x + (x0 + x1) / 2, top + BAR_H / 2) for bid, _, x0, x1 in self.buttons}

    def to_local(self, pt):
        """Global top-left point -> shot-local coords (bottom-left origin at the shot's corner;
        outside 0..w/0..h means in the margin) or None if not on the canvas."""
        gx, gy, cw, ch = self._canvas_tl()
        x, y = pt.x - gx, pt.y - gy
        if not (0 <= x <= cw and 0 <= y <= ch):
            return None
        return CGPoint(x - self.off[0], self.h - (y - self.off[1]))

    # -- drawing primitives (all return a CALayer added to self.ann)
    def _shape(self):
        s = send(cls("CAShapeLayer"), "layer")
        send(s, "setStrokeColor:", None, (vp,), cgcolor(*EDIT_COLOR))
        send(s, "setFillColor:", None, (vp,), None)
        send(s, "setLineWidth:", None, (f64,), EDIT_STROKE)
        send(s, "setLineCap:", None, (vp,), nsstr("round"))
        send(s, "setLineJoin:", None, (vp,), nsstr("round"))
        send(s, "setContentsScale:", None, (f64,), self.scale)
        send(self.ann, "addSublayer:", None, (vp,), s)
        return s

    @staticmethod
    def _rect_path(a, b):
        p = CG.CGPathCreateMutable()
        r = CGRect(CGPoint(min(a.x, b.x), min(a.y, b.y)), CGSize(abs(b.x - a.x), abs(b.y - a.y)))
        CG.CGPathAddRoundedRect(p, None, r, 3.0, 3.0)
        return p

    @staticmethod
    def _arrow_path(a, b):
        p = CG.CGPathCreateMutable()
        CG.CGPathMoveToPoint(p, None, a.x, a.y)
        CG.CGPathAddLineToPoint(p, None, b.x, b.y)
        ang = math.atan2(b.y - a.y, b.x - a.x)
        L = max(10.0, EDIT_STROKE * 3.5)
        for d in (ang + math.radians(150), ang - math.radians(150)):
            CG.CGPathMoveToPoint(p, None, b.x, b.y)
            CG.CGPathAddLineToPoint(p, None, b.x + L * math.cos(d), b.y + L * math.sin(d))
        return p

    def _set_path(self, layer, path):
        send(layer, "setPath:", None, (vp,), path)
        CG.CGPathRelease(path)

    # -- tap-driven events (global top-left points; return True if consumed)
    def mouse_down(self, pt, flags):
        if self.on_resize_edge(pt):
            return False  # AppKit resizes; we stay focused
        hit = self.bar_hit(pt)
        lp = None if hit else self.to_local(pt)
        if lp is None and hit is None:
            if not self.in_window(pt):
                self.focused = False  # clicked into another app: it gets the keyboard back
                self._end_text()
            return False  # (else the title bar: AppKit moves the window)
        if not self.focused:
            self.focused = True
            self._activate()
        if hit:
            if hit in TOOLS:
                self.set_tool(hit)
            elif hit == "undo":
                self.undo()
            elif hit == "copy":
                self.copy()
            elif hit == "save":
                self.save()
            return True
        assert lp is not None
        self._end_text()
        self.drag_start = lp
        if self.tool == "text":
            self._begin_text(lp)
            return True
        self.live = self._shape()
        if self.tool == "pen":
            self.pen_path = CG.CGPathCreateMutable()
            CG.CGPathMoveToPoint(self.pen_path, None, lp.x, lp.y)
        elif self.tool == "pixelate":
            send(self.live, "setLineWidth:", None, (f64,), 1.0)
            send(self.live, "setLineDashPattern:", None, (vp,), send(cls("NSArray"), "arrayWithObjects:", vp, (vp, vp, vp),
                 send(cls("NSNumber"), "numberWithInt:", vp, (C.c_int,), 4), send(cls("NSNumber"), "numberWithInt:", vp, (C.c_int,), 3), None))
        return True

    def mouse_dragged(self, pt):
        if self.drag_start is None or self.live is None:
            return self.drag_start is not None
        self._update_live(self._local_clamped(pt))
        return True

    def _local_clamped(self, pt):
        gx, gy, cw, ch = self._canvas_tl()
        x, y = min(max(pt.x - gx, 0.0), cw), min(max(pt.y - gy, 0.0), ch)
        return CGPoint(x - self.off[0], self.h - (y - self.off[1]))

    def _update_live(self, b):
        a = self.drag_start

        def upd():
            if self.tool == "arrow":
                self._set_path(self.live, self._arrow_path(a, b))
            elif self.tool in ("rect", "pixelate"):
                self._set_path(self.live, self._rect_path(a, b))
            elif self.tool == "pen":
                CG.CGPathAddLineToPoint(self.pen_path, None, b.x, b.y)
                send(self.live, "setPath:", None, (vp,), self.pen_path)

        no_anim(upd)

    def mouse_up(self, pt):
        if self.drag_start is None:
            return False
        a = self.drag_start
        if self.live is None:
            self.drag_start = None
            return True
        b = self._local_clamped(pt)
        self._update_live(b)  # the release point is the final geometry, not the last drag sample
        self.drag_start = None
        tiny = abs(b.x - a.x) < 3 and abs(b.y - a.y) < 3
        layer, self.live = self.live, None
        if self.tool == "pen":
            CG.CGPathRelease(self.pen_path)
            self.pen_path = None
        if tiny:
            send(layer, "removeFromSuperlayer")
            return True
        if self.tool == "pixelate":
            send(layer, "removeFromSuperlayer")
            self._pixelate(a, b)
            return True
        self.items.append(layer)
        return True

    def _pixelate(self, a, b):
        sc = self.scale
        # only the shot has pixels to scramble: clip the drag to it
        x0, y0 = max(0.0, min(a.x, b.x)), max(0.0, min(a.y, b.y))
        x1, y1 = min(self.w, max(a.x, b.x)), min(self.h, max(a.y, b.y))
        if x1 - x0 < 1 or y1 - y0 < 1:
            return
        w, h = x1 - x0, y1 - y0
        H = self.h
        # image pixel rect: top-left origin
        px = CGRect(CGPoint(round(x0 * sc), round((H - y0 - h) * sc)), CGSize(round(w * sc), round(h * sc)))
        mosaic = imaging.pixelate(self.image, px, max(6, int(8 * sc)))
        if not mosaic:
            return
        layer = send(cls("CALayer"), "layer")
        send(layer, "setFrame:", None, (CGRect,), CGRect(CGPoint(x0, y0), CGSize(w, h)))
        send(layer, "setContents:", None, (vp,), mosaic)
        send(layer, "setContentsScale:", None, (f64,), sc)
        CG.CGImageRelease(mosaic)
        no_anim(lambda: send(self.ann, "addSublayer:", None, (vp,), layer))
        self.items.append(layer)

    def _begin_text(self, lp):
        size = max(16.0, EDIT_STROKE * 5)
        t = send(cls("CATextLayer"), "layer")
        send(t, "setFont:", None, (vp,), nsstr("Helvetica-Bold"))
        send(t, "setFontSize:", None, (f64,), size)
        send(t, "setForegroundColor:", None, (vp,), cgcolor(*EDIT_COLOR))
        send(t, "setContentsScale:", None, (f64,), self.scale)
        send(t, "setWrapped:", None, (cbool,), False)
        # a full line box, baseline near the click; CATextLayer draws from the top of its frame
        right = self._canvas_tl()[2] - self.off[0]  # canvas' right edge, shot-local
        send(t, "setFrame:", None, (CGRect,), CGRect(CGPoint(lp.x, lp.y - size * 0.3), CGSize(max(40.0, right - lp.x), size * 1.3)))
        no_anim(lambda: send(self.ann, "addSublayer:", None, (vp,), t))
        self.text_layer, self.text_buf, self.text_pos = t, "", lp
        self._render_text(caret=True)

    def _render_text(self, caret):
        if self.text_layer:
            no_anim(lambda: send(self.text_layer, "setString:", None, (vp,), nsstr(self.text_buf + ("|" if caret else ""))))

    def _end_text(self):
        if not self.text_layer:
            return
        t, self.text_layer = self.text_layer, None
        if self.text_buf.strip():
            no_anim(lambda: send(t, "setString:", None, (vp,), nsstr(self.text_buf)))
            self.items.append(t)
        else:
            send(t, "removeFromSuperlayer")
        self.text_buf = ""

    def key(self, keycode, flags, chars):
        """Returns True if consumed. Only while the editor owns the keyboard: with it open but
        the user clicked into another app, typing there must not be eaten."""
        if not self.focused:
            return False
        mods = flags & MODIFIER_MASK
        if self.text_layer is not None and mods & FLAG_CMD:
            self._end_text()  # Cmd+C while typing: commit the label, then copy
        if self.text_layer is not None:
            if keycode == KEY_ESC:
                self.text_buf = ""
                self._end_text()
            elif keycode == KEY_RETURN:
                self._end_text()
            elif keycode == KEY_DELETE:
                self.text_buf = self.text_buf[:-1]
                self._render_text(caret=True)
            elif chars:
                self.text_buf += chars
                self._render_text(caret=True)
            return True
        if mods & FLAG_CMD:
            if keycode == 8:      # C
                self.copy()
            elif keycode == 1:    # S
                self.save()
            elif keycode == 6:    # Z
                self.undo()
            elif keycode == 13:   # W
                self.close()
            else:
                return False
            return True
        if keycode == KEY_ESC:
            self.close()
            return True
        if keycode in TOOL_KEYS and not mods:
            self.set_tool(TOOL_KEYS[keycode])
            return True
        return False

    def undo(self):
        self._end_text()
        if self.items:
            no_anim(lambda: send(self.items.pop(), "removeFromSuperlayer"))

    def flattened(self):
        """The whole canvas: margin, shot and annotations, at the shot's pixel density."""
        self._end_text()
        _, _, cw, ch = self._canvas_tl()
        return no_anim(lambda: imaging.flatten(self.canvas, round(cw * self.scale), round(ch * self.scale), self.scale))

    def copy(self):
        img = self.flattened()
        png, tiff = imaging.encode(img, "public.png"), imaging.encode(img, "public.tiff")
        imaging.pasteboard_set_image(png, tiff)
        log(f"editor: copied annotated {imaging.size(img)[0]}x{imaging.size(img)[1]} ({CF.CFDataGetLength(png) // 1024} KB)")
        CF.CFRelease(png)
        CF.CFRelease(tiff)
        CG.CGImageRelease(img)
        self.close()

    def save(self):
        img = self.flattened()
        d = SAVE_DIR or os.path.expanduser("~/Desktop")
        os.makedirs(d, exist_ok=True)
        path = imaging.unique_path(d, imaging.timestamp_name())
        ok = imaging.write_png(img, path)
        CG.CGImageRelease(img)
        log(f"editor: {'saved ' + path if ok else 'FAILED to save ' + path}")
        self.close()


# --- Event tap --------------------------------------------------------------------------
def _chars(event):
    buf = (C.c_uint16 * 8)()
    n = C.c_ulong(0)
    CG.CGEventKeyboardGetUnicodeString(event, 8, C.byref(n), buf)
    s = "".join(chr(buf[i]) for i in range(n.value))
    return s if s.isprintable() else ""


def on_event(proxy, etype, event, refcon):
    if etype in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
        log(f"event tap disabled ({etype:#x}); re-enabling")
        daemon.reenable_tap()
        return event

    # --- selection in progress: we own the mouse and Esc
    if S.active:
        if etype == kCGEventLeftMouseDown:
            S.anchor = S.cur = clamp_to_frame(CG.CGEventGetLocation(event))
            S.dragging = True
            return None
        if etype == kCGEventMouseMoved:
            queue_refresh()  # keeps the crosshair asserted while hovering
            return None
        if etype == kCGEventLeftMouseDragged and S.dragging:
            S.cur = clamp_to_frame(CG.CGEventGetLocation(event))
            queue_refresh()
            return None
        if etype == kCGEventLeftMouseUp and S.dragging:
            S.dragging = False
            r = norm_rect(S.anchor, clamp_to_frame(CG.CGEventGetLocation(event)))
            if r.size.w >= 2 and r.size.h >= 2:
                finish(r)
            else:
                cancel()
            return None
        if etype == kCGEventKeyDown:
            if CG.CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) == KEY_ESC:
                cancel()
            return None  # swallow typing while selecting
        if etype == kCGEventRightMouseDown:
            cancel()
            return None
        return event

    # --- editor open: route its window's mouse + all keys
    if _EDITOR is not None and _EDITOR.open_:
        if etype == kCGEventLeftMouseDown:
            pt = CG.CGEventGetLocation(event)
            return None if main_sync(lambda: _EDITOR.mouse_down(pt, CG.CGEventGetFlags(event))) else event
        if etype == kCGEventLeftMouseDragged:
            pt = CG.CGEventGetLocation(event)
            return None if main_sync(lambda: _EDITOR.mouse_dragged(pt)) else event
        if etype == kCGEventLeftMouseUp:
            pt = CG.CGEventGetLocation(event)
            return None if main_sync(lambda: _EDITOR.mouse_up(pt)) else event
        if etype == kCGEventKeyDown:
            keycode = CG.CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
            flags = CG.CGEventGetFlags(event)
            chars = _chars(event)
            if main_sync(lambda: _EDITOR.key(keycode, flags, chars)):
                return None
            # hotkeys still work while the editor is up (fall through)

    if etype != kCGEventKeyDown:
        return event
    combo = (CG.CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode), CG.CGEventGetFlags(event) & MODIFIER_MASK)
    mode = HK.get(combo)
    if mode is None:
        return event
    if CG.CGEventGetIntegerValueField(event, 8):  # autorepeat
        return None
    if mode == "edit":
        open_last()
        return None
    if _EDITOR is not None and _EDITOR.open_:
        main_sync(_EDITOR.close)
    begin(mode)
    return None


def main():
    global _OVERLAY, _EDITOR, _MAIN_SRC, _CURSOR_TIMER, CATransaction
    if "--check-config" in sys.argv:
        print("config OK:", CFG_PATH)
        return
    daemon.wait_for_accessibility("shot")
    if not CG.CGPreflightScreenCaptureAccess():
        CG.CGRequestScreenCaptureAccess()
        log("shot: Screen Recording not granted to /usr/bin/python3 - captures will be wallpaper only until it is "
            "(System Settings > Privacy & Security > Screen & System Audio Recording)")

    app = send(cls("NSApplication"), "sharedApplication")
    send(app, "setActivationPolicy:", cbool, (i64,), 1)
    CATransaction = cls("CATransaction")
    # Let a background process change the cursor (the crosshair during selection). Private
    # connection property; without it NSCursor only applies while we are the active app.
    if SL is not None and getattr(SL, "SLSSetConnectionProperty", None):
        SL.SLSSetConnectionProperty(CID, CID, cfstr("SetsCursorInBackground"), kCFBooleanTrue)

    # a run-loop source for main-thread hops (version-0 source, signalled from the tap thread)
    class Ctx(C.Structure):
        _fields_ = [("version", C.c_long), ("info", vp), ("retain", vp), ("release", vp), ("copyDescription", vp),
                    ("equal", vp), ("hash", vp), ("schedule", vp), ("cancel", vp), ("perform", vp)]
    perform = C.CFUNCTYPE(None, vp)(_drain_main)
    ctx = Ctx(0, None, None, None, None, None, None, None, None, C.cast(perform, vp))
    _MAIN_SRC = CF.CFRunLoopSourceCreate(None, 0, C.byref(ctx))
    CF.CFRunLoopAddSource(CF.CFRunLoopGetMain(), _MAIN_SRC, kCFRunLoopCommonModes)
    main._keep = (perform, ctx)  # type: ignore[attr-defined]
    # the crosshair watchdog: parked far in the future, pulled in by each capture
    _CURSOR_TIMER = CF.CFRunLoopTimerCreate(None, CF.CFAbsoluteTimeGetCurrent() + 1e9, CURSOR_TICK_S, 0, 0, _CURSOR_CB, None)
    CF.CFRunLoopAddTimer(CF.CFRunLoopGetMain(), _CURSOR_TIMER, kCFRunLoopCommonModes)

    _OVERLAY = Overlay()
    _EDITOR = Editor()
    # First CGWindowListCreateImage in a process costs ~40 ms (pipeline setup); every later
    # one is ~1 ms. Pay it now, not on the first hotkey.
    warm = imaging.capture(CG.CGDisplayBounds(CG.CGMainDisplayID()))
    if warm:
        CG.CGImageRelease(warm)
    threading.Thread(target=lambda: trace(f"ocr warm in {imaging.warm_ocr():.0f} ms"), daemon=True).start()

    mask = ((1 << kCGEventKeyDown) | (1 << kCGEventLeftMouseDown) | (1 << kCGEventLeftMouseDragged)
            | (1 << kCGEventLeftMouseUp) | (1 << kCGEventRightMouseDown) | (1 << kCGEventMouseMoved))
    daemon.install("shot", mask, on_event,
                   ready_msg=f"shot ready: {', '.join(f'{CFG[k]}={m}' for k, m in (('area_to_clipboard', 'copy'), ('ocr_to_clipboard', 'ocr'), ('edit_last', 'edit')) if CFG[k])}"
                             f"{' save_dir=' + SAVE_DIR if SAVE_DIR else ''} (config {CFG_PATH})")
    send(app, "run")


if __name__ == "__main__":
    main()
