import argparse
from pathlib import Path
from datetime import datetime

import cv2


DEFAULT_SERIAL = "08520932"
WINDOW_NAME = "GigE Camera Viewer"
SCRIPT_DIR = Path(__file__).resolve().parent
CAPTURE_DIR = SCRIPT_DIR / "camera_rectify" / "capture"

MODE_CAPS = {
    "max": "video/x-bayer,format=grbg,width=2592,height=1944,framerate=22/1",
    "fhd": "video/x-bayer,format=grbg,width=1920,height=1080,framerate=30/1",
}


def make_pipeline(serial: str, mode: str) -> str:
    caps = MODE_CAPS[mode]
    return (
        f'tcamsrc serial="{serial}" type=aravis ! '
        f"{caps} ! "
        "bayer2rgb ! "
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
    parser.add_argument(
        "--mode",
        choices=["max", "fhd"],
        default="fhd",
        help="Capture mode to preview",
    )
    parser.add_argument(
        "--display-scale",
        type=float,
        default=1.0,
        help="Scale factor applied only to the displayed image. Capture is saved at original resolution.",
    )
    parser.add_argument(
        "--window-width",
        type=int,
        default=None,
        help="Initial window width in pixels.",
    )
    parser.add_argument(
        "--window-height",
        type=int,
        default=None,
        help="Initial window height in pixels.",
    )
    args = parser.parse_args()

    if args.display_scale <= 0:
        parser.error("--display-scale must be > 0")
    if (args.window_width is None) != (args.window_height is None):
        parser.error("--window-width and --window-height must be specified together")

    return args


def save_capture(frame) -> Path:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    save_path = CAPTURE_DIR / f"capture_{timestamp}.png"
    ok = cv2.imwrite(str(save_path), frame)
    if not ok:
        raise RuntimeError(f"failed to save capture: {save_path}")
    return save_path


def resize_for_display(frame, display_scale: float):
    if display_scale == 1.0:
        return frame
    h, w = frame.shape[:2]
    disp_w = max(1, int(round(w * display_scale)))
    disp_h = max(1, int(round(h * display_scale)))
    return cv2.resize(frame, (disp_w, disp_h), interpolation=cv2.INTER_AREA)


def main() -> None:
    args = parse_args()
    pipeline = make_pipeline(args.serial, args.mode)
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)

    print("script_dir:", SCRIPT_DIR)
    print("capture_dir:", CAPTURE_DIR)
    print("pipeline:", pipeline)
    print("opened:", cap.isOpened())
    print("display_scale:", args.display_scale)
    if args.window_width is not None and args.window_height is not None:
        print("window_size:", f"{args.window_width}x{args.window_height}")

    if not cap.isOpened():
        raise RuntimeError("failed to open camera via GStreamer/tcamsrc")

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    if args.window_width is not None and args.window_height is not None:
        cv2.resizeWindow(WINDOW_NAME, args.window_width, args.window_height)

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                continue

            display_frame = resize_for_display(frame, args.display_scale)
            cv2.imshow(WINDOW_NAME, display_frame)
            key = cv2.waitKey(1) & 0xFF

            if key == ord("c"):
                save_path = save_capture(frame)
                print(f"saved: {save_path}")

            if key in (27, ord("q")):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
