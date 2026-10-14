"""macbridge: ctypes bindings for CoreFoundation / CoreGraphics / HIServices / SkyLight.

Shared by every mactools daemon. Pure Apple frameworks via Apple's /usr/bin/python3 —
no third-party binaries, so nothing here needs codesigning.

Everything private (SkyLight `CGS*` / `SLS*`) is loaded defensively: a missing symbol
sets the attribute to None instead of raising, so each daemon can degrade.
"""

import ctypes as C
import sys
import time

# --- frameworks --------------------------------------------------------------
CF = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
CG = C.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
AX = C.CDLL("/System/Library/Frameworks/ApplicationServices.framework/Frameworks/HIServices.framework/HIServices")
CS = C.CDLL("/System/Library/Frameworks/ColorSync.framework/ColorSync")
LIBSYS = C.CDLL("/usr/lib/libSystem.B.dylib")
try:
    SL = C.CDLL("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight")
except OSError:
    SL = None

vp = C.c_void_p
i32, i64, u32, u64 = C.c_int32, C.c_int64, C.c_uint32, C.c_uint64
f32, f64, cbool = C.c_float, C.c_double, C.c_bool


class CGPoint(C.Structure):
    _fields_ = [("x", f64), ("y", f64)]


class CGSize(C.Structure):
    _fields_ = [("w", f64), ("h", f64)]


class CGRect(C.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]

    def tuple(self):
        return (self.origin.x, self.origin.y, self.size.w, self.size.h)

    @classmethod
    def make(cls, x, y, w, h):
        return cls(CGPoint(x, y), CGSize(w, h))


def _sig(lib, name, restype, argtypes):
    fn = getattr(lib, name, None)
    if fn is None:
        return None
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


# --- CoreFoundation ------------------------------------------------------------
_sig(CF, "CFRetain", vp, [vp])
_sig(CF, "CFRelease", None, [vp])
_sig(CF, "CFEqual", cbool, [vp, vp])
_sig(CF, "CFGetTypeID", C.c_ulong, [vp])
_sig(CF, "CFNumberGetTypeID", C.c_ulong, [])
_sig(CF, "CFStringCreateWithCString", vp, [vp, C.c_char_p, u32])
_sig(CF, "CFStringGetCString", cbool, [vp, C.c_char_p, C.c_long, u32])
_sig(CF, "CFNumberCreate", vp, [vp, C.c_int, vp])
_sig(CF, "CFNumberGetValue", cbool, [vp, C.c_int, vp])
_sig(CF, "CFArrayCreate", vp, [vp, C.POINTER(vp), C.c_long, vp])
_sig(CF, "CFArrayGetCount", C.c_long, [vp])
_sig(CF, "CFArrayGetValueAtIndex", vp, [vp, C.c_long])
_sig(CF, "CFDictionaryCreate", vp, [vp, C.POINTER(vp), C.POINTER(vp), C.c_long, vp, vp])
_sig(CF, "CFDictionaryGetValue", vp, [vp, vp])
_sig(CF, "CFSetGetCount", C.c_long, [vp])
_sig(CF, "CFSetGetValues", None, [vp, C.POINTER(vp)])
_sig(CF, "CFDataGetLength", C.c_long, [vp])
_sig(CF, "CFDataGetBytePtr", vp, [vp])
_sig(CF, "CFDataCreate", vp, [vp, C.c_char_p, C.c_long])
_sig(CF, "CFPreferencesCopyAppValue", vp, [vp, vp])
_sig(CF, "CFMachPortCreateRunLoopSource", vp, [vp, vp, C.c_long])
_sig(CF, "CFMachPortIsValid", cbool, [vp])
_sig(CF, "CFMachPortInvalidate", None, [vp])
_sig(CF, "CFRunLoopGetCurrent", vp, [])
_sig(CF, "CFRunLoopGetMain", vp, [])
_sig(CF, "CFRunLoopRun", None, [])
_sig(CF, "CFRunLoopRunInMode", i32, [vp, f64, cbool])
_sig(CF, "CFRunLoopWakeUp", None, [vp])
_sig(CF, "CFRunLoopAddSource", None, [vp, vp, vp])
_sig(CF, "CFRunLoopRemoveSource", None, [vp, vp, vp])
_sig(CF, "CFRunLoopAddTimer", None, [vp, vp, vp])
_sig(CF, "CFAbsoluteTimeGetCurrent", f64, [])
_sig(CF, "CFUUIDCreateString", vp, [vp, vp])
TIMER_CB = C.CFUNCTYPE(None, vp, vp)
_sig(CF, "CFRunLoopTimerCreate", vp, [vp, f64, f64, C.c_ulong, C.c_long, TIMER_CB, vp])
_sig(CF, "CFRunLoopTimerSetNextFireDate", None, [vp, f64])
_sig(CF, "CFRunLoopSourceCreate", vp, [vp, C.c_long, vp])
_sig(CF, "CFRunLoopSourceSignal", None, [vp])

kCFStringEncodingUTF8 = 0x08000100
kCFNumberSInt32Type = 3
kCFNumberSInt64Type = 4
kCFRunLoopCommonModes = vp.in_dll(CF, "kCFRunLoopCommonModes")
kCFTypeArrayCallBacks = vp.in_dll(CF, "kCFTypeArrayCallBacks")
kCFTypeDictionaryKeyCallBacks = C.addressof(vp.in_dll(CF, "kCFTypeDictionaryKeyCallBacks"))
kCFTypeDictionaryValueCallBacks = C.addressof(vp.in_dll(CF, "kCFTypeDictionaryValueCallBacks"))
kCFBooleanTrue = vp.in_dll(CF, "kCFBooleanTrue").value
kCFBooleanFalse = vp.in_dll(CF, "kCFBooleanFalse").value
kCFPreferencesAnyApplication = vp.in_dll(CF, "kCFPreferencesAnyApplication")

# --- CoreGraphics --------------------------------------------------------------
TAP_CB = C.CFUNCTYPE(vp, vp, u32, vp, vp)
_sig(CG, "CGEventTapCreate", vp, [u32, u32, u32, u64, TAP_CB, vp])
_sig(CG, "CGEventTapEnable", None, [vp, cbool])
_sig(CG, "CGEventTapIsEnabled", cbool, [vp])
_sig(CG, "CGEventTapPostEvent", None, [vp, vp])
_sig(CG, "CGEventCreate", vp, [vp])
_sig(CG, "CGEventCreateCopy", vp, [vp])
_sig(CG, "CGEventCreateData", vp, [vp, vp])
_sig(CG, "CGEventCreateFromData", vp, [vp, vp])
_sig(CG, "CGEventCreateKeyboardEvent", vp, [vp, C.c_uint16, cbool])
_sig(CG, "CGEventCreateMouseEvent", vp, [vp, u32, CGPoint, u32])
_sig(CG, "CGEventCreateScrollWheelEvent", vp, [vp, u32, u32, i32])
_sig(CG, "CGEventPost", None, [u32, vp])
_sig(CG, "CGEventSetType", None, [vp, u32])
_sig(CG, "CGEventGetType", u32, [vp])
_sig(CG, "CGEventGetFlags", u64, [vp])
_sig(CG, "CGEventSetFlags", None, [vp, u64])
_sig(CG, "CGEventGetLocation", CGPoint, [vp])
_sig(CG, "CGEventGetTimestamp", u64, [vp])
_sig(CG, "CGEventGetIntegerValueField", i64, [vp, u32])
_sig(CG, "CGEventKeyboardGetUnicodeString", None, [vp, C.c_ulong, C.POINTER(C.c_ulong), C.POINTER(C.c_uint16)])
_sig(CG, "CGEventSetIntegerValueField", None, [vp, u32, i64])
_sig(CG, "CGEventGetDoubleValueField", f64, [vp, u32])
_sig(CG, "CGEventSetDoubleValueField", None, [vp, u32, f64])
_sig(CG, "CGGetDisplaysWithPoint", i32, [CGPoint, u32, C.POINTER(u32), C.POINTER(u32)])
_sig(CG, "CGDisplayBounds", CGRect, [u32])
_sig(CG, "CGMainDisplayID", u32, [])
_sig(CG, "CGGetActiveDisplayList", i32, [u32, C.POINTER(u32), C.POINTER(u32)])
_sig(CG, "CGDisplayPixelsWide", C.c_size_t, [u32])
_sig(CG, "CGDisplayPixelsHigh", C.c_size_t, [u32])
_sig(CG, "CGDisplayHideCursor", i32, [u32])
_sig(CG, "CGDisplayShowCursor", i32, [u32])
_sig(CG, "CGPreflightScreenCaptureAccess", cbool, [])
_sig(CG, "CGRequestScreenCaptureAccess", cbool, [])
_sig(CG, "CGPathCreateMutable", vp, [])
_sig(CG, "CGPathCreateWithRect", vp, [CGRect, vp])
_sig(CG, "CGPathRelease", None, [vp])
_sig(CG, "CGPathMoveToPoint", None, [vp, vp, f64, f64])
_sig(CG, "CGPathAddLineToPoint", None, [vp, vp, f64, f64])
_sig(CG, "CGPathAddRect", None, [vp, vp, CGRect])
_sig(CG, "CGPathAddRoundedRect", None, [vp, vp, CGRect, f64, f64])
_sig(CG, "CGWindowListCopyWindowInfo", vp, [u32, u32])
_sig(CG, "CGImageGetWidth", C.c_size_t, [vp])
_sig(CS, "CGDisplayCreateUUIDFromDisplayID", vp, [u32])
_sig(LIBSYS, "mach_absolute_time", u64, [])

kCGSessionEventTap = 1
kCGHeadInsertEventTap = 0
kCGEventTapOptionDefault = 0
kCGEventTapOptionListenOnly = 1
kCGEventLeftMouseDown, kCGEventLeftMouseUp = 1, 2
kCGEventRightMouseDown, kCGEventRightMouseUp = 3, 4
kCGEventMouseMoved = 5
kCGEventLeftMouseDragged = 6
kCGEventKeyDown, kCGEventKeyUp, kCGEventFlagsChanged = 10, 11, 12
kCGEventScrollWheel = 22
kCGEventTapDisabledByTimeout = 0xFFFFFFFE
kCGEventTapDisabledByUserInput = 0xFFFFFFFF
kCGKeyboardEventAutorepeat = 8
kCGKeyboardEventKeycode = 9
kCGScrollWheelEventDeltaAxis1 = 11
kCGScrollWheelEventDeltaAxis2 = 12
kCGScrollWheelEventIsContinuous = 88
kCGEventSourceUnixProcessID = 41
kCGEventSourceUserData = 42
kCGScrollEventUnitLine = 1
kCGWindowListOptionOnScreenOnly = 1
kCGWindowListExcludeDesktopElements = 16
K_WIN_LAYER = vp.in_dll(CG, "kCGWindowLayer").value
K_WIN_OWNER = vp.in_dll(CG, "kCGWindowOwnerName").value
K_WIN_OWNER_PID = vp.in_dll(CG, "kCGWindowOwnerPID").value
K_WIN_BOUNDS = vp.in_dll(CG, "kCGWindowBounds").value
K_WIN_NUMBER = vp.in_dll(CG, "kCGWindowNumber").value
K_WIN_ALPHA = vp.in_dll(CG, "kCGWindowAlpha").value
_sig(CG, "CGRectMakeWithDictionaryRepresentation", cbool, [vp, C.POINTER(CGRect)])

FLAG_SHIFT, FLAG_CTRL, FLAG_OPT, FLAG_CMD = 0x20000, 0x40000, 0x80000, 0x100000
MODIFIER_MASK = FLAG_SHIFT | FLAG_CTRL | FLAG_OPT | FLAG_CMD
FLAG_NAMES = {"shift": FLAG_SHIFT, "ctrl": FLAG_CTRL, "control": FLAG_CTRL, "opt": FLAG_OPT,
              "option": FLAG_OPT, "alt": FLAG_OPT, "cmd": FLAG_CMD, "command": FLAG_CMD}

# --- HIServices (Accessibility) -----------------------------------------------
_sig(AX, "AXIsProcessTrusted", cbool, [])
_sig(AX, "AXIsProcessTrustedWithOptions", cbool, [vp])
_sig(AX, "AXUIElementCreateApplication", vp, [i32])
_sig(AX, "AXUIElementCopyAttributeValue", i32, [vp, vp, C.POINTER(vp)])
_sig(AX, "AXUIElementSetAttributeValue", i32, [vp, vp, vp])
_sig(AX, "AXUIElementPerformAction", i32, [vp, vp])
_sig(AX, "AXUIElementSetMessagingTimeout", i32, [vp, f32])
_sig(AX, "AXValueCreate", vp, [i32, vp])
_sig(AX, "AXValueGetValue", cbool, [vp, i32, vp])
AX_GET_WINDOW = _sig(AX, "_AXUIElementGetWindow", i32, [vp, C.POINTER(u32)])
kAXValueTypeCGPoint, kAXValueTypeCGSize = 1, 2
kAXErrorInvalidUIElement = -25202

# --- SkyLight (private; every symbol optional) --------------------------------
if SL is not None:
    _sig(SL, "CGSMainConnectionID", u32, [])
    _sig(SL, "CGSGetActiveSpace", u64, [u32])
    _sig(SL, "CGSCopyManagedDisplaySpaces", vp, [u32])
    _sig(SL, "SLSCopySpaces", vp, [u32, i32])
    _sig(SL, "SLSSpaceCopyName", vp, [u32, u64])
    _sig(SL, "CGSCopyWindowsWithOptionsAndTags", vp, [u32, i32, vp, u32, C.POINTER(u64), C.POINTER(u64)])
    _sig(SL, "SLSWindowQueryWindows", vp, [u32, vp, i32])
    _sig(SL, "SLSWindowQueryResultCopyWindows", vp, [vp])
    _sig(SL, "SLSWindowIteratorAdvance", cbool, [vp])
    _sig(SL, "SLSWindowIteratorGetWindowID", u32, [vp])
    _sig(SL, "SLSWindowIteratorGetPID", i32, [vp])
    _sig(SL, "SLSWindowIteratorGetLevel", i32, [vp])
    _sig(SL, "SLSWindowIteratorGetParentID", u32, [vp])
    _sig(SL, "SLSWindowIteratorGetAlpha", f32, [vp])
    _sig(SL, "SLSWindowIteratorCopyTitle", vp, [vp])
    _sig(SL, "SLSGetWindowBounds", i32, [u32, u32, C.POINTER(CGRect)])
    _sig(SL, "SLSGetWindowOwner", i32, [u32, u32, C.POINTER(i32)])
    _sig(SL, "CGSHWCaptureWindowList", vp, [u32, C.POINTER(u32), u32, u32])
    SLS_NOTIFY_CB = C.CFUNCTYPE(None, u32, vp, C.c_size_t, vp, u32)
    _sig(SL, "SLSRegisterConnectionNotifyProc", i32, [u32, SLS_NOTIFY_CB, u32, vp])
    _sig(SL, "SLSRequestNotificationsForWindows", i32, [u32, C.POINTER(u32), i32])
    _sig(SL, "SLSSetConnectionProperty", i32, [u32, u32, vp, vp])
    CID = SL.CGSMainConnectionID() if getattr(SL, "CGSMainConnectionID", None) else 0
else:
    SLS_NOTIFY_CB = None
    CID = 0


# --- helpers -------------------------------------------------------------------
_CFSTR_CACHE = {}


def cfstr(s):
    r = _CFSTR_CACHE.get(s)
    if r is None:
        r = CF.CFStringCreateWithCString(None, s.encode("utf-8"), kCFStringEncodingUTF8)
        _CFSTR_CACHE[s] = r
    return r


def cf_to_str(ref, release=False, size=1024):
    if not ref:
        return ""
    buf = C.create_string_buffer(size)
    ok = CF.CFStringGetCString(ref, buf, size, kCFStringEncodingUTF8)
    if release:
        CF.CFRelease(ref)
    return buf.value.decode("utf-8", "replace") if ok else ""


def cf_to_i64(ref):
    if not ref:
        return 0
    v = i64(0)
    return v.value if CF.CFNumberGetValue(ref, kCFNumberSInt64Type, C.byref(v)) else 0


def cf_to_f64(ref):
    if not ref:
        return 0.0
    v = f64(0)
    return v.value if CF.CFNumberGetValue(ref, 13, C.byref(v)) else 0.0  # 13 = kCFNumberDoubleType


def cf_number(n, kind=kCFNumberSInt64Type):
    v = i64(n) if kind == kCFNumberSInt64Type else i32(n)
    return CF.CFNumberCreate(None, kind, C.byref(v))


def cf_array(refs):
    arr = (vp * max(1, len(refs)))(*refs)
    return CF.CFArrayCreate(None, arr, len(refs), C.addressof(kCFTypeArrayCallBacks))


def cf_dict(keys, vals):
    k = (vp * len(keys))(*keys)
    v = (vp * len(vals))(*vals)
    return CF.CFDictionaryCreate(None, k, v, len(keys), kCFTypeDictionaryKeyCallBacks, kCFTypeDictionaryValueCallBacks)


def log(msg):
    sys.stderr.write(time.strftime("%H:%M:%S ") + msg + "\n")
    sys.stderr.flush()


def post_key(keycode, flags=0, source=None):
    """Post a key down+up pair as if typed (used by packages that want to drive the system
    without depending on another package being installed)."""
    for down in (True, False):
        ev = CG.CGEventCreateKeyboardEvent(source, keycode, down)
        if not ev:
            return False
        if flags:
            CG.CGEventSetFlags(ev, flags)
        CG.CGEventPost(kCGSessionEventTap, ev)
        CF.CFRelease(ev)
    return True
