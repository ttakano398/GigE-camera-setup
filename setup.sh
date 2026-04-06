#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
VENV_DIR="$PROJECT_DIR/.venv"

echo "[1/4] Install runtime dependencies for B path"
sudo apt update
sudo apt install -y \
    python3-venv python3-gi python3-gst-1.0 \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad

echo "[2/4] Recreate venv"
mkdir -p "$PROJECT_DIR"
rm -rf "$VENV_DIR"
python3 -m venv --system-site-packages "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "[3/4] Install lightweight Python helpers"
pip install --upgrade pip wheel setuptools

echo "[4/4] Verify current environment"
python - <<'PY'
import sys
import cv2
import gi

gi.require_version("Gst", "1.0")
gi.require_version("Tcam", "1.0")
from gi.repository import Gst

print("python:", sys.executable)
print("cv2:", cv2.__file__)
print("cv2 version:", cv2.__version__)
print("cv2.aruco available:", hasattr(cv2, "aruco"))

for line in cv2.getBuildInformation().splitlines():
    if "GStreamer" in line or "GUI" in line or "GTK" in line:
        print(line)

Gst.init(None)
print("gi/Gst/Tcam: ok")
PY

echo
echo "Done."
echo "This setup.sh is for the B path and does not build OpenCV."
echo "If the checks above fail, use setup-opencv.sh and README-opencv.md for the A/OpenCV-build path."
echo
echo "Next:"
echo "  source $VENV_DIR/bin/activate"
echo "  python $PROJECT_DIR/viewer.py --serial 08520932 --mode fhd"
echo "  python $PROJECT_DIR/continue_calib-gige.py --serial 08520932 --mode fhd --marker aruco"
