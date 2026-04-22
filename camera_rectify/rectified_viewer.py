import argparse
from pathlib import Path
from datetime import datetime

import cv2
import numpy as np


DEFAULT_SERIAL = "08520932"
WINDOW_NAME = "GigE Camera Rectified Viewer"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CALIB_PATH = SCRIPT_DIR / "camera_calib.yaml"
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
        description="Preview a TIS GigE camera and show rectified frames using OpenCV calibration YAML."
    )
    parser.add_argument(
        "--serial",
        default=DEFAULT_SERIAL,
        help=f"Camera serial number (default: {DEFAULT_SERIAL})",
    )
    parser.add_argument(
        "--mode",
        choices=["max", "fhd"],
        default="max",
        help="Capture mode to preview",
    )
    parser.add_argument(
        "--calib",
        type=Path,
        default=DEFAULT_CALIB_PATH,
        help=f"Path to camera calibration YAML (default: {DEFAULT_CALIB_PATH})",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=0.0,
        help="Alpha for getOptimalNewCameraMatrix in [0, 1]. 0 crops more, 1 preserves more FoV.",
    )
    parser.add_argument(
        "--show-raw",
        action="store_true",
        help="Show raw and rectified frames side by side.",
    )
    parser.add_argument(
        "--display-scale",
        type=float,
        default=1.0,
        help="Scale factor applied only to the displayed image. Capture/rectify/save remain at full resolution.",
    )
    parser.add_argument(
        "--window-width",
        type=int,
        default=None,
        help="Initial OpenCV window width in pixels. If omitted, use the displayed image width.",
    )
    parser.add_argument(
        "--window-height",
        type=int,
        default=None,
        help="Initial OpenCV window height in pixels. If omitted, use the displayed image height.",
    )
    return parser.parse_args()



def load_calib_yaml(yaml_path: Path) -> tuple[np.ndarray, np.ndarray, tuple[int | None, int | None]]:
    fs = cv2.FileStorage(str(yaml_path), cv2.FILE_STORAGE_READ)
    if not fs.isOpened():
        raise RuntimeError(f"Failed to open calib yaml: {yaml_path}")

    try:
        k_node = fs.getNode("K")
        dist_node = fs.getNode("dist")
        width_node = fs.getNode("image_width")
        height_node = fs.getNode("image_height")

        if k_node.empty() or dist_node.empty():
            raise RuntimeError("Invalid yaml: 'K' or 'dist' not found.")

        K = np.array(k_node.mat(), dtype=np.float64)
        dist = np.array(dist_node.mat(), dtype=np.float64).reshape(-1, 1)

        calib_w = int(width_node.real()) if not width_node.empty() else None
        calib_h = int(height_node.real()) if not height_node.empty() else None
        return K, dist, (calib_w, calib_h)
    finally:
        fs.release()



def build_undistort_maps(
    K: np.ndarray,
    dist: np.ndarray,
    image_size: tuple[int, int],
    alpha: float = 0.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    w, h = image_size
    new_K, _ = cv2.getOptimalNewCameraMatrix(K, dist, (w, h), alpha, (w, h))
    map1, map2 = cv2.initUndistortRectifyMap(
        K,
        dist,
        R=None,
        newCameraMatrix=new_K,
        size=(w, h),
        m1type=cv2.CV_16SC2,
    )
    return new_K, map1, map2



def save_capture(frame: np.ndarray) -> Path:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    save_path = CAPTURE_DIR / f"capture_{timestamp}.png"
    ok = cv2.imwrite(str(save_path), frame)
    if not ok:
        raise RuntimeError(f"failed to save capture: {save_path}")
    return save_path



def put_label(frame: np.ndarray, text: str) -> np.ndarray:
    out = frame.copy()
    cv2.putText(
        out,
        text,
        (16, 36),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return out



def stack_side_by_side(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    h = min(left.shape[0], right.shape[0])
    if left.shape[0] != h:
        left = cv2.resize(left, (int(left.shape[1] * h / left.shape[0]), h))
    if right.shape[0] != h:
        right = cv2.resize(right, (int(right.shape[1] * h / right.shape[0]), h))
    return np.hstack([left, right])



def resize_for_display(frame: np.ndarray, scale: float) -> np.ndarray:
    if np.isclose(scale, 1.0):
        return frame
    if scale <= 0:
        raise ValueError("display_scale must be > 0")

    h, w = frame.shape[:2]
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    return cv2.resize(frame, (new_w, new_h), interpolation=interpolation)



def main() -> None:
    args = parse_args()

    if args.display_scale <= 0:
        raise ValueError("--display-scale must be > 0")

    K, dist, calib_size = load_calib_yaml(args.calib)
    print("calib:", args.calib)
    print("K:\n", K)
    print("dist:", dist.ravel())
    print("calib_image_size:", calib_size)

    pipeline = make_pipeline(args.serial, args.mode)
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)

    print("script_dir:", SCRIPT_DIR)
    print("capture_dir:", CAPTURE_DIR)
    print("pipeline:", pipeline)
    print("opened:", cap.isOpened())
    print("display_scale:", args.display_scale)

    if not cap.isOpened():
        raise RuntimeError("failed to open camera via GStreamer/tcamsrc")

    map1 = None
    map2 = None
    map_size = None
    window_initialized = False

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                continue

            h, w = frame.shape[:2]
            current_size = (w, h)
            if map1 is None or map2 is None or map_size != current_size:
                _, map1, map2 = build_undistort_maps(K, dist, current_size, alpha=args.alpha)
                map_size = current_size
                print(f"built undistort maps for frame size: {w}x{h}")
                if calib_size[0] is not None and calib_size[1] is not None and calib_size != current_size:
                    print(
                        "[warn] calibration image size and capture size differ:",
                        f"calib={calib_size[0]}x{calib_size[1]}, capture={w}x{h}",
                    )

            rectified = cv2.remap(
                frame,
                map1,
                map2,
                interpolation=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
            )

            if args.show_raw:
                display = stack_side_by_side(
                    put_label(frame, "RAW"),
                    put_label(rectified, "RECTIFIED"),
                )
            else:
                display = put_label(rectified, "RECTIFIED")

            display = resize_for_display(display, args.display_scale)

            if not window_initialized:
                disp_h, disp_w = display.shape[:2]
                window_w = args.window_width if args.window_width is not None else disp_w
                window_h = args.window_height if args.window_height is not None else disp_h
                cv2.resizeWindow(WINDOW_NAME, window_w, window_h)
                print(f"window_size: {window_w}x{window_h}")
                window_initialized = True

            cv2.imshow(WINDOW_NAME, display)
            key = cv2.waitKey(1) & 0xFF

            if key == ord("c"):
                save_path = save_capture(rectified)
                print(f"saved rectified frame: {save_path}")

            if key in (27, ord("q")):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
