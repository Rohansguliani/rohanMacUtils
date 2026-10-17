"""Post forged DockSwipe sequences from the recorded macOS 27 payloads and watch CGS.

usage: sstest.py "<name>: <step>;<step>;..."  where step = PHASE,cgprog,hidprog,vel
       PHASE in B C E, e.g.  "flick: B,1e-4,1e-4,2000; E,1e-4,1e-4,2000"
"""
import struct
import sys
import time

sys.path.insert(0, "/Users/rohansguliani/mactools/macos27/spaceswitch")
import spaceswitch as ss  # noqa: E402

CG, CF, C = ss.CG, ss.CF, ss.C
PH = {"B": ss.PHASE_BEGAN, "C": ss.PHASE_CHANGED, "E": ss.PHASE_ENDED}


def idx():
    info = ss.space_info()
    return info[1] if info else None


def watch(label, seconds=1.5):
    t0 = time.monotonic()
    last = idx()
    trail = [(0.0, last)]
    while time.monotonic() - t0 < seconds:
        cur = idx()
        if cur != last:
            trail.append((round(time.monotonic() - t0, 3), cur))
            last = cur
        time.sleep(0.004)
    print("  %-10s trail=%s" % (label, trail), flush=True)
    return last


def make(motion, phase, cg_prog, hid_prog, vx, vy):
    payload = ss.RECORDINGS.get(motion, phase)
    assert payload, "no recording for motion %d phase %d" % (motion, phase)
    ev = CG.CGEventCreate(None)
    CG.CGEventSetIntegerValueField(ev, ss.F_CGS_EVENT_TYPE, ss.CGS_EVENT_DOCK_CONTROL)
    CG.CGEventSetIntegerValueField(ev, ss.F_GESTURE_HID_TYPE, ss.HID_DOCK_SWIPE)
    CG.CGEventSetIntegerValueField(ev, ss.F_GESTURE_PHASE, phase)
    CG.CGEventSetIntegerValueField(ev, ss.F_SWIPE_MOTION, motion)
    CG.CGEventSetDoubleValueField(ev, ss.F_SWIPE_PROGRESS, cg_prog)
    CG.CGEventSetDoubleValueField(ev, ss.F_SWIPE_VEL_X, vx)
    CG.CGEventSetDoubleValueField(ev, ss.F_SWIPE_VEL_Y, vy)
    data = CG.CGEventCreateData(None, ev)
    CF.CFRelease(ev)
    blob = C.string_at(CF.CFDataGetBytePtr(data), CF.CFDataGetLength(data))
    CF.CFRelease(data)
    hid = ss.patch_payload(payload, hid_prog, vx, vy)
    blob += struct.pack(">HH", len(hid), ss.FIELD_IOHID_PAYLOAD) + hid
    nd = CF.CFDataCreate(None, blob, len(blob))
    out = CG.CGEventCreateFromData(None, nd)
    CF.CFRelease(nd)
    CG.CGEventSetIntegerValueField(out, ss.kCGEventSourceUserData, ss.SYNTHETIC_MARKER)
    return out


def run(name, steps, direction):
    s = float(direction)
    for phase, cgp, hidp, vel in steps:
        ev = make(ss.MOTION_HORIZONTAL, PH[phase], s * cgp, s * hidp, s * vel, s * vel)
        CG.CGEventPost(ss.kCGSessionEventTap, ev)
        CF.CFRelease(ev)


def parse(spec):
    name, _, body = spec.partition(":")
    steps = []
    for st in body.split(";"):
        p, cgp, hidp, vel = [x.strip() for x in st.split(",")]
        steps.append((p, float(cgp), float(hidp), float(vel)))
    return name.strip(), steps


print("start index:", idx(), "of", (ss.space_info() or (0, 0, 0))[2], flush=True)
for spec in sys.argv[1:]:
    name, steps = parse(spec)
    start = idx()
    print("%s  (from %s)" % (name, start), flush=True)
    run(name, steps, +1)
    a = watch("+1")
    time.sleep(1.0)
    run(name, steps, -1)
    b = watch("-1")
    time.sleep(1.0)
    print("  net: %s -> %s -> %s (settled %s)" % (start, a, b, idx()), flush=True)
print("end index:", idx())
