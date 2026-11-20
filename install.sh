#!/bin/sh
# Install boox-pen: uinput access, `boox-pen` on PATH, and autostart when this tablet is plugged in.
# Run with the tablet plugged in and USB debugging authorized. Uses sudo for the udev rules.
set -e
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

SERIAL="$(adb get-serialno 2>/dev/null || true)"
[ -n "$SERIAL" ] && [ "$SERIAL" != "unknown" ] || { echo "plug in the tablet and authorize USB debugging first"; exit 1; }
VID="$(for d in /sys/bus/usb/devices/*; do [ "$(cat "$d/serial" 2>/dev/null)" = "$SERIAL" ] && cat "$d/idVendor" && break; done)"

# 1. Let this user create virtual input devices.
sudo groupadd -f uinput
sudo usermod -aG uinput "$USER"
echo 'KERNEL=="uinput", MODE="0660", GROUP="uinput", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/60-uinput.rules >/dev/null

# 2. Start the user service when this tablet (by serial) appears.
echo "ACTION==\"add\", SUBSYSTEM==\"usb\", ENV{DEVTYPE}==\"usb_device\", ATTR{serial}==\"$SERIAL\", TAG+=\"systemd\", ENV{SYSTEMD_USER_WANTS}+=\"boox-pen.service\"" \
  | sudo tee /etc/udev/rules.d/61-boox-pen.rules >/dev/null
sudo udevadm control --reload
sudo udevadm trigger --name-match=uinput

# 3. The service: waits for adb to see the tablet, runs the bridge, ends when the tablet is unplugged.
mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/bin"
cat > "$HOME/.config/systemd/user/boox-pen.service" <<EOF
[Unit]
Description=BOOX pen as a drawing tablet (boox-pen)

[Service]
Type=simple
ExecStartPre=/usr/bin/timeout 30 /usr/bin/adb wait-for-usb-device
ExecStart=$DIR/run.sh
Restart=no
EOF
systemctl --user daemon-reload
ln -sf "$DIR/run.sh" "$HOME/.local/bin/boox-pen"

echo "installed: tablet $SERIAL (vendor ${VID:-?}). Unplug and replug to test; logs: journalctl --user -u boox-pen -f"
