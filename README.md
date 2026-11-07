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
- **Pen pad page.** On start, the bridge opens a fullscreen blank page in Chrome on the tablet, served
  over `adb reverse`. The page swallows all pen and touch input so Android doesn't react to your strokes.
  After a few pen taps it calibrates itself, outlines the active area and greys out the palm zone.
- **Rotation.** `--rotate` turns both position and tilt.
- **Keeps the tablet awake.** Runs `svc power stayon usb`, because the digitizer sleeps when the
  screen does.

## Setup

```sh
sudo apt install adb python3-evdev
# allow your user to create virtual input devices
sudo groupadd -f uinput && sudo usermod -aG uinput $USER
echo 'KERNEL=="uinput", MODE="0660", GROUP="uinput", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/60-uinput.rules
sudo udevadm control --reload && sudo udevadm trigger
ln -s "$PWD/run.sh" ~/.local/bin/boox-pen
```

On the tablet: enable Developer options → USB debugging. Plug it in and accept the prompt.

## Usage

```
boox-pen [--rotate 0|90|180|270] [--aspect 16:9|1920x1080|off] [--no-pad]
```

Ctrl-C stops it. `run.sh` uses `sg uinput` so it works before you log out and back in for the
new group.

With multiple monitors, pass `--aspect` and map the tablet to one output in your compositor, for
example in Hyprland: `input { tablet { output = eDP-1 } }`.

## Limits

- BOOX system gestures (edge swipes, the floating button) are handled above any app and still fire.
- Assumes an arm64 tablet (24-byte `input_event`) and roughly 100 sensor units/mm for the reported
  resolution.
- Chrome may open a new tab each start.

## License

MIT
