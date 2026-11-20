#!/usr/bin/python3
"""Boox pen -> Linux pen tablet, over adb/USB.

Streams raw evdev events from the Boox Wacom digitizer (adb shell user is in
the `input` group, no root needed) into a uinput tablet on this machine, so
Blender/Krita/libinput see a native pen: 4096 pressure levels, tilt, hover,
eraser, side buttons. No browser, no screen streaming.

The active area is cropped to the target screen's aspect ratio (circles stay
round), anchored top-left; the spare strip is a palm rest where the pen is
ignored. On Hyprland the pad page on the tablet can switch the target screen.

Usage: boox-pen [--rotate 0|90|180|270] [--aspect 16:9|1920x1080|off] [--no-pad]
Needs write access to /dev/uinput (group `uinput`; see run.sh).
"""
import argparse, fcntl, glob, json, os, select, shutil, struct, subprocess, sys, threading, time
from evdev import UInput, AbsInfo, ecodes as e

EVT = struct.Struct("<qqHHi")  # arm64 input_event: sec, usec, type, code, value
PAD_PORT = 8766
DEVICE_NAME = "Boox Pen (adb)"
HYPR_DEVICE = "boox-pen-(adb)"   # Hyprland's name for it: lowercased, spaces -> '-'
CONF = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "boox-pen")
MARGIN_MM = 3                    # pen keeps working this far past the area edge (clamped), then goes dead

# Shared with the HTTP thread (pad.html). The area is in raw-normalized sensor coords.
STATE = {"area": [0, 0, 1, 1], "ds": [1, 0], "dt": [0, 1],
         "screens": [], "active": None, "can_switch": False}
LOCK = threading.Lock()

# ---------- tablet discovery ----------

def adb(*args, **kw):
    return subprocess.run(["adb", *args], capture_output=True, text=True, **kw).stdout

def find_node():
    node = None
    for line in adb("shell", "getevent", "-pl").splitlines():
        if line.startswith("add device"):
            node = line.split(":", 1)[1].strip()
        elif "ABS_PRESSURE" in line and node:
            return node
    sys.exit("no pen digitizer found over adb (is the tablet plugged in and authorized?)")

def absinfo(node):
    """Read axis ranges from the tablet so the virtual device matches exactly."""
    axes = {}
    for line in adb("shell", "getevent", "-pl", node).splitlines():
        for name in ("ABS_X", "ABS_Y", "ABS_PRESSURE", "ABS_DISTANCE", "ABS_TILT_X", "ABS_TILT_Y"):
            if f"{name} " in line:
                f = dict(p.strip().split(" ", 1) for p in line.split(":", 1)[1].split(",") if p.strip().count(" ") >= 1)
                axes[name] = (int(f["min"]), int(f["max"]))
    return axes

# ---------- screens (Hyprland) ----------

def hyprctl(*args):
    """Run the real hyprctl against the current Hyprland instance; None if unavailable."""
    exe = "/usr/bin/hyprctl" if os.path.exists("/usr/bin/hyprctl") else shutil.which("hyprctl")
    if not exe:
        return None
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:  # e.g. started from udev/systemd before the env was imported: newest instance
        dirs = sorted(glob.glob(os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "hypr", "*", ".socket.sock")),
                      key=os.path.getmtime)
        if not dirs:
            return None
        sig = os.path.basename(os.path.dirname(dirs[-1]))
    r = subprocess.run([exe, "-i", sig, *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None

def hypr_screens():
    out = hyprctl("monitors", "-j")
    if not out:
        return []
    screens = []
    for m in json.loads(out):
        if m.get("disabled"):
            continue
        w, h = m["width"], m["height"]
        if m.get("transform", 0) % 2:
            w, h = h, w
        sc = m.get("scale", 1) or 1
        # w/h: pixels (shown to the user); x/y/lw/lh: layout in logical px, for the pad's screen map.
        screens.append({"name": m["name"], "w": w, "h": h, "x": m.get("x", 0), "y": m.get("y", 0),
                        "lw": round(w / sc), "lh": round(h / sc), "focused": m.get("focused", False)})
    return screens

def sysfs_aspect():
    modes = [open(m).readline().strip() for m in glob.glob("/sys/class/drm/card*-*/modes")
             if open(os.path.join(os.path.dirname(m), "status")).read().strip() == "connected"]
    modes = [m for m in modes if m]
    if len(modes) != 1:
        return None
    w, h = modes[0].split("x")
    return int(w) / int(h)

def saved_screen():
    try:
        return open(os.path.join(CONF, "screen")).read().strip()
    except OSError:
        return None

def save_screen(name):
    try:
        os.makedirs(CONF, exist_ok=True)
        open(os.path.join(CONF, "screen"), "w").write(name + "\n")
    except OSError:
        pass

# ---------- geometry ----------

# Rotation in normalized coords: raw (p,q) in [0,1]^2 -> output (s,t). Tilt follows the same map.
ROT = {
    0:   (lambda p, q: (p, q),         lambda tx, ty: (tx, ty)),
    90:  (lambda p, q: (1 - q, p),     lambda tx, ty: (-ty, tx)),
    180: (lambda p, q: (1 - p, 1 - q), lambda tx, ty: (-tx, -ty)),
    270: (lambda p, q: (q, 1 - p),     lambda tx, ty: (ty, -tx)),
}
INV = {0: 0, 90: 270, 180: 180, 270: 90}

class Geometry:
    """Output-space size and the active area (largest rect of the screen aspect, top-left)."""
    def __init__(self, rotate, rxw, ryh):
        self.rotate = rotate
        self.W, self.H = (ryh, rxw) if rotate in (90, 270) else (rxw, ryh)
        self.sw = self.th = 1.0
        inv = ROT[INV[rotate]][0]
        o = inv(0, 0)
        STATE["ds"] = [a - b for a, b in zip(inv(1, 0), o)]
        STATE["dt"] = [a - b for a, b in zip(inv(0, 1), o)]

    def set_aspect(self, asp):
        W, H = self.W, self.H
        aw, ah = (W, H) if not asp else ((H * asp, H) if W / H > asp else (W, W / asp))
        self.sw, self.th = aw / W, ah / H
        inv = ROT[INV[self.rotate]][0]
        c = [inv(s, t) for s in (0, self.sw) for t in (0, self.th)]
        STATE["area"] = [min(x[0] for x in c), min(x[1] for x in c), max(x[0] for x in c), max(x[1] for x in c)]
        return aw, ah

def apply_screen(name, geo, fixed_aspect):
    """Point the pen at a Hyprland output and refit the area to its aspect."""
    screens = STATE["screens"]
    m = next((s for s in screens if s["name"] == name), None)
    if not m:
        return False
    hyprctl("keyword", f"device[{HYPR_DEVICE}]:output", name)
    with LOCK:
        aw, ah = geo.set_aspect(fixed_aspect or m["w"] / m["h"])
        STATE["active"] = name
    save_screen(name)
    print(f"boox-pen: screen {name} {m['w']}x{m['h']}, area {aw/100:.0f}x{ah/100:.0f}mm", flush=True)
    return True

# ---------- pad page ----------

def open_pad(geo, fixed_aspect):
    """Serve pad.html + state on localhost, tunnel to the tablet, open fullscreen in Chrome."""
    import http.server, functools
    here = os.path.dirname(os.path.abspath(__file__))
    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
        def _json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if self.path.startswith("/state.json"):
                with LOCK:
                    self._json(STATE)
            else:
                super().do_GET()
        def do_POST(self):
            if self.path.startswith("/screen"):
                name = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode().strip()
                STATE["screens"] = hypr_screens() or STATE["screens"]
                ok = apply_screen(name, geo, fixed_aspect)
                with LOCK:
                    self._json({"ok": ok, **STATE})
            else:
                self.send_error(404)
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", PAD_PORT), functools.partial(H, directory=here))
    except OSError as err:
        print(f"boox-pen: pad server not started ({err}); continuing without it", flush=True)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    adb("reverse", f"tcp:{PAD_PORT}", f"tcp:{PAD_PORT}")
    # application_id makes Chrome reuse the same tab instead of opening a new one each start.
    adb("shell", "am", "start", "-a", "android.intent.action.VIEW",
        "-d", f"http://localhost:{PAD_PORT}/pad.html",
        "--es", "com.android.browser.application_id", "boox-pen",
        "-n", "com.android.chrome/com.google.android.apps.chrome.Main")
    # A tap injected by adb counts as a real user gesture, so the page can go fullscreen by itself.
    time.sleep(2.5)
    w, h = (int(n) for n in adb("shell", "wm", "size").strip().split()[-1].split("x"))
    adb("shell", "input", "tap", str(w // 2), str(h // 2))

# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rotate", type=int, default=0, choices=(0, 90, 180, 270))
    ap.add_argument("--aspect", help="fixed aspect W:H or WxH (default: the target screen's); 'off' = whole tablet")
    ap.add_argument("--node")
    ap.add_argument("--no-pad", action="store_true", help="don't open the input-swallowing page on the tablet")
    a = ap.parse_args()

    # One bridge at a time (udev autostart + a manual run would make two pens).
    lockf = open(os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "boox-pen.lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("boox-pen is already running")

    node = a.node or find_node()
    ax = absinfo(node)
    (x0, x1), (y0, y1) = ax["ABS_X"], ax["ABS_Y"]
    rxw, ryh = x1 - x0, y1 - y0
    pos, tilt = ROT[a.rotate]
    geo = Geometry(a.rotate, rxw, ryh)
    W, H = geo.W, geo.H

    res = 100  # Tab Ultra C: ~209x157 mm over 20966x15725 units -> ~100 units/mm
    caps = {
        e.EV_KEY: [e.BTN_TOOL_PEN, e.BTN_TOOL_RUBBER, e.BTN_TOUCH, e.BTN_STYLUS, e.BTN_STYLUS2],
        e.EV_ABS: [
            # Fixed full-size range; the active area is scaled up to it, so switching screens
            # (different aspect) needs no new device.
            (e.ABS_X, AbsInfo(0, 0, int(W), 0, 0, res)),
            (e.ABS_Y, AbsInfo(0, 0, int(H), 0, 0, res)),
            (e.ABS_PRESSURE, AbsInfo(0, *ax["ABS_PRESSURE"], 0, 0, 0)),
            (e.ABS_DISTANCE, AbsInfo(0, *ax.get("ABS_DISTANCE", (0, 255)), 0, 0, 0)),
            (e.ABS_TILT_X, AbsInfo(0, *ax.get("ABS_TILT_X", (-63, 63)), 0, 0, 57)),  # degrees -> units/rad
            (e.ABS_TILT_Y, AbsInfo(0, *ax.get("ABS_TILT_Y", (-63, 63)), 0, 0, 57)),
        ],
    }
    ui = UInput(caps, name=DEVICE_NAME, vendor=0x056A, product=0x0001, version=1,
                input_props=[e.INPUT_PROP_POINTER])

    fixed = None
    if a.aspect == "off":
        fixed = W / H
    elif a.aspect:
        w, h = a.aspect.replace("x", ":").split(":")
        fixed = float(w) / float(h)
    STATE["screens"] = hypr_screens()
    STATE["can_switch"] = bool(STATE["screens"])
    if STATE["screens"]:
        time.sleep(0.3)  # let Hyprland register the new device before configuring it
        names = [s["name"] for s in STATE["screens"]]
        want = saved_screen()
        if want not in names:
            want = next((s["name"] for s in STATE["screens"] if s["focused"]), names[0])
        apply_screen(want, geo, fixed)
    else:
        asp = fixed or sysfs_aspect()
        if not asp:
            print("boox-pen: several monitors and no Hyprland; pass --aspect W:H (using full tablet)", flush=True)
        aw, ah = geo.set_aspect(asp)
        print(f"boox-pen: area {aw/res:.0f}x{ah/res:.0f}mm", flush=True)

    adb("shell", "svc", "power", "stayon", "usb")  # digitizer sleeps with the screen
    if not a.no_pad:
        threading.Thread(target=open_pad, args=(geo, fixed), daemon=True).start()

    p = subprocess.Popen(["adb", "exec-out", "cat", node], stdout=subprocess.PIPE, bufsize=0)
    fd = p.stdout.fileno()
    print(f"boox-pen: {node} -> {ui.device.path} rotate={a.rotate}", flush=True)

    # Physical pen state, updated per event; output decided once per SYN frame.
    rx, ry, tx, ty, pres, dist = x0, y0, 0, 0, 0, 0
    keys = {e.BTN_TOUCH: 0, e.BTN_STYLUS: 0, e.BTN_STYLUS2: 0}
    tool = None        # physical tool in proximity (BTN_TOOL_PEN / BTN_TOOL_RUBBER)
    synth = False      # device sent no tool key; we inferred one from ABS events
    out_tool = None    # tool the laptop currently thinks is in proximity
    margin = MARGIN_MM * res
    last = time.monotonic()

    def frame():
        nonlocal out_tool
        s, t = pos((rx - x0) / rxw, (ry - y0) / ryh)
        sx, ty_ = s * W, t * H
        aw, ah = geo.sw * W, geo.th * H
        inside = sx <= aw + margin and ty_ <= ah + margin and sx >= -margin and ty_ >= -margin
        want = tool if inside else None
        if out_tool != want:
            if out_tool:  # leave cleanly: lift, release buttons, out of proximity
                for k in keys: ui.write(e.EV_KEY, k, 0)
                ui.write(e.EV_ABS, e.ABS_PRESSURE, 0)
                ui.write(e.EV_KEY, out_tool, 0); ui.syn()
            if want:
                ui.write(e.EV_KEY, want, 1)
            out_tool = want
        if want:
            ui.write(e.EV_ABS, e.ABS_X, int(min(max(sx, 0), aw) / aw * W))
            ui.write(e.EV_ABS, e.ABS_Y, int(min(max(ty_, 0), ah) / ah * H))
            ui.write(e.EV_ABS, e.ABS_PRESSURE, pres)
            ui.write(e.EV_ABS, e.ABS_DISTANCE, dist)
            ox, oy = tilt(tx, ty)
            ui.write(e.EV_ABS, e.ABS_TILT_X, ox); ui.write(e.EV_ABS, e.ABS_TILT_Y, oy)
            for k, v in keys.items(): ui.write(e.EV_KEY, k, v)
            ui.syn()

    buf = b""
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 0.15)
            if not r:
                if synth and tool and time.monotonic() - last > 0.15:  # left proximity
                    tool, synth = None, False
                    keys[e.BTN_TOUCH] = 0; pres = 0
                    frame()
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
                    if v: tool, synth = c, False
                    elif tool == c: tool, synth = None, False
                elif t == e.EV_KEY and c in keys:
                    keys[c] = v
                elif t == e.EV_ABS:
                    if tool is None and c in (e.ABS_X, e.ABS_Y, e.ABS_PRESSURE):
                        tool, synth = e.BTN_TOOL_PEN, True
                    if c == e.ABS_X: rx = v
                    elif c == e.ABS_Y: ry = v
                    elif c == e.ABS_TILT_X: tx = v
                    elif c == e.ABS_TILT_Y: ty = v
                    elif c == e.ABS_PRESSURE: pres = v
                    elif c == e.ABS_DISTANCE: dist = v
                elif t == e.EV_SYN and c == e.SYN_REPORT:
                    frame()
            buf = buf[n:]
    except KeyboardInterrupt:
        pass
    finally:
        p.terminate(); ui.close()

if __name__ == "__main__":
    main()
