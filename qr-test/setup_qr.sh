#!/usr/bin/env bash
set -euo pipefail

# Add QR-reader dependencies to the existing GigE venv created by ../setup.sh.
# This script intentionally does not install opencv-python/opencv-contrib-python,
# because viewer_qr.py needs the GStreamer-enabled OpenCV built by the root setup.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/.venv}"
INSTALL_SYSTEM_DEPS=1
INSTALL_QREADER=1

usage() {
    cat <<USAGE
Usage: $(basename "$0") [--venv PATH] [--skip-system-deps] [--no-qreader] [-h|--help]

Installs QR dependencies into an existing venv created by the repository root setup.sh.

Options:
  --venv PATH          Target venv path (default: $PROJECT_DIR/.venv)
  --skip-system-deps   Do not try to install OS packages such as libzbar0
  --no-qreader         Install lightweight QR dependencies only; skip torch/qreader
  -h, --help           Show this help

Environment:
  VENV_DIR             Alternative target venv path
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --venv)
            if [[ $# -lt 2 ]]; then
                echo "[ERROR] --venv requires a path" >&2
                exit 1
            fi
            VENV_DIR="$2"
            shift 2
            ;;
        --skip-system-deps)
            INSTALL_SYSTEM_DEPS=0
            shift
            ;;
        --no-qreader)
            INSTALL_QREADER=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown option: $1" >&2
            usage
            exit 1
            ;;
    esac
done

PYTHON_BIN="$VENV_DIR/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
    cat >&2 <<EOF
[ERROR] Existing venv was not found: $VENV_DIR

Create the GigE runtime first from the repository root:
  cd "$PROJECT_DIR"
  ./setup.sh

Then rerun:
  ./qr-test/setup_qr.sh
EOF
    exit 1
fi

echo "[INFO] Project: $PROJECT_DIR"
echo "[INFO] Target venv: $VENV_DIR"

if [[ "$INSTALL_SYSTEM_DEPS" -eq 1 ]]; then
    echo "[INFO] Installing system dependencies when apt-get is available..."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y \
            libzbar0 \
            ffmpeg \
            libgl1 \
            libglib2.0-0
    else
        echo "[WARN] apt-get not found. Install equivalent packages manually if needed:"
        echo "       - zbar/libzbar (required by pyzbar)"
        echo "       - ffmpeg"
        echo "       - OpenGL runtime libraries"
    fi
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "[INFO] Checking existing OpenCV runtime..."
python - <<'PY'
import sys

try:
    import cv2
except Exception as exc:
    raise SystemExit(
        "[ERROR] cv2 is not importable from this venv. "
        "Run the repository root setup.sh first."
    ) from exc

print("[CHECK] python:", sys.executable)
print("[CHECK] cv2:", cv2.__file__)
print("[CHECK] cv2 version:", cv2.__version__)
print("[CHECK] QRCodeDetector:", hasattr(cv2, "QRCodeDetector"))

gstreamer_lines = [
    line.strip()
    for line in cv2.getBuildInformation().splitlines()
    if line.strip().startswith("GStreamer:")
]
if gstreamer_lines:
    print("[CHECK]", gstreamer_lines[0])
    if "YES" not in gstreamer_lines[0]:
        print("[WARN] viewer_qr.py needs OpenCV with GStreamer support for GigE input.")
else:
    print("[WARN] Could not find the OpenCV GStreamer build flag.")
PY

echo "[INFO] Upgrading pip/setuptools/wheel..."
python -m pip install --upgrade pip setuptools wheel

echo "[INFO] Installing lightweight QR dependencies..."
python -m pip install --upgrade-strategy only-if-needed \
    "pyzbar>=0.1.9,<0.2" \
    "pypng>=0.20220715.0" \
    "PyQRCode>=1.2.1,<2" \
    "pillow>=10,<13" \
    "requests>=2.31,<3" \
    "tqdm>=4.66,<5" \
    "PyYAML>=6,<7" \
    "psutil>=5,<8" \
    "polars>=1,<2" \
    "scipy>=1.10,<2" \
    "shapely>=2,<3" \
    "quadrilateral-fitter>=1.12,<2" \
    "matplotlib>=3.7,<3.11" \
    "contourpy>=1.2,<1.4" \
    "cycler>=0.12,<0.13" \
    "fonttools>=4.50,<5" \
    "kiwisolver>=1.4,<2" \
    "pyparsing>=3.1,<4" \
    "python-dateutil>=2.9,<3" \
    "six>=1.16,<2" \
    "packaging>=23,<26" \
    "typing_extensions>=4.10,<5"

if [[ "$INSTALL_QREADER" -eq 1 ]]; then
    echo "[INFO] Installing QReader/YOLO support packages..."
    python -m pip install --upgrade-strategy only-if-needed \
        "torch>=2.1,<3" \
        "torchvision>=0.16,<1" \
        "ultralytics-thop>=2.0,<3"

    echo "[INFO] Installing QR packages without dependencies to preserve the custom OpenCV build..."
    python -m pip install --no-deps \
        "ultralytics>=8.3,<9" \
        "qrdet>=2.5,<3" \
        "qreader>=3.16,<4"
else
    echo "[INFO] Skipping QReader/YOLO packages (--no-qreader)."
fi

echo "[INFO] Verifying QR imports..."
if [[ "$INSTALL_QREADER" -eq 1 ]]; then
    python - <<'PY'
import cv2
from pyzbar import pyzbar
import qreader
import torch
import ultralytics

print("[CHECK] cv2:", cv2.__version__)
print("[CHECK] pyzbar: ok")
print("[CHECK] qreader:", getattr(qreader, "__version__", "ok"))
print("[CHECK] torch:", torch.__version__)
print("[CHECK] ultralytics:", ultralytics.__version__)
for line in cv2.getBuildInformation().splitlines():
    if "GStreamer" in line:
        print("[CHECK]", line.strip())
PY
else
    python - <<'PY'
import cv2
from pyzbar import pyzbar

print("[CHECK] cv2:", cv2.__version__)
print("[CHECK] pyzbar: ok")
for line in cv2.getBuildInformation().splitlines():
    if "GStreamer" in line:
        print("[CHECK]", line.strip())
PY
fi

echo ""
echo "[INFO] Done."
echo "Run:"
echo "  source \"$VENV_DIR/bin/activate\""
echo "  python \"$PROJECT_DIR/qr-test/viewer_qr.py\""
