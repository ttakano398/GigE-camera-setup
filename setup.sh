#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
VENV_DIR="$PROJECT_DIR/.venv"
SRC_DIR="$PROJECT_DIR/opencv-python"
BACKUP_SRC_DIR="$PROJECT_DIR/opencv-python-src"

echo "[1/7] Install system dependencies"
sudo apt update
sudo apt install -y \
    build-essential cmake pkg-config python3-dev \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    libgtk-3-dev

echo "[2/7] Recreate venv"
mkdir -p "$PROJECT_DIR"
rm -rf "$VENV_DIR"
python3 -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "[3/7] Install Python build tools"
pip install --upgrade pip wheel setuptools numpy scikit-build

echo "[4/7] Prepare opencv-python source"
rm -rf "$SRC_DIR"
if [[ -d "$BACKUP_SRC_DIR" ]]; then
    rm -rf "$BACKUP_SRC_DIR"
fi
git clone --recursive https://github.com/opencv/opencv-python.git "$SRC_DIR"
cd "$SRC_DIR"

echo "[5/7] Build wheel with GStreamer + GTK"
export CMAKE_ARGS="-DWITH_GSTREAMER=ON -DWITH_GTK=ON"
python -m pip wheel . --verbose

echo "[6/7] Install built wheel into venv"
ls -1 ./*.whl
pip install --force-reinstall ./opencv_python-*.whl

echo "[7/7] Move source tree aside and verify build"
cd "$PROJECT_DIR"
mv "$SRC_DIR" "$BACKUP_SRC_DIR"

python - <<'PY'
import sys
import cv2

print("python:", sys.executable)
print("cv2:", cv2.__file__)
for line in cv2.getBuildInformation().splitlines():
    if "GStreamer" in line or "GUI" in line or "GTK" in line:
        print(line)
PY

echo
echo "Done."
echo "Next: activate the venv and run your viewer script."
echo "  source $VENV_DIR/bin/activate"
echo "  python $PROJECT_DIR/viewer.py"
