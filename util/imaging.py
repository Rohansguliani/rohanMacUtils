"""imaging: screen capture, cropping, PNG/TIFF encoding, pasteboard, Vision OCR, pixelate,
and flattening a CALayer tree to a bitmap. CoreGraphics / ImageIO / Vision via ctypes.

Capture uses CGWindowListCreateImage (deprecated since macOS 14 in favour of
ScreenCaptureKit, still present and ~1 ms warm on 26.x; ScreenCaptureKit is
async/Swift-shaped and not reachable from ctypes without a helper binary). It needs the
Screen Recording permission for /usr/bin/python3 — without it the image contains only
the wallpaper.
"""

from __future__ import annotations

import ctypes as C
import math
import os
import time

from macbridge import CF, CG, CGPoint, CGRect, CGSize, cbool, cf_to_str, cfstr, f64, i32, u32, vp, _sig
from objc import cls, ns_array, nsstr, sel, send

IIO = C.CDLL("/System/Library/Frameworks/ImageIO.framework/ImageIO")
C.CDLL("/System/Library/Frameworks/Vision.framework/Vision")

_sig(CG, "CGWindowListCreateImage", vp, [CGRect, u32, u32, u32])
_sig(CG, "CGImageCreateWithImageInRect", vp, [vp, CGRect])
_sig(CG, "CGImageGetWidth", C.c_size_t, [vp])
_sig(CG, "CGImageGetHeight", C.c_size_t, [vp])
_sig(CG, "CGImageRelease", None, [vp])
_sig(CG, "CGImageRetain", vp, [vp])
_sig(CG, "CGColorSpaceCreateDeviceRGB", vp, [])
_sig(CG, "CGBitmapContextCreate", vp, [vp, C.c_size_t, C.c_size_t, C.c_size_t, C.c_size_t, vp, u32])
_sig(CG, "CGBitmapContextCreateImage", vp, [vp])
_sig(CG, "CGContextRelease", None, [vp])
_sig(CG, "CGContextScaleCTM", None, [vp, f64, f64])
_sig(CG, "CGContextDrawImage", None, [vp, CGRect, vp])
_sig(CG, "CGContextSetInterpolationQuality", None, [vp, i32])
_sig(CG, "CGContextSetRGBFillColor", None, [vp, f64, f64, f64, f64])
_sig(CG, "CGContextFillRect", None, [vp, CGRect])
_sig(IIO, "CGImageDestinationCreateWithData", vp, [vp, vp, C.c_size_t, vp])
_sig(IIO, "CGImageDestinationCreateWithURL", vp, [vp, vp, C.c_size_t, vp])
_sig(IIO, "CGImageDestinationAddImage", None, [vp, vp, vp])
_sig(IIO, "CGImageDestinationFinalize", cbool, [vp])
_sig(CF, "CFDataCreateMutable", vp, [vp, C.c_long])
_sig(CF, "CFURLCreateWithFileSystemPath", vp, [vp, vp, C.c_long, cbool])

kCGWindowListOptionOnScreenOnly = 1
kCGWindowListOptionOnScreenBelowWindow = 4
kCGWindowImageBestResolution = 8
_RGB = CG.CGColorSpaceCreateDeviceRGB()
_BITMAP_INFO = 0x2002  # kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Little (BGRA, what the GPU likes)


def capture(rect_tl: CGRect, below_window: int = 0):
    """Pixels of `rect_tl` (global top-left points) at the display's native scale. With
    `below_window`, only windows beneath that window number are included (used to leave
    the selection overlay out of its own shot). Returns a CGImageRef or None."""
    if below_window:
        img = CG.CGWindowListCreateImage(rect_tl, kCGWindowListOptionOnScreenBelowWindow, below_window, kCGWindowImageBestResolution)
    else:
        img = CG.CGWindowListCreateImage(rect_tl, kCGWindowListOptionOnScreenOnly, 0, kCGWindowImageBestResolution)
    if img and CG.CGImageGetWidth(img) == 0:
        CG.CGImageRelease(img)
        return None
    return img


def size(img):
    return CG.CGImageGetWidth(img), CG.CGImageGetHeight(img)


def crop(img, rect_px: CGRect):
    return CG.CGImageCreateWithImageInRect(img, rect_px)


def encode(img, uti="public.png"):
    """CFDataRef with the encoded image. Caller releases."""
    data = CF.CFDataCreateMutable(None, 0)
    dst = IIO.CGImageDestinationCreateWithData(data, cfstr(uti), 1, None)
    IIO.CGImageDestinationAddImage(dst, img, None)
    ok = IIO.CGImageDestinationFinalize(dst)
    CF.CFRelease(dst)
    if not ok:
        CF.CFRelease(data)
        return None
    return data


def write_png(img, path):
    url = CF.CFURLCreateWithFileSystemPath(None, cfstr(path), 0, False)
    dst = IIO.CGImageDestinationCreateWithURL(url, cfstr("public.png"), 1, None)
    CF.CFRelease(url)
    if not dst:
        return False
    IIO.CGImageDestinationAddImage(dst, img, None)
    ok = IIO.CGImageDestinationFinalize(dst)
    CF.CFRelease(dst)
    return bool(ok)


def pasteboard_set_image(png_data, tiff_data=None):
    """Main thread. Both flavours so every paste target (browsers want PNG, older Cocoa
    apps want TIFF) finds one it likes."""
    pb = send(cls("NSPasteboard"), "generalPasteboard")
    send(pb, "clearContents", C.c_long)
    ok = send(pb, "setData:forType:", cbool, (vp, vp), png_data, nsstr("public.png"))
    if tiff_data:
        send(pb, "setData:forType:", cbool, (vp, vp), tiff_data, nsstr("public.tiff"))
    return bool(ok)


def pasteboard_set_text(text):
    pb = send(cls("NSPasteboard"), "generalPasteboard")
    send(pb, "clearContents", C.c_long)
    return bool(send(pb, "setString:forType:", cbool, (vp, vp), nsstr(text), nsstr("public.utf8-plain-text")))


# --- Vision OCR --------------------------------------------------------------------
def ocr(img, languages=("en-US",), auto_language=False):
    """Recognised text as a list of lines, top to bottom (left to right within a line).
    Safe to call from a background thread. First call in a process costs ~0.5 s of model
    loading — call warm_ocr() early. Automatic language detection is off by default: on a
    short snippet one stray glyph (an x, a bullet) can flip the whole result to another
    script."""
    handler = send(send(cls("VNImageRequestHandler"), "alloc"), "initWithCGImage:options:", vp, (vp, vp),
                   img, send(cls("NSDictionary"), "dictionary"))
    req = send(send(cls("VNRecognizeTextRequest"), "alloc"), "init")
    send(req, "setRecognitionLevel:", None, (C.c_long,), 0)  # accurate
    send(req, "setUsesLanguageCorrection:", None, (cbool,), True)
    if languages:
        send(req, "setRecognitionLanguages:", None, (vp,), ns_array([nsstr(l) for l in languages]))
    if auto_language and send(req, "respondsToSelector:", cbool, (vp,), sel("setAutomaticallyDetectsLanguage:")):
        send(req, "setAutomaticallyDetectsLanguage:", None, (cbool,), True)
    ok = send(handler, "performRequests:error:", cbool, (vp, vp), ns_array([req]), None)
    lines = []
    if ok:
        results = send(req, "results")
        n = CF.CFArrayGetCount(results) if results else 0
        obs = []
        for i in range(n):
            o = CF.CFArrayGetValueAtIndex(results, i)
            cands = send(o, "topCandidates:", vp, (C.c_ulong,), 1)
            if not cands or CF.CFArrayGetCount(cands) == 0:
                continue
            text = cf_to_str(send(CF.CFArrayGetValueAtIndex(cands, 0), "string"), size=4096)
            bb = send(o, "boundingBox", CGRect)  # normalised, origin bottom-left
            obs.append((1.0 - bb.origin.y - bb.size.h, bb.origin.x, bb.size.h, text))
        lines = _reading_order(obs)
    send(req, "release")
    send(handler, "release")
    return lines


def _reading_order(obs):
    """Group observations into visual lines (vertical centres within half a line height),
    sort each line left to right. Vision mostly returns whole lines already; this fixes
    the cases where it splits one line into pieces."""
    obs.sort(key=lambda o: o[0] + o[2] / 2)
    lines, cur, cur_cy = [], [], None
    for top, x, h, text in obs:
        cy = top + h / 2
        if cur and abs(cy - cur_cy) > max(h, cur[0][2]) * 0.5:
            lines.append(cur)
            cur = []
        if not cur:
            cur_cy = cy
        cur.append((top, x, h, text))
    if cur:
        lines.append(cur)
    return ["  ".join(t for _, _, _, t in sorted(line, key=lambda o: o[1])) for line in lines]


def warm_ocr():
    ctx = CG.CGBitmapContextCreate(None, 64, 32, 8, 0, _RGB, _BITMAP_INFO)
    CG.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
    CG.CGContextFillRect(ctx, CGRect(CGPoint(0, 0), CGSize(64, 32)))
    img = CG.CGBitmapContextCreateImage(ctx)
    CG.CGContextRelease(ctx)
    t0 = time.perf_counter()
    ocr(img)
    CG.CGImageRelease(img)
    return (time.perf_counter() - t0) * 1e3


# --- pixel work ----------------------------------------------------------------------
def pixelate(img, rect_px: CGRect, block_px: int):
    """A mosaic of `rect_px` of `img` (top-left pixel coords), same size as the rect."""
    sub = CG.CGImageCreateWithImageInRect(img, rect_px)
    if not sub:
        return None
    w, h = int(rect_px.size.w), int(rect_px.size.h)
    sw, sh = max(1, math.ceil(w / block_px)), max(1, math.ceil(h / block_px))
    small = CG.CGBitmapContextCreate(None, sw, sh, 8, 0, _RGB, _BITMAP_INFO)
    CG.CGContextSetInterpolationQuality(small, 3)  # high: average the block
    CG.CGContextDrawImage(small, CGRect(CGPoint(0, 0), CGSize(sw, sh)), sub)
    small_img = CG.CGBitmapContextCreateImage(small)
    CG.CGContextRelease(small)
    CG.CGImageRelease(sub)
    big = CG.CGBitmapContextCreate(None, w, h, 8, 0, _RGB, _BITMAP_INFO)
    CG.CGContextSetInterpolationQuality(big, 1)  # none: hard-edged blocks
    CG.CGContextDrawImage(big, CGRect(CGPoint(0, 0), CGSize(w, h)), small_img)
    out = CG.CGBitmapContextCreateImage(big)
    CG.CGContextRelease(big)
    CG.CGImageRelease(small_img)
    return out


def flatten(layer, w_px: int, h_px: int, scale: float):
    """Render a CALayer tree (whose coordinates are points) into a w_px x h_px bitmap.
    Main thread only; the receiver's own transform is not applied, sublayers' are."""
    ctx = CG.CGBitmapContextCreate(None, w_px, h_px, 8, 0, _RGB, _BITMAP_INFO)
    CG.CGContextScaleCTM(ctx, scale, scale)
    send(layer, "renderInContext:", None, (vp,), ctx)
    out = CG.CGBitmapContextCreateImage(ctx)
    CG.CGContextRelease(ctx)
    return out


def timestamp_name(prefix="Screenshot"):
    return time.strftime(f"{prefix} %Y-%m-%d at %H.%M.%S.png")


def unique_path(directory, name):
    base, ext = os.path.splitext(name)
    path = os.path.join(directory, name)
    n = 2
    while os.path.exists(path):
        path = os.path.join(directory, f"{base} ({n}){ext}")
        n += 1
    return path
