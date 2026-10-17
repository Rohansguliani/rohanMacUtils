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


_sig(OBJC, "objc_allocateClassPair", vp, [vp, C.c_char_p, C.c_size_t])
_sig(OBJC, "objc_registerClassPair", None, [vp])
_sig(OBJC, "class_addMethod", C.c_bool, [vp, vp, vp, C.c_char_p])
_IMPS = []  # ctypes thunks must outlive the classes that point at them


def define_class(name, superclass, methods):
    """Create an Objective-C subclass at runtime (an NSView that gets mouseDown:, a window
    delegate, ...). `methods` maps "selector:" to (restype, argtypes, fn, type_encoding);
    `fn(self, _cmd, *args)` is a plain Python callable. Returns the Class."""
    k = OBJC.objc_allocateClassPair(cls(superclass), name.encode(), 0)
    if not k:
        return cls(name)  # already registered in this process
    for selector, (restype, argtypes, fn, enc) in methods.items():
        imp = C.CFUNCTYPE(restype, vp, vp, *argtypes)(fn)
        _IMPS.append(imp)
        OBJC.class_addMethod(k, sel(selector), C.cast(imp, vp), enc.encode())
    OBJC.objc_registerClassPair(k)
    _CLASSES[name] = k
    return k


def ns_array(objs):
    arr = send(cls("NSMutableArray"), "arrayWithCapacity:", vp, (C.c_ulong,), len(objs))
    for o in objs:
        send(arr, "addObject:", None, (vp,), o)
    return arr


class autorelease_pool:
    """`with autorelease_pool():` — drains AppKit's autoreleased objects at the end of the
    block. A background agent that owns no windows may not run an AppKit event turn for a
    long time, so without this a closed NSPanel lingers (off-screen) until one happens."""

    def __enter__(self):
        self.pool = send(send(cls("NSAutoreleasePool"), "alloc"), "init")
        return self

    def __exit__(self, *exc):
        send(self.pool, "drain")
        return False


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
