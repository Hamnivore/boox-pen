#!/usr/bin/python3
"""Boox pen -> Linux pen tablet, over adb/USB.

Streams raw evdev events from the Boox Wacom digitizer (adb shell user is in
the `input` group, no root needed) into a uinput tablet on this machine, so
Blender/Krita/libinput see a native pen: 4096 pressure levels, tilt, hover,
eraser, side buttons. No browser, no screen streaming.

The active area is cropped to the laptop's aspect ratio (circles stay round),
anchored top-left so the spare strip is at the bottom/right for your palm.

Usage: boox-pen [--rotate 0|90|180|270] [--aspect 16:9|1920x1080|off] [--no-pad]
Needs write access to /dev/uinput (group `uinput`; see run.sh).
"""
import argparse, glob, json, os, select, struct, subprocess, sys, threading, time
from evdev import UInput, AbsInfo, ecodes as e

EVT = struct.Struct("<qqHHi")  # arm64 input_event: sec, usec, type, code, value
PAD_PORT = 8766
STATE = {"seq": 0, "raw": [0.5, 0.5], "area": [0, 0, 1, 1]}  # served to pad.html; raw-normalized coords

def find_node():
    out = subprocess.run(["adb", "shell", "getevent", "-pl"], capture_output=True, text=True).stdout
    node = None
    for line in out.splitlines():
        if line.startswith("add device"):
            node = line.split(":", 1)[1].strip()
        elif "ABS_PRESSURE" in line and node:
            return node
    sys.exit("no pen digitizer found over adb (is the tablet plugged in and authorized?)")

def absinfo(node):
    """Read axis ranges from the tablet so the virtual device matches exactly."""
    out = subprocess.run(["adb", "shell", "getevent", "-pl", node], capture_output=True, text=True).stdout
    axes = {}
    for line in out.splitlines():
        for name in ("ABS_X", "ABS_Y", "ABS_PRESSURE", "ABS_DISTANCE", "ABS_TILT_X", "ABS_TILT_Y"):
            if f"{name} " in line:
                f = dict(p.strip().split(" ", 1) for p in line.split(":", 1)[1].split(",") if p.strip().count(" ") >= 1)
                axes[name] = (int(f["min"]), int(f["max"]))
    return axes

def screen_aspect(arg):
    if arg == "off":
        return None
    if arg:
        w, h = arg.replace("x", ":").split(":")
        return float(w) / float(h)
    modes = [open(m).readline().strip() for m in glob.glob("/sys/class/drm/card*-*/modes")
             if open(os.path.join(os.path.dirname(m), "status")).read().strip() == "connected"]
    modes = [m for m in modes if m]
    if len(modes) != 1:
        print(f"boox-pen: {len(modes)} monitors connected; pass --aspect W:H (using full tablet)", flush=True)
        return None
    w, h = modes[0].split("x")
    return int(w) / int(h)

# Rotation in normalized coords: raw (p,q) in [0,1]^2 -> output (s,t). Tilt follows the same map.
ROT = {
    0:   (lambda p, q: (p, q),         lambda tx, ty: (tx, ty)),
    90:  (lambda p, q: (1 - q, p),     lambda tx, ty: (-ty, tx)),
    180: (lambda p, q: (1 - p, 1 - q), lambda tx, ty: (-tx, -ty)),
    270: (lambda p, q: (q, 1 - p),     lambda tx, ty: (ty, -tx)),
}
INV = {0: 0, 90: 270, 180: 180, 270: 90}

def open_pad():
    """Serve pad.html + state on localhost, tunnel to the tablet, open fullscreen in Chrome."""
    import http.server, functools
    here = os.path.dirname(os.path.abspath(__file__))
    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if self.path.startswith("/state.json"):
                body = json.dumps(STATE).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            else:
                super().do_GET()
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", PAD_PORT), functools.partial(H, directory=here))
    except OSError as err:
        print(f"boox-pen: pad server not started ({err}); continuing without it", flush=True)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    subprocess.run(["adb", "reverse", f"tcp:{PAD_PORT}", f"tcp:{PAD_PORT}"], capture_output=True)
    subprocess.run(["adb", "shell", "am", "start", "-a", "android.intent.action.VIEW",
                    "-d", f"http://localhost:{PAD_PORT}/pad.html",
                    "-n", "com.android.chrome/com.google.android.apps.chrome.Main"], capture_output=True)
    # A tap injected by adb counts as a real user gesture, so the page can go fullscreen by itself.
    time.sleep(2.5)
    size = subprocess.run(["adb", "shell", "wm", "size"], capture_output=True, text=True).stdout
    w, h = (int(n) for n in size.strip().split()[-1].split("x"))
    subprocess.run(["adb", "shell", "input", "tap", str(w // 2), str(h // 2)], capture_output=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rotate", type=int, default=0, choices=(0, 90, 180, 270))
    ap.add_argument("--aspect", help="screen aspect W:H or WxH; default: the connected monitor; 'off' = whole tablet")
    ap.add_argument("--node")
    ap.add_argument("--no-pad", action="store_true", help="don't open the input-swallowing page on the tablet")
    a = ap.parse_args()

    node = a.node or find_node()
    ax = absinfo(node)
    (x0, x1), (y0, y1) = ax["ABS_X"], ax["ABS_Y"]
    rxw, ryh = x1 - x0, y1 - y0
    pos, tilt = ROT[a.rotate]
    W, H = (ryh, rxw) if a.rotate in (90, 270) else (rxw, ryh)   # output size in sensor units (~0.01 mm)

    # Active area: largest rect of the screen's aspect, anchored top-left.
    asp = screen_aspect(a.aspect)
    aw, ah = W, H
    if asp:
        aw, ah = (H * asp, H) if W / H > asp else (W, W / asp)
    sw, th = aw / W, ah / H                                       # area as a fraction of the output
    inv = ROT[INV[a.rotate]][0]
    corners = [inv(s, t) for s in (0, sw) for t in (0, th)]
    STATE["area"] = [min(c[0] for c in corners), min(c[1] for c in corners),
                     max(c[0] for c in corners), max(c[1] for c in corners)]

    res = 100  # Tab Ultra C: ~209x157 mm over 20966x15725 units -> ~100 units/mm
    caps = {
        e.EV_KEY: [e.BTN_TOOL_PEN, e.BTN_TOOL_RUBBER, e.BTN_TOUCH, e.BTN_STYLUS, e.BTN_STYLUS2],
        e.EV_ABS: [
            (e.ABS_X, AbsInfo(0, 0, int(aw), 0, 0, res)),
            (e.ABS_Y, AbsInfo(0, 0, int(ah), 0, 0, res)),
            (e.ABS_PRESSURE, AbsInfo(0, *ax["ABS_PRESSURE"], 0, 0, 0)),
            (e.ABS_DISTANCE, AbsInfo(0, *ax.get("ABS_DISTANCE", (0, 255)), 0, 0, 0)),
            (e.ABS_TILT_X, AbsInfo(0, *ax.get("ABS_TILT_X", (-63, 63)), 0, 0, 57)),  # degrees -> units/rad
            (e.ABS_TILT_Y, AbsInfo(0, *ax.get("ABS_TILT_Y", (-63, 63)), 0, 0, 57)),
        ],
    }
    ui = UInput(caps, name="Boox Pen (adb)", vendor=0x056A, product=0x0001, version=1,
                input_props=[e.INPUT_PROP_POINTER])

    subprocess.run(["adb", "shell", "svc", "power", "stayon", "usb"])  # digitizer sleeps with the screen
    if not a.no_pad:
        threading.Thread(target=open_pad, daemon=True).start()
    p = subprocess.Popen(["adb", "exec-out", "cat", node], stdout=subprocess.PIPE, bufsize=0)
    fd = p.stdout.fileno()
    buf = b""
    tool = None        # code of the tool currently in proximity
    synth = False      # True if we injected BTN_TOOL_PEN because the device sends none
    rx, ry, tx, ty = x0, y0, 0, 0
    dpos = dtilt = down = False
    last = time.monotonic()
    print(f"boox-pen: {node} -> {ui.device.path} rotate={a.rotate} "
          f"area={aw/res:.0f}x{ah/res:.0f}mm of {W/res:.0f}x{H/res:.0f}mm", flush=True)
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 0.15)
            if not r:
                if synth and tool and time.monotonic() - last > 0.15:  # left proximity
                    ui.write(e.EV_KEY, e.BTN_TOUCH, 0); ui.write(e.EV_ABS, e.ABS_PRESSURE, 0)
                    ui.write(e.EV_KEY, tool, 0); ui.syn(); tool = None
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                sys.exit("adb stream ended (tablet unplugged?)")
            buf += chunk
            n = len(buf) // EVT.size * EVT.size
            for off in range(0, n, EVT.size):
                _, _, t, c, v = EVT.unpack_from(buf, off)
                last = time.monotonic()
                if t == e.EV_KEY and c in (e.BTN_TOOL_PEN, e.BTN_TOOL_RUBBER, e.BTN_TOOL_BRUSH):
                    c = e.BTN_TOOL_RUBBER if c == e.BTN_TOOL_RUBBER else e.BTN_TOOL_PEN
                    if v and tool and tool != c:          # tool switch: out then in
                        ui.write(e.EV_KEY, tool, 0); ui.syn()
                    tool, synth = (c, False) if v else (None, False)
                    ui.write(e.EV_KEY, c, v)
                elif t == e.EV_ABS:
                    if tool is None:
                        tool, synth = e.BTN_TOOL_PEN, True
                        ui.write(e.EV_KEY, e.BTN_TOOL_PEN, 1)
                    if c == e.ABS_X: rx, dpos = v, True
                    elif c == e.ABS_Y: ry, dpos = v, True
                    elif c == e.ABS_TILT_X: tx, dtilt = v, True
                    elif c == e.ABS_TILT_Y: ty, dtilt = v, True
                    else: ui.write(e.EV_ABS, c, v)
                elif t == e.EV_KEY and c in (e.BTN_TOUCH, e.BTN_STYLUS, e.BTN_STYLUS2):
                    if c == e.BTN_TOUCH and v:
                        down = True                          # record position at SYN, once X/Y are in
                    ui.write(e.EV_KEY, c, v)
                elif t == e.EV_SYN and c == e.SYN_REPORT:
                    if dpos:
                        s, tt = pos((rx - x0) / rxw, (ry - y0) / ryh)
                        ui.write(e.EV_ABS, e.ABS_X, int(min(max(s * W, 0), aw)))
                        ui.write(e.EV_ABS, e.ABS_Y, int(min(max(tt * H, 0), ah)))
                    if dtilt:
                        ox, oy = tilt(tx, ty)
                        ui.write(e.EV_ABS, e.ABS_TILT_X, ox); ui.write(e.EV_ABS, e.ABS_TILT_Y, oy)
                    if down:                                 # pen-down position, for pad.html calibration
                        STATE["raw"] = [(rx - x0) / rxw, (ry - y0) / ryh]; STATE["seq"] += 1
                    dpos = dtilt = down = False
                    ui.syn()
            buf = buf[n:]
    except KeyboardInterrupt:
        pass
    finally:
        p.terminate(); ui.close()

if __name__ == "__main__":
    main()
