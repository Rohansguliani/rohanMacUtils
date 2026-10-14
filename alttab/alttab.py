#!/usr/bin/python3
"""alttab: an Opt+Tab window switcher scoped to the current Space, with live thumbnails.

  Opt+Tab        cycle the windows on the CURRENT Space (never other Spaces, never minimized
                 windows), most-recently-used first. Hold Opt; Tab / Shift+Tab / arrows / J / K
                 move the highlight; release Opt to switch. Esc cancels. W closes the window.
  Opt+`          the same, limited to the frontmost app's windows. ` inside an open switcher
                 toggles between the two scopes.

The order is frozen while the switcher is open, so fast Tab-Tab-Tab never reorders under you.
Hotkeys and the card grid are in ~/.config/mactools/alttab.json.

The window list, thumbnails and cross-app focus are SkyLight SPI (see util/windows.py); the
same calls AltTab and yabai use. Tested on macOS 26.x.

Permissions for /usr/bin/python3: Accessibility (the hotkey tap and AXRaise) and Screen
Recording (thumbnails and window titles; without it the cards show app icons only).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "util"))
import config  # noqa: E402
import daemon  # noqa: E402
import windows  # noqa: E402
from cards import CardHUD  # noqa: E402
from macbridge import (CF, CG, CID, SL, TIMER_CB, FLAG_SHIFT, MODIFIER_MASK, cbool, i64, kCFRunLoopCommonModes,  # noqa: E402
                       kCGEventFlagsChanged, kCGEventKeyDown, kCGEventTapDisabledByTimeout,
                       kCGEventTapDisabledByUserInput, kCGKeyboardEventKeycode, log)
from objc import cls, send  # noqa: E402
from windows import (ax_raise_or_close, focus_window_async, get_cached_thumb, ordered_windows_for_session,  # noqa: E402
                     prune_thumbs, recent_mru, update_thumb, warm_thumbnails_async)

DEFAULTS = {
    "switch_windows": "opt+tab",       # all windows on the current Space
    "switch_app_windows": "opt+`",     # frontmost app's windows on the current Space; "" disables
    "columns": 4,                      # cards per row
    "max_cards": 12,
}
CFG, CFG_PATH = config.load("alttab", DEFAULTS)
HK_SWITCH = config.parse_hotkey(CFG["switch_windows"])
HK_SWITCH_APP = config.parse_hotkey(CFG["switch_app_windows"]) if CFG["switch_app_windows"] else None
# keys that act only while the switcher is open
KEY_TAB, KEY_ESC, KEY_W, KEY_J, KEY_K = 48, 53, 13, 38, 40
KEY_LEFT, KEY_RIGHT, KEY_DOWN, KEY_UP = 123, 124, 125, 126

_HUD: Any = None
_ACTIVE = False
_WINDOWS = []
_IDX = 0
_APP_ONLY = False
_MODS = 0  # modifier bits held when summoned; releasing any of them commits
_DIRTY = False  # a background thumbnail refresh changed a card we are showing


def _mark_dirty():
    global _DIRTY
    if _ACTIVE:
        _DIRTY = True
        CF.CFRunLoopWakeUp(CF.CFRunLoopGetMain())


def _open(wins, app_only, mods, backwards):
    global _ACTIVE, _WINDOWS, _IDX, _APP_ONLY, _MODS
    _ACTIVE, windows.mru_frozen = True, True
    _APP_ONLY, _MODS, _WINDOWS = app_only, mods, wins
    _IDX = 0 if len(wins) == 1 else ((len(wins) - 1) if backwards else 1)
    _HUD.render(_WINDOWS, _IDX, reposition=True)
    warm_thumbnails_async(wins, on_changed=_mark_dirty)


def _close(commit):
    global _ACTIVE, _WINDOWS
    chosen = _WINDOWS[_IDX] if (commit and _WINDOWS) else None
    _ACTIVE, windows.mru_frozen = False, False
    _WINDOWS = []
    _HUD.hide()
    if chosen:
        focus_window_async(chosen)


def on_event(proxy, etype, event, refcon):
    global _WINDOWS, _IDX, _APP_ONLY
    if etype in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
        log(f"event tap disabled ({etype:#x}); re-enabling")
        daemon.reenable_tap()
        return event

    mods = CG.CGEventGetFlags(event) & MODIFIER_MASK
    if etype == kCGEventFlagsChanged:
        if _ACTIVE and (mods & _MODS) != _MODS:  # a summon modifier went up: commit
            _close(commit=True)
        return event
    if etype != kCGEventKeyDown:
        return event

    key = CG.CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
    shift = bool(mods & FLAG_SHIFT)

    if not _ACTIVE:
        combo = (key, mods)
        if combo == HK_SWITCH or combo == (HK_SWITCH[0], HK_SWITCH[1] | FLAG_SHIFT):  # Shift+hotkey: start backwards
            app_only = False
        elif HK_SWITCH_APP and combo in (HK_SWITCH_APP, (HK_SWITCH_APP[0], HK_SWITCH_APP[1] | FLAG_SHIFT)):
            app_only = True
        else:
            return event
        wins = ordered_windows_for_session(app_only=app_only)
        if not wins:
            return None
        _open(wins, app_only, mods & ~FLAG_SHIFT, backwards=shift)
        return None

    # Open session: pure in-memory index math (~0.04 ms); nothing else reaches the apps.
    n = len(_WINDOWS)
    if key == KEY_ESC:
        _close(commit=False)
    elif HK_SWITCH_APP and key == HK_SWITCH_APP[0]:
        wins = ordered_windows_for_session(app_only=not _APP_ONLY)
        if wins:
            _APP_ONLY = not _APP_ONLY
            _WINDOWS, _IDX = wins, min(_IDX, len(wins) - 1)
            _HUD.render(_WINDOWS, _IDX, reposition=True)
            warm_thumbnails_async(wins, on_changed=_mark_dirty)
    elif key == KEY_W and n:
        w = _WINDOWS.pop(_IDX)
        threading.Thread(target=ax_raise_or_close, args=(w["pid"], w["wid"], "AXClose"), daemon=True).start()
        if not _WINDOWS:
            _close(commit=False)
        else:
            _IDX %= len(_WINDOWS)
            _HUD.render(_WINDOWS, _IDX, reposition=True)
    elif key in (KEY_TAB, KEY_RIGHT, KEY_J) or (HK_SWITCH and key == HK_SWITCH[0]):
        _IDX = (_IDX + (-1 if shift else 1)) % n
        _HUD.render(_WINDOWS, _IDX, reposition=False)
    elif key in (KEY_LEFT, KEY_K):
        _IDX = (_IDX - 1) % n
        _HUD.render(_WINDOWS, _IDX, reposition=False)
    elif key == KEY_DOWN:
        _IDX = (_IDX + min(n, _HUD.columns)) % n
        _HUD.render(_WINDOWS, _IDX, reposition=False)
    elif key == KEY_UP:
        _IDX = (_IDX - min(n, _HUD.columns)) % n
        _HUD.render(_WINDOWS, _IDX, reposition=False)
    return None  # every other key is swallowed while the switcher is up


def run_benchmark():
    app = send(cls("NSApplication"), "sharedApplication")
    send(app, "setActivationPolicy:", cbool, (i64,), 1)
    hud = CardHUD(CFG["max_cards"], CFG["columns"])
    wins = ordered_windows_for_session(app_only=False)
    for w in wins:
        update_thumb(w["wid"], max_age=0.0)
    t1 = time.perf_counter()
    wins2 = ordered_windows_for_session(app_only=False)
    dt_enum = (time.perf_counter() - t1) * 1000.0
    t2 = time.perf_counter()
    hud.render(wins2, min(1, len(wins2) - 1), reposition=True)
    dt_summon = (time.perf_counter() - t2) * 1000.0
    t3 = time.perf_counter()
    for i in range(10):
        hud.render(wins2, i % max(1, len(wins2)), reposition=False)
    dt_step = (time.perf_counter() - t3) * 100.0
    hud.hide()
    print(f"Current Space ID={SL.CGSGetActiveSpace(CID)} | Windows on THIS Space={len(wins2)}")
    print(f"Per-Space snapshot latency    : {dt_enum:.3f} ms")
    print(f"Warm HUD summon w/ thumbnails : {dt_summon:.3f} ms")
    print(f"Tab cycle step latency (avg)  : {dt_step:.3f} ms")
    for w in wins2:
        print(f"  wid={w['wid']:<6} thumb={get_cached_thumb(w['wid']) is not None} | {w['app']:<15} | {w['title']}")


def main():
    global _HUD
    if "--check-config" in sys.argv:
        print("config OK:", CFG_PATH)
        return
    if "--test" in sys.argv:
        run_benchmark()
        return
    daemon.wait_for_accessibility("alttab")

    app = send(cls("NSApplication"), "sharedApplication")
    send(app, "setActivationPolicy:", cbool, (i64,), 1)
    _HUD = CardHUD(CFG["max_cards"], CFG["columns"])
    wins = ordered_windows_for_session(app_only=False)
    for w in wins:
        update_thumb(w["wid"], max_age=0.0)
    windows.start_focus_tracking()

    tick_n = [0]

    def tick_cb(timer, info):  # 50 ms: repaint changed thumbnails; every 2.5 s idle: pre-warm + prune
        global _DIRTY
        if _DIRTY and _ACTIVE:
            _DIRTY = False
            _HUD.refresh_thumbnails_in_place(_WINDOWS)
        tick_n[0] += 1
        if tick_n[0] % 50 == 0 and not _ACTIVE:
            cur = ordered_windows_for_session(app_only=False)
            prune_thumbs({w["wid"] for w in cur} | set(recent_mru()))
            warm_thumbnails_async(cur, max_age=2.0)

    tcb = TIMER_CB(tick_cb)
    timer = CF.CFRunLoopTimerCreate(None, CF.CFAbsoluteTimeGetCurrent() + 0.05, 0.05, 0, 0, tcb, None)
    CF.CFRunLoopAddTimer(CF.CFRunLoopGetMain(), timer, kCFRunLoopCommonModes)

    daemon.install("alttab", (1 << kCGEventKeyDown) | (1 << kCGEventFlagsChanged), on_event,
                   ready_msg=f"alttab ready: {CFG['switch_windows']} / {CFG['switch_app_windows'] or 'off'}"
                             f" ({len(wins)} windows on current Space; config {CFG_PATH})")
    send(app, "run")


if __name__ == "__main__":
    main()
