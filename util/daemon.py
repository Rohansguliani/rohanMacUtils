"""daemon: the shared skeleton every mactools event-tap daemon runs on.

- waits for the Accessibility grant (prompting once),
- creates the session event tap with the caller's mask + callback,
- installs the 5 s health timer that re-enables a tap macOS disabled during sleep/lock
  and exits (for launchd KeepAlive) if the Mach port itself died,
- runs the main run loop.

TCC note: all mactools daemons run under /usr/bin/python3, Apple's Xcode-select shim.
That one binary is hardlinked under ~78 names (/usr/bin/git, /usr/bin/DeRez, ...), and
tccd attributes a process to whichever *name* the kernel's vnode cache currently holds
for that inode. If another hardlink was resolved more recently, a long-running daemon can
lose its grant mid-flight (Accessibility or Input Monitoring) even though System Settings
still shows it. `respawn_if_fresh_process_is_allowed()` is the remedy: probe in a fresh
/usr/bin/python3 child (which re-caches the name) and, if *it* is allowed, exit so
launchd restarts us clean.
"""

import ctypes as C
import os
import subprocess
import sys
import time

from macbridge import (AX, CF, CG, TAP_CB, TIMER_CB, cf_dict, kCFBooleanTrue, kCFRunLoopCommonModes,
                       kCGEventTapOptionDefault, kCGHeadInsertEventTap, kCGSessionEventTap, log, vp)

_TAP = None
_REFS = []  # keep ctypes callbacks alive for the process lifetime


def accessibility_trusted(prompt=False):
    if not prompt:
        return bool(AX.AXIsProcessTrusted())
    key = vp.in_dll(AX, "kAXTrustedCheckOptionPrompt").value
    opts = cf_dict([key], [kCFBooleanTrue])
    ok = AX.AXIsProcessTrustedWithOptions(opts)
    CF.CFRelease(opts)
    return bool(ok)


def wait_for_accessibility(name):
    if accessibility_trusted(prompt=True):
        return
    log(f"{name}: waiting for Accessibility (System Settings > Privacy & Security > Accessibility > +"
        f" > press Cmd+Shift+G, type /usr/bin/python3, Add)")
    polls = 0
    while not accessibility_trusted():
        time.sleep(2)
        polls += 1
        if polls % 15 == 0 and respawn_if_fresh_process_is_allowed("accessibility"):
            return  # unreachable; respawn exits
    log(f"{name}: accessibility granted")


def respawn_if_fresh_process_is_allowed(service):
    """See module docstring. `service` is 'accessibility' or 'listen' (Input Monitoring).
    Returns False if a fresh process is *also* denied (user really hasn't granted it)."""
    if service == "accessibility":
        probe = ("import ctypes as C, sys; a = C.CDLL('/System/Library/Frameworks/ApplicationServices.framework/"
                 "Frameworks/HIServices.framework/HIServices'); a.AXIsProcessTrusted.restype = C.c_bool; "
                 "sys.exit(0 if a.AXIsProcessTrusted() else 1)")
    else:
        probe = ("import ctypes as C, sys; sys.exit(C.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')"
                 ".IOHIDCheckAccess(1))")
    rc = subprocess.call(["/usr/bin/python3", "-c", probe], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if rc == 0:
        log(f"{service}: denied to this process but granted to a fresh /usr/bin/python3 "
            f"(stale TCC attribution); exiting for respawn")
        os._exit(2)
    return False


def install(name, mask, callback, on_tick=None, tick_s=5.0, ready_msg=None):
    """Create the tap and the health timer on the current (main) run loop without blocking.
    `callback(proxy, etype, event, refcon)` is the raw CGEventTapCallBack; `on_tick()` runs
    every `tick_s` on the main loop. Daemons that drive AppKit call this, then `[NSApp run]`."""
    global _TAP
    wait_for_accessibility(name)
    cb = TAP_CB(callback)
    _REFS.append(cb)
    _TAP = CG.CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap, kCGEventTapOptionDefault, mask, cb, None)
    if not _TAP:
        log(f"{name}: CGEventTapCreate failed (Accessibility revoked?)")
        sys.exit(1)
    src = CF.CFMachPortCreateRunLoopSource(None, _TAP, 0)
    CF.CFRunLoopAddSource(CF.CFRunLoopGetCurrent(), src, kCFRunLoopCommonModes)
    CG.CGEventTapEnable(_TAP, True)

    def health(timer, info):
        if not CF.CFMachPortIsValid(_TAP):
            log(f"{name}: event tap port invalidated; exiting for respawn")
            os._exit(2)
        if not CG.CGEventTapIsEnabled(_TAP):
            log(f"{name}: event tap found disabled (sleep/lock?); re-enabling")
            CG.CGEventTapEnable(_TAP, True)
        if on_tick:
            on_tick()

    tcb = TIMER_CB(health)
    _REFS.append(tcb)
    timer = CF.CFRunLoopTimerCreate(None, CF.CFAbsoluteTimeGetCurrent() + tick_s, tick_s, 0, 0, tcb, None)
    CF.CFRunLoopAddTimer(CF.CFRunLoopGetCurrent(), timer, kCFRunLoopCommonModes)
    if ready_msg:
        log(ready_msg)


def run(name, mask, callback, on_tick=None, tick_s=5.0, ready_msg=None):
    """`install()` then block in the main run loop."""
    install(name, mask, callback, on_tick, tick_s, ready_msg)
    CF.CFRunLoopRun()


def tap():
    return _TAP


def reenable_tap():
    if _TAP:
        CG.CGEventTapEnable(_TAP, True)
