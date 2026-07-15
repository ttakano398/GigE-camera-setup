import argparse
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime

import cv2
import numpy as np


DEFAULT_SERIAL = "16620659"

WINDOW_NAME = "GigE Camera Calibration Comparison"
CONTROL_WINDOW_NAME = "Calibration Controls"

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CALIB_PATH = SCRIPT_DIR / "camera_calib.yaml"
CAPTURE_DIR = SCRIPT_DIR / "camera_rectify" / "capture"

MAX_PANELS = 4

MODE_CAPS = {
    "max": "video/x-bayer,format=grbg,width=2592,height=1944,framerate=22/1",
    "fhd": "video/x-bayer,format=grbg,width=1920,height=1080,framerate=30/1",
}

# FoV scale:
#   1.0  -> original focal length from K
#   >1.0 -> wider FoV
#   <1.0 -> narrower FoV / zoomed-in
DEFAULT_FOV_SCALE = 1.0
FOV_SCALE_MIN = 0.50
FOV_SCALE_MAX = 2.00
FOV_SLIDER_STEPS = 150

ZERO_TANGENTIAL_TRACKBAR = "Zero p1,p2"
SOLO_MODE_TRACKBAR = "Solo mode"
SOLO_PANEL_TRACKBAR = "Solo panel"


@dataclass
class Calibration:
    path: Path
    K: np.ndarray
    dist: np.ndarray
    image_size: tuple[int | None, int | None]

    map1: np.ndarray | None = None
    map2: np.ndarray | None = None
    map_size: tuple[int, int] | None = None
    last_fov_scale: float | None = None


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
            "Preview a TIS GigE camera and compare up to three calibration YAMLs "
            "with independent FoV controls and solo viewing."
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
        help="Calibration YAML files. Up to 3 files can be specified.",
    )

    parser.add_argument(
        "--fov-scale",
        type=float,
        default=DEFAULT_FOV_SCALE,
        help=(
            "Initial FoV scale for all calibrations. "
            "1.0=original K, >1.0=wider, <1.0=zoomed-in."
        ),
    )

    parser.add_argument(
        "--display-scale",
        type=float,
        default=1.0,
        help=(
            "Display-only scale for the comparison grid. "
            "Solo mode always sends the full-resolution frame to imshow()."
        ),
    )

    parser.add_argument(
        "--window-width",
        type=int,
        default=None,
        help="Initial viewer window width",
    )

    parser.add_argument(
        "--window-height",
        type=int,
        default=None,
        help="Initial viewer window height",
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

        if K.shape != (3, 3):
            raise RuntimeError(
                f"Invalid K shape in {yaml_path}: {K.shape}"
            )

        calib_w = int(width_node.real()) if not width_node.empty() else None
        calib_h = int(height_node.real()) if not height_node.empty() else None

        return K, dist, (calib_w, calib_h)

    finally:
        fs.release()


def scale_camera_matrix(
    K: np.ndarray,
    calib_size: tuple[int | None, int | None],
    image_size: tuple[int, int],
) -> np.ndarray:
    calib_w, calib_h = calib_size
    image_w, image_h = image_size

    if calib_w is None or calib_h is None:
        return K.copy()

    if (calib_w, calib_h) == image_size:
        return K.copy()

    scale_x = image_w / calib_w
    scale_y = image_h / calib_h

    scaled_K = K.copy()

    scaled_K[0, 0] *= scale_x
    scaled_K[0, 1] *= scale_x
    scaled_K[0, 2] *= scale_x

    scaled_K[1, 0] *= scale_y
    scaled_K[1, 1] *= scale_y
    scaled_K[1, 2] *= scale_y

    scaled_K[2, :] = K[2, :]

    return scaled_K


def build_undistort_maps(
    K: np.ndarray,
    dist: np.ndarray,
    calib_size: tuple[int | None, int | None],
    image_size: tuple[int, int],
    fov_scale: float,
    zero_tangential: bool,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Build undistortion maps with manual output FoV control.

    fov_scale:
        1.0  -> original focal length
        >1.0 -> wider FoV
        <1.0 -> zoomed-in

    zero_tangential:
        True -> set p1 and p2 to zero before building the map.
    """
    if fov_scale <= 0:
        raise ValueError("fov_scale must be > 0")

    w, h = image_size

    K_for_image = scale_camera_matrix(
        K=K,
        calib_size=calib_size,
        image_size=image_size,
    )

    dist_for_map = dist.copy()

    # OpenCV pinhole/rational layout:
    # [k1, k2, p1, p2, k3, k4, k5, k6, ...]
    if zero_tangential and dist_for_map.size >= 4:
        dist_for_map[2, 0] = 0.0
        dist_for_map[3, 0] = 0.0

    new_K = K_for_image.copy()

    # Larger scale -> smaller focal length -> wider output FoV.
    new_K[0, 0] = K_for_image[0, 0] / fov_scale
    new_K[1, 1] = K_for_image[1, 1] / fov_scale

    map1, map2 = cv2.initUndistortRectifyMap(
        K_for_image,
        dist_for_map,
        R=None,
        newCameraMatrix=new_K,
        size=(w, h),
        m1type=cv2.CV_16SC2,
    )

    return new_K, map1, map2


def fov_scale_to_slider(fov_scale: float) -> int:
    clipped = float(
        np.clip(
            fov_scale,
            FOV_SCALE_MIN,
            FOV_SCALE_MAX,
        )
    )

    normalized = (
        (clipped - FOV_SCALE_MIN)
        / (FOV_SCALE_MAX - FOV_SCALE_MIN)
    )

    return int(round(normalized * FOV_SLIDER_STEPS))


def slider_to_fov_scale(position: int) -> float:
    normalized = position / FOV_SLIDER_STEPS

    return (
        FOV_SCALE_MIN
        + normalized * (FOV_SCALE_MAX - FOV_SCALE_MIN)
    )


def save_capture(frame: np.ndarray) -> Path:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    save_path = CAPTURE_DIR / f"comparison_{timestamp}.png"

    ok = cv2.imwrite(str(save_path), frame)

    if not ok:
        raise RuntimeError(f"Failed to save capture: {save_path}")

    return save_path


def put_label(
    frame: np.ndarray,
    text: str,
) -> np.ndarray:
    out = frame.copy()

    cv2.putText(
        out,
        text,
        (16, 36),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (0, 0, 0),
        5,
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


def make_grid(
    frames: list[np.ndarray],
) -> np.ndarray:
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

    grid_frames = frames.copy()

    while len(grid_frames) < 4:
        grid_frames.append(
            np.zeros_like(grid_frames[0])
        )

    top = np.hstack(
        [
            grid_frames[0],
            grid_frames[1],
        ]
    )

    bottom = np.hstack(
        [
            grid_frames[2],
            grid_frames[3],
        ]
    )

    return np.vstack([top, bottom])


def resize_for_display(
    frame: np.ndarray,
    scale: float,
) -> np.ndarray:
    if np.isclose(scale, 1.0):
        return frame

    if scale <= 0:
        raise ValueError("display_scale must be > 0")

    h, w = frame.shape[:2]

    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))

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
        raise ValueError("--display-scale must be > 0")

    if not (
        FOV_SCALE_MIN
        <= args.fov_scale
        <= FOV_SCALE_MAX
    ):
        raise ValueError(
            "--fov-scale must be within "
            f"[{FOV_SCALE_MIN}, {FOV_SCALE_MAX}]"
        )

    if len(args.calib) + 1 > MAX_PANELS:
        raise ValueError(
            f"At most {MAX_PANELS - 1} calibration YAML files "
            "can be specified because RAW occupies one panel."
        )

    calibrations: list[Calibration] = []

    for yaml_path in args.calib:
        K, dist, calib_size = load_calib_yaml(yaml_path)

        calibrations.append(
            Calibration(
                path=yaml_path,
                K=K,
                dist=dist,
                image_size=calib_size,
            )
        )

        print()
        print("=" * 70)
        print("calib:", yaml_path)
        print("K:")
        print(K)
        print("dist:", dist.ravel())
        print("calib image size:", calib_size)

    pipeline = make_pipeline(
        serial=args.serial,
        mode=args.mode,
    )

    cap = cv2.VideoCapture(
        pipeline,
        cv2.CAP_GSTREAMER,
    )

    print()
    print("=" * 70)
    print("script:", Path(__file__).resolve())
    print("pipeline:", pipeline)
    print("opened:", cap.isOpened())
    print("display_scale:", args.display_scale)
    print("initial FoV scale:", args.fov_scale)
    print("number of panels:", len(calibrations) + 1)

    if not cap.isOpened():
        raise RuntimeError(
            "Failed to open camera via GStreamer/tcamsrc"
        )

    cv2.namedWindow(
        WINDOW_NAME,
        cv2.WINDOW_NORMAL,
    )

    cv2.namedWindow(
        CONTROL_WINDOW_NAME,
        cv2.WINDOW_NORMAL,
    )

    # HighGUI does not provide native checkboxes,
    # so 0/1 trackbars are used as toggles.
    cv2.createTrackbar(
        ZERO_TANGENTIAL_TRACKBAR,
        CONTROL_WINDOW_NAME,
        0,
        1,
        lambda _value: None,
    )

    cv2.createTrackbar(
        SOLO_MODE_TRACKBAR,
        CONTROL_WINDOW_NAME,
        0,
        1,
        lambda _value: None,
    )

    # 0=RAW, 1=calib1, 2=calib2, 3=calib3
    cv2.createTrackbar(
        SOLO_PANEL_TRACKBAR,
        CONTROL_WINDOW_NAME,
        0,
        len(calibrations),
        lambda _value: None,
    )

    initial_slider_position = fov_scale_to_slider(
        args.fov_scale
    )

    fov_trackbar_names: list[str] = []

    for i, calibration in enumerate(calibrations):
        # Keep trackbar names short to avoid GTK layout issues.
        trackbar_name = f"FoV {i + 1}"

        fov_trackbar_names.append(
            trackbar_name
        )

        cv2.createTrackbar(
            trackbar_name,
            CONTROL_WINDOW_NAME,
            initial_slider_position,
            FOV_SLIDER_STEPS,
            lambda _value: None,
        )

    # Make the control window large enough for all trackbars.
    num_control_bars = 3 + len(calibrations)

    cv2.resizeWindow(
        CONTROL_WINDOW_NAME,
        760,
        max(
            500,
            75 * num_control_bars + 100,
        ),
    )

    # Small dummy canvas. Keeping this short leaves more vertical space
    # for the HighGUI trackbars.
    control_image = np.zeros(
        (80, 760, 3),
        dtype=np.uint8,
    )

    cv2.putText(
        control_image,
        "Solo panel: 0=RAW, 1..3=calib | FoV: >1 wider, <1 zoom",
        (12, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )

    cv2.putText(
        control_image,
        "Zero p1,p2 / Solo mode: 0=OFF, 1=ON",
        (12, 58),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )

    cv2.imshow(
        CONTROL_WINDOW_NAME,
        control_image,
    )

    window_initialized = False
    last_zero_tangential: bool | None = None

    try:
        while True:
            ok, frame = cap.read()

            if not ok:
                continue

            h, w = frame.shape[:2]
            current_size = (w, h)

            zero_tangential = bool(
                cv2.getTrackbarPos(
                    ZERO_TANGENTIAL_TRACKBAR,
                    CONTROL_WINDOW_NAME,
                )
            )

            solo_mode = bool(
                cv2.getTrackbarPos(
                    SOLO_MODE_TRACKBAR,
                    CONTROL_WINDOW_NAME,
                )
            )

            solo_panel_index = cv2.getTrackbarPos(
                SOLO_PANEL_TRACKBAR,
                CONTROL_WINDOW_NAME,
            )

            current_fov_scales: list[float] = []

            for trackbar_name in fov_trackbar_names:
                position = cv2.getTrackbarPos(
                    trackbar_name,
                    CONTROL_WINDOW_NAME,
                )

                current_fov_scales.append(
                    slider_to_fov_scale(position)
                )

            tangential_changed = (
                last_zero_tangential is None
                or zero_tangential
                != last_zero_tangential
            )

            panels = [
                put_label(
                    frame,
                    "RAW / NO CALIBRATION",
                )
            ]

            for i, calibration in enumerate(calibrations):
                current_fov_scale = current_fov_scales[i]

                need_rebuild_map = (
                    calibration.map1 is None
                    or calibration.map2 is None
                    or calibration.map_size != current_size
                    or calibration.last_fov_scale is None
                    or not np.isclose(
                        current_fov_scale,
                        calibration.last_fov_scale,
                    )
                    or tangential_changed
                )

                if need_rebuild_map:
                    new_K, map1, map2 = build_undistort_maps(
                        K=calibration.K,
                        dist=calibration.dist,
                        calib_size=calibration.image_size,
                        image_size=current_size,
                        fov_scale=current_fov_scale,
                        zero_tangential=zero_tangential,
                    )

                    calibration.map1 = map1
                    calibration.map2 = map2
                    calibration.map_size = current_size
                    calibration.last_fov_scale = current_fov_scale

                    print()
                    print(
                        f"[calib {i + 1}] "
                        f"{calibration.path.name}"
                    )
                    print(
                        f"FoV scale: {current_fov_scale:.3f}"
                    )
                    print(
                        f"Zero p1,p2: {zero_tangential}"
                    )
                    print("new_K:")
                    print(new_K)

                rectified = cv2.remap(
                    frame,
                    calibration.map1,
                    calibration.map2,
                    interpolation=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT,
                )

                tangential_label = (
                    ", p1,p2=0"
                    if zero_tangential
                    else ""
                )

                panels.append(
                    put_label(
                        rectified,
                        (
                            f"{calibration.path.stem} "
                            f"[FoV={current_fov_scale:.2f}x"
                            f"{tangential_label}]"
                        ),
                    )
                )

            last_zero_tangential = zero_tangential

            comparison = make_grid(panels)

            if solo_mode:
                selected_index = int(
                    np.clip(
                        solo_panel_index,
                        0,
                        len(panels) - 1,
                    )
                )

                # Full-resolution frame is passed directly to imshow().
                # Window resizing is left to WINDOW_NORMAL.
                display = panels[selected_index]

            else:
                display = resize_for_display(
                    comparison,
                    args.display_scale,
                )

            if not window_initialized:
                disp_h, disp_w = display.shape[:2]

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

                print()
                print(
                    "viewer window size:",
                    f"{window_w}x{window_h}",
                )

                window_initialized = True

            cv2.imshow(
                WINDOW_NAME,
                display,
            )

            # Re-show the control canvas to keep the controls window alive.
            cv2.imshow(
                CONTROL_WINDOW_NAME,
                control_image,
            )

            key = cv2.waitKey(1) & 0xFF

            if key == ord("c"):
                save_path = save_capture(comparison)
                print(
                    "Saved comparison frame:",
                    save_path,
                )

            if key in (27, ord("q")):
                break

    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()