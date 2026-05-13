#!/usr/bin/env bash
set -euo pipefail

# setup.sh
# Purpose:
#   Setup Python environment for this QR-code repository.
#
# Usage:
#   bash setup.sh
#
# Optional:
#   VENV_DIR=.venv bash setup.sh
#   PYTHON_BIN=python3.13 bash setup.sh
#
# Notes:
#   - pyzbar requires system package libzbar.
#   - torch 2.9.1 wheels include CUDA 12 runtime dependencies from pip.
#   - opencv-python and opencv-contrib-python are both installed because
#     this repository may use OpenCV's WeChatQRCode module.

VENV_DIR="${VENV_DIR:-.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3.13}"

echo "[INFO] Checking Python..."
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "[ERROR] ${PYTHON_BIN} not found."
    echo "        Install Python 3.13 or run with:"
    echo "        PYTHON_BIN=python3 bash setup.sh"
    exit 1
fi

PY_VER="$(${PYTHON_BIN} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "[INFO] Python version: ${PY_VER}"

if [[ "${PY_VER}" != "3.13" ]]; then
    echo "[WARN] This environment was recorded with Python 3.13.7."
    echo "       Current Python is ${PY_VER}. It may still work, but Python 3.13 is recommended."
fi

echo "[INFO] Installing system dependencies if apt is available..."
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y \
        python3-venv \
        python3-dev \
        build-essential \
        libzbar0 \
        ffmpeg \
        libgl1 \
        libglib2.0-0
else
    echo "[WARN] apt-get not found. Please install equivalent packages manually:"
    echo "       - libzbar0 / zbar"
    echo "       - ffmpeg"
    echo "       - OpenGL runtime libraries"
fi

echo "[INFO] Creating virtual environment: ${VENV_DIR}"
"${PYTHON_BIN}" -m venv "${VENV_DIR}"

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

echo "[INFO] Upgrading pip/setuptools/wheel..."
python -m pip install --upgrade pip setuptools wheel

echo "[INFO] Installing Python packages..."

python -m pip install \
    certifi==2025.11.12 \
    charset-normalizer==3.4.4 \
    contourpy==1.3.3 \
    cycler==0.12.1 \
    filelock==3.20.0 \
    fonttools==4.60.1 \
    fsspec==2025.10.0 \
    idna==3.11 \
    Jinja2==3.1.6 \
    kiwisolver==1.4.9 \
    MarkupSafe==3.0.3 \
    matplotlib==3.10.7 \
    mpmath==1.3.0 \
    networkx==3.5 \
    numpy==2.2.6 \
    opencv-python==4.12.0.88 \
    opencv-contrib-python==4.12.0.88 \
    packaging==25.0 \
    pillow==12.0.0 \
    polars==1.35.2 \
    polars-runtime-32==1.35.2 \
    psutil==7.1.3 \
    pyparsing==3.2.5 \
    pypng==0.20220715.0 \
    PyQRCode==1.2.1 \
    python-dateutil==2.9.0.post0 \
    PyYAML==6.0.3 \
    pyzbar==0.1.9 \
    qrdet==2.5 \
    qreader==3.16 \
    quadrilateral-fitter==1.12 \
    requests==2.32.5 \
    scipy==1.16.3 \
    setuptools==80.9.0 \
    shapely==2.1.2 \
    six==1.17.0 \
    sympy==1.14.0 \
    tqdm==4.67.1 \
    typing_extensions==4.15.0 \
    ultralytics==8.3.228 \
    ultralytics-thop==2.0.18 \
    urllib3==2.5.0

echo "[INFO] Installing PyTorch..."
python -m pip install \
    torch==2.9.1 \
    torchvision==0.24.1 \
    triton==3.5.1

echo "[INFO] Checking imports..."
python - <<'PY'
import sys
print("[CHECK] python:", sys.version)

import cv2
print("[CHECK] cv2:", cv2.__version__)

import numpy as np
print("[CHECK] numpy:", np.__version__)

import torch
print("[CHECK] torch:", torch.__version__)
print("[CHECK] cuda available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("[CHECK] cuda device:", torch.cuda.get_device_name(0))

from pyzbar import pyzbar
print("[CHECK] pyzbar: ok")

import qreader
print("[CHECK] qreader: ok")

import ultralytics
print("[CHECK] ultralytics:", ultralytics.__version__)

print("[CHECK] setup complete")
PY

echo ""
echo "[INFO] Done."
echo "To activate:"
echo "  source ${VENV_DIR}/bin/activate"