# boox-pen

Use a BOOX e-ink tablet's pen as a native, pressure-sensitive drawing tablet on Linux, over USB.

No root, no app to install, no screen streaming. The laptop reads the tablet's raw Wacom digitizer
through `adb` and replays it into a virtual tablet via uinput. Blender (Grease Pencil), Krita and
anything libinput-based see a real pen: 4096 pressure levels, tilt, hover, eraser, side button.
In practice there's no perceptible lag.

Built for and tested on a BOOX Tab Ultra C (Ubuntu 26.04, Hyprland/Wayland). Other BOOX devices with
a Wacom EMR digitizer should work. The bridge finds the pen device by its `ABS_PRESSURE` axis.

## Why this works

`adb shell` runs as the `shell` user, which belongs to the `input` group on stock Android. That user
can read `/dev/input/eventN` directly, including the pen digitizer
(`onyx_emp_Wacom I2C Digitizer` on the Tab Ultra C). `adb exec-out cat /dev/input/eventN` streams the
raw `input_event` structs over USB, and Python/evdev re-emits them through `/dev/uinput`.

## Features

- **Aspect-correct area.** The active area is cropped to your monitor's aspect ratio, so circles stay
  round. It's anchored top-left, which leaves the spare strip at the bottom/right as a palm rest.
- **Palm zone.** Outside the active area (past a 3 mm margin) the pen is ignored on the laptop.
- **Pen pad page.** On start, the bridge opens a fullscreen blank page in Chrome on the tablet, served
  over `adb reverse`. The page swallows all pen and touch input so Android doesn't react to your strokes.
  After a few pen taps it calibrates itself (once per screen orientation), outlines the active area and
  greys out the palm zone.
- **Screen selector (Hyprland).** A selector in the palm strip picks which monitor the pen drives.
  Hold it for 1 s to unlock, then tap a screen. Two contacts or a palm-sized contact cancel the hold, and
  it relocks after 5 s. The choice is remembered in `~/.config/boox-pen/screen`, and the area refits to
  that monitor's aspect.
- **Starts on plug-in.** `install.sh` adds a udev rule for your tablet's serial that starts a systemd
  user service. The service exits when you unplug the tablet. Only one bridge runs at a time.
- **Rotation.** `--rotate` turns both position and tilt.
- **Keeps the tablet awake.** Runs `svc power stayon usb`, because the digitizer sleeps when the
  screen does.

## Setup

On the tablet: enable Developer options → USB debugging. Plug it in and accept the prompt. Then:

```sh
sudo apt install adb python3-evdev
./install.sh   # uinput access, `boox-pen` on PATH, autostart for this tablet (uses sudo)
```

Logs: `journalctl --user -u boox-pen -f`.

## Usage

```
boox-pen [--rotate 0|90|180|270] [--aspect 16:9|1920x1080|off] [--no-pad]
```

Normally it starts by itself when you plug the tablet in. Run it by hand for other options; Ctrl-C stops it. `run.sh` uses `sg uinput` so it works before you log out and back in for the
new group.

On Hyprland, screens are handled for you. On other compositors, pass `--aspect` and map the device
`Boox Pen (adb)` to one output in your compositor's config.

## Limits

- BOOX system gestures (edge swipes, the floating button) are handled above any app and still fire.
- Assumes an arm64 tablet (24-byte `input_event`) and roughly 100 sensor units/mm for the reported
  resolution.

## License

MIT
