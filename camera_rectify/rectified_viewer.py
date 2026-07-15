import argparse
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime

import cv2
import numpy as np


DEFAULT_SERIAL = "16620659"
WINDOW_NAME = "GigE Camera Calibration Comparison"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CALIB_PATH = SCRIPT_DIR / "camera_calib.yaml"
CAPTURE_DIR = SCRIPT_DIR / "camera_rectify" / "capture"

MAX_PANELS = 4

MODE_CAPS = {
    "max": "video/x-bayer,format=grbg,width=2592,height=1944,framerate=22/1",
    "fhd": "video/x-bayer,format=grbg,width=1920,height=1080,framerate=30/1",
}


@dataclass
class Calibration:
    path: Path
    K: np.ndarray
    dist: np.ndarray
    image_size: tuple[int | None, int | None]
    map1: np.ndarray | None = None
    map2: np.ndarray | None = None
    map_size: tuple[int, int] | None = None


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
        description=(
            "Preview a TIS GigE camera and compare multiple "
            "camera calibration YAML files."
        )
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
        nargs="+",
        default=[DEFAULT_CALIB_PATH],
        help=(
            "Calibration YAML files. "
            "Up to 3 files can be specified because RAW is also displayed."
        ),
    )

    parser.add_argument(
        "--alpha",
        type=float,
        default=0.0,
        help=(
            "Alpha for getOptimalNewCameraMatrix in [0, 1]. "
            "0 crops more, 1 preserves more FoV."
        ),
    )

    parser.add_argument(
        "--display-scale",
        type=float,
        default=1.0,
        help=(
            "Scale factor applied only to the combined display. "
            "For a 2x2 max-resolution view, 0.25 or 0.3 is recommended."
        ),
    )

    parser.add_argument(
        "--window-width",
        type=int,
        default=None,
        help="Initial OpenCV window width in pixels.",
    )

    parser.add_argument(
        "--window-height",
        type=int,
        default=None,
        help="Initial OpenCV window height in pixels.",
    )

    return parser.parse_args()


def load_calib_yaml(
    yaml_path: Path,
) -> tuple[np.ndarray, np.ndarray, tuple[int | None, int | None]]:

    fs = cv2.FileStorage(str(yaml_path), cv2.FILE_STORAGE_READ)

    if not fs.isOpened():
        raise RuntimeError(f"Failed to open calib yaml: {yaml_path}")

    try:
        k_node = fs.getNode("K")
        dist_node = fs.getNode("dist")
        width_node = fs.getNode("image_width")
        height_node = fs.getNode("image_height")

        if k_node.empty() or dist_node.empty():
            raise RuntimeError(
                f"Invalid yaml: 'K' or 'dist' not found: {yaml_path}"
            )

        K = np.array(k_node.mat(), dtype=np.float64)
        dist = np.array(dist_node.mat(), dtype=np.float64).reshape(-1, 1)

        calib_w = (
            int(width_node.real())
            if not width_node.empty()
            else None
        )

        calib_h = (
            int(height_node.real())
            if not height_node.empty()
            else None
        )

        return K, dist, (calib_w, calib_h)

    finally:
        fs.release()


def scale_camera_matrix(
    K: np.ndarray,
    calib_size: tuple[int | None, int | None],
    image_size: tuple[int, int],
) -> np.ndarray:
    """
    Scale camera intrinsics when calibration resolution and
    current capture resolution differ.
    """
    calib_w, calib_h = calib_size
    image_w, image_h = image_size

    if calib_w is None or calib_h is None:
        return K.copy()

    if (calib_w, calib_h) == image_size:
        return K.copy()

    scale_x = image_w / calib_w
    scale_y = image_h / calib_h

    scaled_K = K.copy()

    # fx, skew, cx
    scaled_K[0, :] *= scale_x

    # fy, cy
    scaled_K[1, :] *= scale_y

    # Keep homogeneous coordinate unchanged
    scaled_K[2, :] = K[2, :]

    return scaled_K


def build_undistort_maps(
    K: np.ndarray,
    dist: np.ndarray,
    calib_size: tuple[int | None, int | None],
    image_size: tuple[int, int],
    alpha: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:

    w, h = image_size

    K_for_image = scale_camera_matrix(
        K,
        calib_size,
        image_size,
    )

    new_K, _ = cv2.getOptimalNewCameraMatrix(
        K_for_image,
        dist,
        (w, h),
        alpha,
        (w, h),
    )

    map1, map2 = cv2.initUndistortRectifyMap(
        K_for_image,
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
    save_path = CAPTURE_DIR / f"comparison_{timestamp}.png"

    ok = cv2.imwrite(str(save_path), frame)

    if not ok:
        raise RuntimeError(
            f"Failed to save capture: {save_path}"
        )

    return save_path


def put_label(
    frame: np.ndarray,
    text: str,
) -> np.ndarray:

    out = frame.copy()

    # Shadow for readability
    cv2.putText(
        out,
        text,
        (18, 38),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (0, 0, 0),
        4,
        cv2.LINE_AA,
    )

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


def make_grid(frames: list[np.ndarray]) -> np.ndarray:
    """
    Layout:
        1 panel -> 1x1
        2 panels -> 1x2
        3 panels -> 2x2 + one blank
        4 panels -> 2x2
    """

    if not frames:
        raise ValueError("frames must not be empty")

    if len(frames) > MAX_PANELS:
        raise ValueError(
            f"Maximum number of panels is {MAX_PANELS}"
        )

    if len(frames) == 1:
        return frames[0]

    if len(frames) == 2:
        return np.hstack(frames)

    # 3 or 4 panels -> 2x2
    while len(frames) < 4:
        frames.append(
            np.zeros_like(frames[0])
        )

    top = np.hstack([
        frames[0],
        frames[1],
    ])

    bottom = np.hstack([
        frames[2],
        frames[3],
    ])

    return np.vstack([
        top,
        bottom,
    ])


def resize_for_display(
    frame: np.ndarray,
    scale: float,
) -> np.ndarray:

    if np.isclose(scale, 1.0):
        return frame

    if scale <= 0:
        raise ValueError(
            "display_scale must be > 0"
        )

    h, w = frame.shape[:2]

    new_w = max(
        1,
        int(round(w * scale)),
    )

    new_h = max(
        1,
        int(round(h * scale)),
    )

    interpolation = (
        cv2.INTER_AREA
        if scale < 1.0
        else cv2.INTER_LINEAR
    )

    return cv2.resize(
        frame,
        (new_w, new_h),
        interpolation=interpolation,
    )


def main() -> None:
    args = parse_args()

    if args.display_scale <= 0:
        raise ValueError(
            "--display-scale must be > 0"
        )

    if not 0.0 <= args.alpha <= 1.0:
        raise ValueError(
            "--alpha must be in [0, 1]"
        )

    # RAW occupies one panel.
    if len(args.calib) + 1 > MAX_PANELS:
        raise ValueError(
            f"At most {MAX_PANELS - 1} calibration YAML files "
            f"can be specified because RAW occupies one panel."
        )

    calibrations: list[Calibration] = []

    for yaml_path in args.calib:
        K, dist, calib_size = load_calib_yaml(
            yaml_path
        )

        calibration = Calibration(
            path=yaml_path,
            K=K,
            dist=dist,
            image_size=calib_size,
        )

        calibrations.append(
            calibration
        )

        print()
        print("=" * 60)
        print("calib:", yaml_path)
        print("K:")
        print(K)
        print("dist:", dist.ravel())
        print("calib image size:", calib_size)

    pipeline = make_pipeline(
        args.serial,
        args.mode,
    )

    cap = cv2.VideoCapture(
        pipeline,
        cv2.CAP_GSTREAMER,
    )

    print()
    print("=" * 60)
    print("script_dir:", SCRIPT_DIR)
    print("capture_dir:", CAPTURE_DIR)
    print("pipeline:", pipeline)
    print("opened:", cap.isOpened())
    print("display_scale:", args.display_scale)
    print("alpha:", args.alpha)
    print("number of panels:", len(calibrations) + 1)

    if not cap.isOpened():
        raise RuntimeError(
            "Failed to open camera via GStreamer/tcamsrc"
        )

    window_initialized = False

    cv2.namedWindow(
        WINDOW_NAME,
        cv2.WINDOW_NORMAL,
    )

    try:
        while True:

            ok, frame = cap.read()

            if not ok:
                continue

            h, w = frame.shape[:2]
            current_size = (w, h)

            panels = [
                put_label(
                    frame,
                    "RAW / NO CALIBRATION",
                )
            ]

            for calibration in calibrations:

                # Rebuild maps only when image size changes.
                if (
                    calibration.map1 is None
                    or calibration.map2 is None
                    or calibration.map_size != current_size
                ):

                    _, map1, map2 = build_undistort_maps(
                        K=calibration.K,
                        dist=calibration.dist,
                        calib_size=calibration.image_size,
                        image_size=current_size,
                        alpha=args.alpha,
                    )

                    calibration.map1 = map1
                    calibration.map2 = map2
                    calibration.map_size = current_size

                    print(
                        f"Built undistort maps: "
                        f"{calibration.path.name} "
                        f"for {w}x{h}"
                    )

                    calib_w, calib_h = (
                        calibration.image_size
                    )

                    if (
                        calib_w is not None
                        and calib_h is not None
                        and calibration.image_size
                        != current_size
                    ):
                        print(
                            "[info] calibration image size differs "
                            "from capture size; "
                            "K was automatically scaled:",
                            f"calib={calib_w}x{calib_h}, "
                            f"capture={w}x{h}",
                        )

                rectified = cv2.remap(
                    frame,
                    calibration.map1,
                    calibration.map2,
                    interpolation=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT,
                )

                label = calibration.path.stem

                panels.append(
                    put_label(
                        rectified,
                        label,
                    )
                )

            # Full-resolution comparison image
            comparison = make_grid(
                panels.copy()
            )

            # Resize only for monitor display
            display = resize_for_display(
                comparison,
                args.display_scale,
            )

            if not window_initialized:

                disp_h, disp_w = (
                    display.shape[:2]
                )

                window_w = (
                    args.window_width
                    if args.window_width is not None
                    else disp_w
                )

                window_h = (
                    args.window_height
                    if args.window_height is not None
                    else disp_h
                )

                cv2.resizeWindow(
                    WINDOW_NAME,
                    window_w,
                    window_h,
                )

                print(
                    f"window_size: "
                    f"{window_w}x{window_h}"
                )

                window_initialized = True

            cv2.imshow(
                WINDOW_NAME,
                display,
            )

            key = (
                cv2.waitKey(1)
                & 0xFF
            )

            if key == ord("c"):

                save_path = save_capture(
                    comparison
                )

                print(
                    f"Saved comparison frame: "
                    f"{save_path}"
                )

            if key in (
                27,
                ord("q"),
            ):
                break

    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()