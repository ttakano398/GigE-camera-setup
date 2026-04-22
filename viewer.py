import argparse
from pathlib import Path
from datetime import datetime

import cv2
import numpy as np


DEFAULT_SERIAL = "08520932"
WINDOW_NAME = "GigE Camera Viewer"
SCRIPT_DIR = Path(__file__).resolve().parent
CAPTURE_DIR = SCRIPT_DIR / "camera_rectify" / "capture"
DEFAULT_RECTIFY_CALIB_PATH = SCRIPT_DIR / "camera_rectify" / "camera_calib.yaml"
DEFAULT_RECTIFY_ALPHA = 0.0
DEFAULT_WINDOW_WIDTH = 1920
DEFAULT_WINDOW_HEIGHT = 1080

FISHEYE_DEFAULT_F_SCALE = 0.13
FISHEYE_DEFAULT_ZOOM = 2.8
FISHEYE_DEFAULT_OUTPUT_ZOOM = 2.0
FISHEYE_DEFAULT_RESOLUTION_SCALE = 1.0

MODE_CAPS = {
    "max": "video/x-bayer,format=grbg,width=2592,height=1944,framerate=22/1",
    "fhd": "video/x-bayer,format=grbg,width=1920,height=1080,framerate=30/1",
}

MODE_FPS = {
    "max": 22.0,
    "fhd": 30.0,
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


class FisheyeCorrector:
    def __init__(
        self,
        f_scale: float = FISHEYE_DEFAULT_F_SCALE,
        zoom: float = FISHEYE_DEFAULT_ZOOM,
        output_zoom: float = FISHEYE_DEFAULT_OUTPUT_ZOOM,
        resolution_scale: float = FISHEYE_DEFAULT_RESOLUTION_SCALE,
    ):
        self.f_scale = float(f_scale)
        self.zoom = float(zoom)
        self.output_zoom = float(output_zoom)
        self.resolution_scale = float(resolution_scale)
        if self.f_scale <= 0:
            raise ValueError("--fisheye-f-scale must be > 0")
        if self.zoom <= 0:
            raise ValueError("--fisheye-zoom must be > 0")
        if self.output_zoom <= 0:
            raise ValueError("--fisheye-output-zoom must be > 0")
        if self.resolution_scale <= 0:
            raise ValueError("--fisheye-resolution-scale must be > 0")
        self.map_x = None
        self.map_y = None
        self.current_config = None

    def _calculate_maps(self, width: int, height: int):
        output_w = max(1, int(round(width * self.resolution_scale)))
        output_h = max(1, int(round(height * self.resolution_scale)))
        cx, cy = width / 2.0, height / 2.0

        x = np.linspace(0, width - 1, output_w)
        y = np.linspace(0, height - 1, output_h)
        xx, yy = np.meshgrid(x, y)

        dx = xx - cx
        dy = yy - cy
        radius = np.sqrt(dx**2 + dy**2)

        focal = self.f_scale * min(width, height)
        theta = np.arctan2(radius, focal)
        cv_map = focal * theta

        eps = 1e-8
        map_x = (cx + (dx * self.zoom / (radius + eps)) * cv_map).astype(np.float32)
        map_y = (cy + (dy * self.zoom / (radius + eps)) * cv_map).astype(np.float32)
        return map_x, map_y

    def _apply_output_zoom(self, frame):
        if self.output_zoom == 1.0:
            return frame

        h, w = frame.shape[:2]
        if self.output_zoom > 1.0:
            crop_w = max(1, int(round(w / self.output_zoom)))
            crop_h = max(1, int(round(h / self.output_zoom)))
            x0 = max(0, (w - crop_w) // 2)
            y0 = max(0, (h - crop_h) // 2)
            cropped = frame[y0 : y0 + crop_h, x0 : x0 + crop_w]
            return cv2.resize(cropped, (w, h), interpolation=cv2.INTER_LINEAR)

        resized_w = max(1, int(round(w * self.output_zoom)))
        resized_h = max(1, int(round(h * self.output_zoom)))
        resized = cv2.resize(
            frame, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR
        )
        output = np.zeros_like(frame)
        x0 = (w - resized_w) // 2
        y0 = (h - resized_h) // 2
        output[y0 : y0 + resized_h, x0 : x0 + resized_w] = resized
        return output

    def apply(self, frame):
        if frame is None:
            return frame
        h, w = frame.shape[:2]
        config = (w, h, self.resolution_scale)
        if self.map_x is None or self.map_y is None or self.current_config != config:
            self.map_x, self.map_y = self._calculate_maps(w, h)
            self.current_config = config
            print(f"built fisheye maps for frame size: {w}x{h}")
        corrected = cv2.remap(
            frame, self.map_x, self.map_y, interpolation=cv2.INTER_LINEAR
        )
        return self._apply_output_zoom(corrected)


class RectifyCorrector:
    def __init__(self, calib_path: Path, alpha: float = DEFAULT_RECTIFY_ALPHA):
        self.calib_path = Path(calib_path)
        self.alpha = float(alpha)
        if not 0.0 <= self.alpha <= 1.0:
            raise ValueError("--rectify-alpha must be between 0 and 1")
        self.K, self.dist, self.calib_size = self._load_calib_yaml(self.calib_path)
        self.map1 = None
        self.map2 = None
        self.current_config = None
        self.warned_size_mismatch = False

    def _load_calib_yaml(self, yaml_path: Path):
        fs = cv2.FileStorage(str(yaml_path), cv2.FILE_STORAGE_READ)
        if not fs.isOpened():
            raise RuntimeError(f"Failed to open calib yaml: {yaml_path}")

        try:
            k_node = fs.getNode("K")
            dist_node = fs.getNode("dist")
            width_node = fs.getNode("image_width")
            height_node = fs.getNode("image_height")

            if k_node.empty() or dist_node.empty():
                raise RuntimeError("Invalid calib yaml: 'K' or 'dist' not found.")

            K = np.array(k_node.mat(), dtype=np.float64)
            dist = np.array(dist_node.mat(), dtype=np.float64).reshape(-1, 1)
            calib_w = int(width_node.real()) if not width_node.empty() else None
            calib_h = int(height_node.real()) if not height_node.empty() else None
            return K, dist, (calib_w, calib_h)
        finally:
            fs.release()

    def _scaled_camera_matrix(self, width: int, height: int):
        calib_w, calib_h = self.calib_size
        if calib_w is None or calib_h is None or (calib_w, calib_h) == (width, height):
            return self.K

        if not self.warned_size_mismatch:
            print(
                "Warning: rectify calib size and frame size differ: "
                f"calib={calib_w}x{calib_h}, frame={width}x{height}; scaling K"
            )
            self.warned_size_mismatch = True

        sx = width / float(calib_w)
        sy = height / float(calib_h)
        K = self.K.copy()
        K[0, 0] *= sx
        K[0, 2] *= sx
        K[1, 1] *= sy
        K[1, 2] *= sy
        return K

    def _calculate_maps(self, width: int, height: int):
        K = self._scaled_camera_matrix(width, height)
        new_K, _ = cv2.getOptimalNewCameraMatrix(
            K,
            self.dist,
            (width, height),
            self.alpha,
            (width, height),
        )
        return cv2.initUndistortRectifyMap(
            K,
            self.dist,
            R=None,
            newCameraMatrix=new_K,
            size=(width, height),
            m1type=cv2.CV_16SC2,
        )

    def apply(self, frame):
        if frame is None:
            return frame
        h, w = frame.shape[:2]
        config = (w, h, self.alpha)
        if self.map1 is None or self.map2 is None or self.current_config != config:
            self.map1, self.map2 = self._calculate_maps(w, h)
            self.current_config = config
            print(f"built rectify maps for frame size: {w}x{h}")
        return cv2.remap(
            frame,
            self.map1,
            self.map2,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
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
        default="max",
        help="Capture mode to preview",
    )
    correction_group = parser.add_mutually_exclusive_group()
    correction_group.add_argument(
        "--fisheye",
        action="store_true",
        help="Apply fisheye correction to preview/capture/recording.",
    )
    correction_group.add_argument(
        "--rectify",
        action="store_true",
        help="Apply camera calibration YAML rectification to preview/capture/recording.",
    )
    parser.add_argument(
        "--rectify-calib",
        type=Path,
        default=DEFAULT_RECTIFY_CALIB_PATH,
        help=f"Path to camera calibration YAML (default: {DEFAULT_RECTIFY_CALIB_PATH})",
    )
    parser.add_argument(
        "--rectify-alpha",
        type=float,
        default=DEFAULT_RECTIFY_ALPHA,
        help="Alpha for getOptimalNewCameraMatrix in [0, 1]. 0 crops more, 1 preserves more FoV.",
    )
    parser.add_argument(
        "--fisheye-f-scale",
        type=float,
        default=FISHEYE_DEFAULT_F_SCALE,
        help=f"Fisheye correction strength (default: {FISHEYE_DEFAULT_F_SCALE})",
    )
    parser.add_argument(
        "--fisheye-zoom",
        type=float,
        default=FISHEYE_DEFAULT_ZOOM,
        help=f"Fisheye remap zoom (default: {FISHEYE_DEFAULT_ZOOM})",
    )
    parser.add_argument(
        "--fisheye-output-zoom",
        type=float,
        default=FISHEYE_DEFAULT_OUTPUT_ZOOM,
        help=f"Output zoom after fisheye remap (default: {FISHEYE_DEFAULT_OUTPUT_ZOOM})",
    )
    parser.add_argument(
        "--fisheye-resolution-scale",
        type=float,
        default=FISHEYE_DEFAULT_RESOLUTION_SCALE,
        help=f"Fisheye output resolution scale (default: {FISHEYE_DEFAULT_RESOLUTION_SCALE})",
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
        help=f"Initial window width in pixels (default: {DEFAULT_WINDOW_WIDTH}).",
    )
    parser.add_argument(
        "--window-height",
        type=int,
        default=None,
        help=f"Initial window height in pixels (default: {DEFAULT_WINDOW_HEIGHT}).",
    )
    args = parser.parse_args()

    if args.display_scale <= 0:
        parser.error("--display-scale must be > 0")
    if not 0.0 <= args.rectify_alpha <= 1.0:
        parser.error("--rectify-alpha must be between 0 and 1")
    if args.fisheye_f_scale <= 0:
        parser.error("--fisheye-f-scale must be > 0")
    if args.fisheye_zoom <= 0:
        parser.error("--fisheye-zoom must be > 0")
    if args.fisheye_output_zoom <= 0:
        parser.error("--fisheye-output-zoom must be > 0")
    if args.fisheye_resolution_scale <= 0:
        parser.error("--fisheye-resolution-scale must be > 0")
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


def create_preview_window(width: int, height: int) -> None:
    flags = cv2.WINDOW_NORMAL
    if hasattr(cv2, "WINDOW_KEEPRATIO"):
        flags |= cv2.WINDOW_KEEPRATIO

    cv2.namedWindow(WINDOW_NAME, flags)
    cv2.resizeWindow(WINDOW_NAME, width, height)


def make_video_writer(frame_shape, fps: float):
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

    h, w = frame_shape[:2]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = CAPTURE_DIR / f"record_{timestamp}.mp4"

    fourcc_candidates = [
        ("mp4v", ".mp4"),
        ("avc1", ".mp4"),
        ("XVID", ".avi"),
        ("MJPG", ".avi"),
    ]

    for fourcc_str, ext in fourcc_candidates:
        candidate_path = save_path.with_suffix(ext)
        fourcc = cv2.VideoWriter_fourcc(*fourcc_str)
        writer = cv2.VideoWriter(str(candidate_path), fourcc, fps, (w, h))
        if writer.isOpened():
            return writer, candidate_path, fourcc_str

    raise RuntimeError("failed to open VideoWriter with mp4v/avc1/XVID/MJPG")


def main() -> None:
    args = parse_args()
    pipeline = make_pipeline(args.serial, args.mode)
    corrector = None
    if args.fisheye:
        corrector = FisheyeCorrector(
            f_scale=args.fisheye_f_scale,
            zoom=args.fisheye_zoom,
            output_zoom=args.fisheye_output_zoom,
            resolution_scale=args.fisheye_resolution_scale,
        )
    elif args.rectify:
        corrector = RectifyCorrector(args.rectify_calib, args.rectify_alpha)
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)

    print("script_dir:", SCRIPT_DIR)
    print("capture_dir:", CAPTURE_DIR)
    print("pipeline:", pipeline)
    print("opened:", cap.isOpened())
    print("display_scale:", args.display_scale)
    if corrector is not None:
        print("correction:", "fisheye" if args.fisheye else "rectify")
    if args.fisheye:
        print(
            "fisheye:",
            f"f_scale={args.fisheye_f_scale}",
            f"zoom={args.fisheye_zoom}",
            f"output_zoom={args.fisheye_output_zoom}",
            f"resolution_scale={args.fisheye_resolution_scale}",
        )
    if args.rectify:
        print("rectify_calib:", args.rectify_calib)
        print("rectify_alpha:", args.rectify_alpha)
    window_width = args.window_width or DEFAULT_WINDOW_WIDTH
    window_height = args.window_height or DEFAULT_WINDOW_HEIGHT
    print("window_size:", f"{window_width}x{window_height}")

    if not cap.isOpened():
        raise RuntimeError("failed to open camera via GStreamer/tcamsrc")

    create_preview_window(window_width, window_height)

    writer = None
    recording = False
    record_path = None
    record_codec = None
    fps = MODE_FPS[args.mode]

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                continue
            if corrector is not None:
                frame = corrector.apply(frame)

            if recording and writer is not None:
                writer.write(frame)

            display_frame = resize_for_display(frame, args.display_scale)

            if recording:
                cv2.putText(
                    display_frame,
                    "REC",
                    (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1.2,
                    (0, 0, 255),
                    3,
                    cv2.LINE_AA,
                )

            cv2.imshow(WINDOW_NAME, display_frame)
            key = cv2.waitKey(1) & 0xFF

            if key == ord("c"):
                save_path = save_capture(frame)
                print(f"saved image: {save_path}")

            elif key == ord("r"):
                if not recording:
                    writer, record_path, record_codec = make_video_writer(
                        frame.shape, fps
                    )
                    recording = True
                    print(
                        f"recording started: {record_path} (codec={record_codec}, fps={fps})"
                    )
                else:
                    print("already recording")

            elif key == ord("e"):
                if recording:
                    writer.release()
                    writer = None
                    recording = False
                    print(f"recording stopped: {record_path}")
                    record_path = None
                    record_codec = None
                else:
                    print("recording is not active")

            if key in (27, ord("q")):
                break

    finally:
        if writer is not None:
            writer.release()
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
