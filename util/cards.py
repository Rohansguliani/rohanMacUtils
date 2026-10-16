"""cards: the thumbnail-card HUD that alttab and snap's picker both draw.

One NSPanel with an NSVisualEffectView; each card is an NSView whose thumbnail layer takes
the CGImage from windows.get_cached_thumb() straight into CALayer.contents (no pixel copy),
so a re-render is a handful of property sets inside one CATransaction.
"""

import ctypes as C
import math

from macbridge import CF, CG, CGPoint, CGRect, CGSize, cbool, f64, i64, u32, u64, vp
from objc import autorelease_pool, cls, kCAGravityResizeAspect, nsstr, send
from windows import get_cached_thumb


# ---------------------------------------------------------------------------
# Horizontal Thumbnail Cards HUD (Direct GPU CALayer.contents IOSurface)
# ---------------------------------------------------------------------------
CARD_W = 292.0
CARD_H = 204.0
THUMB_PAD = 10.0
THUMB_W = CARD_W - THUMB_PAD * 2
THUMB_H = CARD_H - 44.0
CARD_GAP = 12.0
OUTER_PAD = 16.0


def rgba(r, g, b, a):
    return send(cls("NSColor"), "colorWithSRGBRed:green:blue:alpha:", vp, (f64, f64, f64, f64), r, g, b, a)


def cgcolor(r, g, b, a):
    return send(rgba(r, g, b, a), "CGColor")


class CardHUD:
    """Per-session thumbnail HUD: built when a switcher/picker opens (`render`) and closed +
    released when it dismisses (`hide`). Keeping zero resident NSPanels when idle prevents
    WindowServer display-reconfiguration rot and leaks zero subviews across sessions."""

    def __init__(self, max_cards=12, columns=4):
        self.max_cards, self.columns = max(1, int(max_cards)), max(1, int(columns))
        self.panel = None
        self.vfx = None
        self.cards = []
        self.frame_bl = None
        self.card_frames = []
        self.sel_bg = cgcolor(0.24, 0.52, 0.96, 0.34)
        self.sel_border = cgcolor(0.36, 0.66, 1.0, 0.95)
        self.idle_bg = cgcolor(1.0, 1.0, 1.0, 0.04)
        self.idle_border = cgcolor(1.0, 1.0, 1.0, 0.10)
        self.thumb_well_bg = cgcolor(0.0, 0.0, 0.0, 0.36)

    def _build(self):
        init_frame = CGRect(CGPoint(0, 0), CGSize(640, 240))
        panel = send(cls("NSPanel"), "alloc")
        panel = send(panel, "initWithContentRect:styleMask:backing:defer:", vp, (CGRect, u64, u64, cbool), init_frame, 1 << 7, 2, False)
        send(panel, "setFloatingPanel:", None, (cbool,), True)
        send(panel, "setLevel:", None, (i64,), 101)  # NSPopUpMenuWindowLevel
        send(panel, "setCollectionBehavior:", None, (u64,), 1 | 16 | 64 | 256)
        send(panel, "setOpaque:", None, (cbool,), False)
        send(panel, "setBackgroundColor:", None, (vp,), send(cls("NSColor"), "clearColor"))
        send(panel, "setHasShadow:", None, (cbool,), True)
        send(panel, "setReleasedWhenClosed:", None, (cbool,), True)
        self.panel = panel

        vfx = send(cls("NSVisualEffectView"), "alloc")
        vfx = send(vfx, "initWithFrame:", vp, (CGRect,), init_frame)
        send(vfx, "setMaterial:", None, (i64,), 13)  # HUDWindow vibrancy
        send(vfx, "setBlendingMode:", None, (i64,), 0)
        send(vfx, "setState:", None, (i64,), 1)
        send(vfx, "setWantsLayer:", None, (cbool,), True)
        vlayer = send(vfx, "layer")
        send(vlayer, "setCornerRadius:", None, (f64,), 18.0)
        send(vlayer, "setMasksToBounds:", None, (cbool,), True)
        send(vlayer, "setBorderWidth:", None, (f64,), 1.0)
        send(vlayer, "setBorderColor:", None, (vp,), cgcolor(1.0, 1.0, 1.0, 0.16))
        send(panel, "setContentView:", None, (vp,), vfx)
        send(vfx, "release")
        self.vfx = vfx

        title_font = send(cls("NSFont"), "systemFontOfSize:weight:", vp, (f64, f64), 13.0, 0.35)
        self.cards = []

        for _ in range(self.max_cards):
            card = send(cls("NSView"), "alloc")
            card = send(card, "initWithFrame:", vp, (CGRect,), CGRect(CGPoint(0, 0), CGSize(CARD_W, CARD_H)))
            send(card, "setWantsLayer:", None, (cbool,), True)
            clayer = send(card, "layer")
            send(clayer, "setCornerRadius:", None, (f64,), 12.0)

            # Header icon (20x20) + title label
            iv = send(cls("NSImageView"), "alloc")
            iv = send(iv, "initWithFrame:", vp, (CGRect,), CGRect(CGPoint(THUMB_PAD, CARD_H - 31), CGSize(20, 20)))
            send(iv, "setImageScaling:", None, (u64,), 3)
            send(card, "addSubview:", None, (vp,), iv)
            send(iv, "release")

            tf = send(cls("NSTextField"), "labelWithString:", vp, (vp,), nsstr(""))
            send(tf, "setFrame:", None, (CGRect,), CGRect(CGPoint(THUMB_PAD + 26, CARD_H - 30), CGSize(CARD_W - THUMB_PAD * 2 - 26, 18)))
            send(tf, "setFont:", None, (vp,), title_font)
            send(tf, "setTextColor:", None, (vp,), rgba(1.0, 1.0, 1.0, 0.96))
            send(tf, "setLineBreakMode:", None, (u64,), 4)
            send(card, "addSubview:", None, (vp,), tf)

            # GPU IOSurface thumbnail layer view
            tv = send(cls("NSView"), "alloc")
            tv = send(tv, "initWithFrame:", vp, (CGRect,), CGRect(CGPoint(THUMB_PAD, THUMB_PAD), CGSize(THUMB_W, THUMB_H)))
            send(tv, "setWantsLayer:", None, (cbool,), True)
            tlayer = send(tv, "layer")
            send(tlayer, "setCornerRadius:", None, (f64,), 8.0)
            send(tlayer, "setMasksToBounds:", None, (cbool,), True)
            send(tlayer, "setBackgroundColor:", None, (vp,), self.thumb_well_bg)
            send(tlayer, "setContentsGravity:", None, (vp,), kCAGravityResizeAspect)
            send(card, "addSubview:", None, (vp,), tv)
            send(tv, "release")

            send(vfx, "addSubview:", None, (vp,), card)
            send(card, "release")
            self.cards.append((card, clayer, iv, tf, tlayer))

    def render(self, windows, selected_idx: int, reposition: bool = False, anchor=None):
        total = min(len(windows), self.max_cards)
        if total == 0:
            self.hide()
            return
        if self.panel is None:
            self._build()
            reposition = True
        max_cols = self.columns
        if anchor is not None:
            max_cols = max(1, min(self.columns, int((anchor.size.w - OUTER_PAD * 2 + CARD_GAP) // (CARD_W + CARD_GAP))))
        cols = min(total, max_cols)
        rows = math.ceil(total / cols)
        panel_w = OUTER_PAD * 2 + cols * CARD_W + (cols - 1) * CARD_GAP
        panel_h = OUTER_PAD * 2 + rows * CARD_H + (rows - 1) * CARD_GAP

        if reposition:
            main_h = CG.CGDisplayBounds(0).size.h
            if anchor is not None:
                # Centre inside the anchor rect (top-left coords -> Cocoa bottom-left)
                ox = anchor.origin.x + (anchor.size.w - panel_w) / 2.0
                oy = (main_h - anchor.origin.y - anchor.size.h) + (anchor.size.h - panel_h) / 2.0
            else:
                ev = CG.CGEventCreate(None)
                pt = CG.CGEventGetLocation(ev) if ev else CGPoint(960, 540)
                if ev:
                    CF.CFRelease(ev)
                did, cnt = u32(0), u32(0)
                CG.CGGetDisplaysWithPoint(pt, 1, C.byref(did), C.byref(cnt))
                bounds = CG.CGDisplayBounds(did.value if cnt.value else 0)
                ox = bounds.origin.x + (bounds.size.w - panel_w) / 2.0
                oy = (main_h - (bounds.origin.y + bounds.size.h)) + (bounds.size.h - panel_h) / 2.0 + bounds.size.h * 0.03
            rect = CGRect(CGPoint(ox, oy), CGSize(panel_w, panel_h))
            self.frame_bl = rect
            self.card_frames = []
            send(self.panel, "setFrame:display:", None, (CGRect, cbool), rect, False)
            send(self.vfx, "setFrame:", None, (CGRect,), CGRect(CGPoint(0, 0), CGSize(panel_w, panel_h)))

        send(cls("CATransaction"), "begin")
        send(cls("CATransaction"), "setDisableActions:", None, (cbool,), True)

        for idx in range(self.max_cards):
            card, clayer, iv, tf, tlayer = self.cards[idx]
            if idx >= total:
                send(card, "setHidden:", None, (cbool,), True)
                continue
            w = windows[idx]
            is_sel = idx == selected_idx
            if reposition:
                r = idx // cols
                c = idx % cols
                row_cols = min(cols, total - r * cols)
                row_w = row_cols * CARD_W + (row_cols - 1) * CARD_GAP
                start_x = (panel_w - row_w) / 2.0
                cx = start_x + c * (CARD_W + CARD_GAP)
                cy = panel_h - OUTER_PAD - (r + 1) * CARD_H - r * CARD_GAP
                self.card_frames.append(CGRect(CGPoint(cx, cy), CGSize(CARD_W, CARD_H)))
                send(card, "setFrame:", None, (CGRect,), CGRect(CGPoint(cx, cy), CGSize(CARD_W, CARD_H)))
                send(card, "setHidden:", None, (cbool,), False)
                send(iv, "setImage:", None, (vp,), w["icon"])
                send(tf, "setStringValue:", None, (vp,), nsstr(w["title"]))
                send(tlayer, "setContents:", None, (vp,), get_cached_thumb(w["wid"]))
            send(clayer, "setBackgroundColor:", None, (vp,), self.sel_bg if is_sel else self.idle_bg)
            send(clayer, "setBorderWidth:", None, (f64,), 2.2 if is_sel else 1.0)
            send(clayer, "setBorderColor:", None, (vp,), self.sel_border if is_sel else self.idle_border)

        send(cls("CATransaction"), "commit")
        if reposition:
            send(self.panel, "orderFrontRegardless")
        send(cls("CATransaction"), "flush")

    def refresh_thumbnails_in_place(self, windows):
        if self.panel is None:
            return
        total = min(len(windows), self.max_cards)
        send(cls("CATransaction"), "begin")
        send(cls("CATransaction"), "setDisableActions:", None, (cbool,), True)
        for idx in range(total):
            _, _, _, _, tlayer = self.cards[idx]
            img = get_cached_thumb(windows[idx]["wid"])
            if img:
                send(tlayer, "setContents:", None, (vp,), img)
        send(cls("CATransaction"), "commit")

    def hide(self):
        panel, self.panel = self.panel, None
        self.cards = []
        self.vfx = None
        self.frame_bl = None
        self.card_frames = []
        if panel is None:
            return
        with autorelease_pool():
            send(cls("CATransaction"), "begin")
            send(cls("CATransaction"), "setDisableActions:", None, (cbool,), True)
            send(panel, "orderOut:", None, (vp,), None)
            send(cls("CATransaction"), "commit")
            send(cls("CATransaction"), "flush")
            send(panel, "close")

    def card_at(self, pt_tl: CGPoint):
        """Card index under a top-left-coords point; -1 = on panel but no card; None = outside."""
        fr = getattr(self, "frame_bl", None)
        if fr is None:
            return None
        main_h = CG.CGDisplayBounds(0).size.h
        px, py = pt_tl.x - fr.origin.x, (main_h - pt_tl.y) - fr.origin.y
        if not (0 <= px <= fr.size.w and 0 <= py <= fr.size.h):
            return None
        for i, c in enumerate(getattr(self, "card_frames", [])):
            if c.origin.x <= px <= c.origin.x + c.size.w and c.origin.y <= py <= c.origin.y + c.size.h:
                return i
        return -1
