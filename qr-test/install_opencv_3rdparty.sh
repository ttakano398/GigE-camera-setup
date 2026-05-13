#!/usr/bin/env bash
set -euo pipefail

# Download WeChat QRCode model files used by OpenCV's WeChatQRCode detector.
# These files are data/model assets, not Python packages. They are stored under
# qr-test/opencv_3rdparty/ so viewer_qr.py can enable the "wechat" algorithm.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_DIR="${MODEL_DIR:-$SCRIPT_DIR/opencv_3rdparty}"
BASE_URL="${BASE_URL:-https://raw.githubusercontent.com/WeChatCV/opencv_3rdparty/wechat_qrcode}"
FORCE=0

FILES=(
    "detect.prototxt"
    "detect.caffemodel"
    "sr.prototxt"
    "sr.caffemodel"
)

usage() {
    cat <<USAGE
Usage: $(basename "$0") [--model-dir PATH] [--force] [-h|--help]

Downloads OpenCV WeChatQRCode model files into qr-test/opencv_3rdparty.

Options:
  --model-dir PATH   Destination directory (default: $MODEL_DIR)
  --force            Re-download files even if they already exist
  -h, --help         Show this help

Environment:
  MODEL_DIR          Alternative destination directory
  BASE_URL           Alternative raw model base URL
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model-dir)
            if [[ $# -lt 2 ]]; then
                echo "[ERROR] --model-dir requires a path" >&2
                exit 1
            fi
            MODEL_DIR="$2"
            shift 2
            ;;
        --force)
            FORCE=1
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

if command -v curl >/dev/null 2>&1; then
    download() {
        local url="$1"
        local output="$2"
        curl --fail --location --show-error --progress-bar "$url" --output "$output"
    }
elif command -v wget >/dev/null 2>&1; then
    download() {
        local url="$1"
        local output="$2"
        wget --output-document="$output" "$url"
    }
else
    echo "[ERROR] curl or wget is required." >&2
    exit 1
fi

mkdir -p "$MODEL_DIR"

echo "[INFO] Destination: $MODEL_DIR"
echo "[INFO] Source: $BASE_URL"

for file in "${FILES[@]}"; do
    dest="$MODEL_DIR/$file"
    tmp="$dest.tmp"
    url="$BASE_URL/$file"

    if [[ "$FORCE" -eq 0 && -s "$dest" ]]; then
        echo "[SKIP] $file already exists"
        continue
    fi

    echo "[GET]  $file"
    rm -f "$tmp"
    download "$url" "$tmp"

    if [[ ! -s "$tmp" ]]; then
        echo "[ERROR] Downloaded file is empty: $file" >&2
        rm -f "$tmp"
        exit 1
    fi

    mv "$tmp" "$dest"
done

echo "[INFO] Verifying files..."
for file in "${FILES[@]}"; do
    path="$MODEL_DIR/$file"
    if [[ ! -s "$path" ]]; then
        echo "[ERROR] Missing or empty file: $path" >&2
        exit 1
    fi
    size="$(wc -c < "$path" | tr -d ' ')"
    echo "[CHECK] $file ($size bytes)"
done

echo ""
echo "[INFO] Done."
echo "WeChat QRCode should now be available in:"
echo "  python \"$SCRIPT_DIR/viewer_qr.py\" --algorithm wechat"
