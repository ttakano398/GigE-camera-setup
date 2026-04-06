import argparse
from pathlib import Path

import cv2

DEFAULT_SERIAL = "08520932"
WINDOW_NAME = "GigE Camera Viewer"
SCRIPT_DIR = Path(__file__).resolve().parent


def make_pipeline(serial: str) -> str:
    return (
        f"tcamsrc serial={serial} ! "
        "videoconvert ! "
        "video/x-raw,format=BGR ! "
        "appsink sync=false drop=true max-buffers=1"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview a TIS GigE camera through OpenCV + GStreamer."
    )
    parser.add_argument(
        "--serial",
        default=DEFAULT_SERIAL,
        help=f"Camera serial number (default: {DEFAULT_SERIAL})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pipeline = make_pipeline(args.serial)
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)

    print("script_dir:", SCRIPT_DIR)
    print("pipeline:", pipeline)
    print("opened:", cap.isOpened())

    if not cap.isOpened():
        raise RuntimeError("failed to open camera via GStreamer/tcamsrc")

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                continue

            cv2.imshow(WINDOW_NAME, frame)
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
