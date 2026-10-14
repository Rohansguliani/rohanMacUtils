"""objc: minimal Objective-C runtime bridge + the SkyLight window-focus SPI.

Used by packages that draw UI (NSPanel / CALayer) or raise windows across apps. Kept out
of macbridge so daemons that only need event taps don't pay for AppKit.
"""

import ctypes as C

from macbridge import CF, CGRect, SL, cbool, f64, i32, i64, u32, u64, vp, _sig  # noqa: F401

APPKIT = C.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
QUARTZ = C.CDLL("/System/Library/Frameworks/QuartzCore.framework/QuartzCore")
OBJC = C.CDLL("/usr/lib/libobjc.A.dylib")
_sig(OBJC, "objc_getClass", vp, [C.c_char_p])
_sig(OBJC, "sel_registerName", vp, [C.c_char_p])
_MSG_ADDR = C.cast(OBJC.objc_msgSend, vp).value or 0
_CLASSES, _SELS, _SENDERS = {}, {}, {}


def cls(name):
    c = _CLASSES.get(name)
    if c is None:
        c = OBJC.objc_getClass(name.encode())
        _CLASSES[name] = c
    return c


def sel(name):
    s = _SELS.get(name)
    if s is None:
        s = OBJC.sel_registerName(name.encode())
        _SELS[name] = s
    return s


def send(obj, selector, restype=vp, argtypes=(), *args):
    """objc_msgSend with an explicit C signature. arm64 passes/returns small structs
    (CGRect = 4 doubles) in registers, so a plain CFUNCTYPE prototype is ABI-correct."""
    if not obj:
        return None
    key = (restype, argtypes)
    fn = _SENDERS.get(key)
    if fn is None:
        fn = C.CFUNCTYPE(restype, vp, vp, *argtypes)(_MSG_ADDR)
        _SENDERS[key] = fn
    return fn(obj, sel(selector), *args)


def nsstr(s):
    return send(cls("NSString"), "stringWithUTF8String:", vp, (C.c_char_p,), s.encode("utf-8", "replace"))


kCAGravityResizeAspect = vp.in_dll(QUARTZ, "kCAGravityResizeAspect")


class ProcessSerialNumber(C.Structure):
    _fields_ = [("highLongOfPSN", u32), ("lowLongOfPSN", u32)]


# --- SkyLight focus SPI (private; AltTab/yabai depend on the same) -----------------
if SL is not None:
    _sig(SL, "SLSGetConnectionPSN", i32, [i32, C.POINTER(ProcessSerialNumber)])
    SL_SET_FRONT = _sig(SL, "_SLPSSetFrontProcessWithOptions", i32, [C.POINTER(ProcessSerialNumber), u32, u32])
    _sig(SL, "SLPSPostEventRecordTo", i32, [C.POINTER(ProcessSerialNumber), C.POINTER(C.c_uint8)])
else:
    SL_SET_FRONT = None
