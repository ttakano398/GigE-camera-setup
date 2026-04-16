#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
VENV_DIR="$PROJECT_DIR/.venv"
SRC_DIR="$PROJECT_DIR/opencv-python"
BACKUP_SRC_DIR="$PROJECT_DIR/opencv-python-src"
WITH_OPENCV_BUILD=1
SYSTEM_PYTHON="/usr/bin/python3"
NUMPY_SPEC="numpy<2"
OPENCV_CONTRIB_SPEC="opencv-contrib-python<4.13"

if [[ -x "$SYSTEM_PYTHON" ]]; then
    PYTHON_BIN="$SYSTEM_PYTHON"
else
    PYTHON_BIN="$(command -v python3)"
fi

usage() {
    cat <<USAGE
Usage: $(basename "$0") [--with-opencv-build] [-h|--help]

Default:
  Build OpenCV from source with GStreamer + GTK enabled.

Options:
  --with-opencv-build   Build opencv-python wheel with GStreamer + GTK enabled
  -h, --help            Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-opencv-build) WITH_OPENCV_BUILD=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 1
            ;;
    esac
done

echo "[1/5] Install system dependencies"
sudo apt update
sudo apt install -y python3-dev python3-venv \
    python3-gi python3-gst-1.0 \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad

if [[ "$WITH_OPENCV_BUILD" -eq 1 ]]; then
    echo "[1/5] Install OpenCV build dependencies"
    sudo apt install -y \
        build-essential cmake pkg-config \
        libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
        libgtk-3-dev
fi

echo "[2/5] Recreate venv"
mkdir -p "$PROJECT_DIR"
rm -rf "$VENV_DIR"
echo "Using python: $PYTHON_BIN"
"$PYTHON_BIN" -m venv --system-site-packages "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "[3/5] Install Python packages"
pip install --upgrade pip wheel setuptools "$NUMPY_SPEC" "$OPENCV_CONTRIB_SPEC"

if [[ "$WITH_OPENCV_BUILD" -eq 1 ]]; then
    echo "[4/5] Prepare opencv-python source"
    pip install --upgrade scikit-build
    rm -rf "$SRC_DIR"
    if [[ -d "$BACKUP_SRC_DIR" ]]; then
        rm -rf "$BACKUP_SRC_DIR"
    fi
    git clone --recursive https://github.com/opencv/opencv-python.git "$SRC_DIR"
    cd "$SRC_DIR"

    echo "[5/5] Build wheel with GStreamer + GTK"
    export ENABLE_CONTRIB=1
    export CMAKE_ARGS="-DWITH_GSTREAMER=ON -DWITH_GTK=ON"
    python -m pip wheel . --verbose

    echo "[5/5] Install built wheel into venv"
    ls -1 ./*.whl
    if ls -1 ./opencv_contrib_python-*.whl >/dev/null 2>&1; then
        pip install --force-reinstall ./opencv_contrib_python-*.whl
    else
        pip install --force-reinstall ./opencv_python-*.whl
    fi
    pip install --force-reinstall "$NUMPY_SPEC"

    echo "[5/5] Move source tree aside"
    cd "$PROJECT_DIR"
    mv "$SRC_DIR" "$BACKUP_SRC_DIR"
fi

echo "[5/5] Verify runtime"

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
print("has cv2.aruco:", hasattr(cv2, "aruco"))
for line in cv2.getBuildInformation().splitlines():
    if "GStreamer" in line or "GUI" in line or "GTK" in line:
        print(line)

Gst.init(None)
print("gi/Gst/Tcam: ok")
PY

echo
echo "Done."
echo "Next:"
echo "  source $VENV_DIR/bin/activate"
if [[ "$WITH_OPENCV_BUILD" -eq 1 ]]; then
    echo "  python $PROJECT_DIR/viewer.py --serial 08520932"
else
    echo "  # viewer.py needs cv2 GStreamer backend; use --with-opencv-build if needed"
fi
echo "  python $PROJECT_DIR/continue_calib-gige.py --serial 08520932 --mode fhd --marker aruco"
