#!/bin/sh
# Start the Boox pen bridge. `sg uinput` grants /dev/uinput access until you re-log in.
cd "$(dirname "$(readlink -f "$0")")"
exec sg uinput -c "/usr/bin/python3 -u ./boox-pen.py $*"
