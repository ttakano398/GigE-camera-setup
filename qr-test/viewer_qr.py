from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from viewer import (  # noqa: E402
    DEFAULT_RECTIFY_ALPHA,
    DEFAULT_RECTIFY_CALIB_PATH,
    DEFAULT_SERIAL,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    FISHEYE_DEFAULT_F_SCALE,
    FISHEYE_DEFAULT_OUTPUT_ZOOM,
    FISHEYE_DEFAULT_RESOLUTION_SCALE,
    FISHEYE_DEFAULT_ZOOM,
    FisheyeCorrector,
    MODE_FPS,
    RectifyCorrector,
    make_pipeline,
    resize_for_display,
)


WINDOW_NAME = "GigE QR Viewer"
SELECT_WINDOW_NAME = "GigE QR Startup"
CAPTURE_DIR = SCRIPT_DIR / "capture"
DEFAULT_WECHAT_MODEL_DIR = SCRIPT_DIR / "opencv_3rdparty"
KNOWN_CAMERA_SERIALS = (DEFAULT_SERIAL, "05620902", "05620909", "05620901")

DETECTOR_ORDER = ("opencv", "wechat", "pyzbar", "qreader")
PIPELINE_ORDER = ("opencv", "pyzbar", "wechat")
ALGORITHM_LABELS = {
    "pipeline": "Pipeline OCV>Pyz>WC",
    "opencv": "OpenCV QRCode",
    "wechat": "WeChat QRCode",
    "pyzbar": "pyzbar",
    "qreader": "QReader YOLO",
    "all": "All available",
}
ALGORITHM_COLORS = {
    "opencv": (0, 80, 255),
    "wechat": (0, 220, 0),
    "pyzbar": (255, 255, 0),
    "qreader": (255, 80, 0),
}


@dataclass(frozen=True)
class AlgorithmStatus:
    key: str
    label: str
    available: bool
    reason: str = ""


@dataclass
class ButtonSpec:
    key: str
    label: str
    rect: tuple[int, int, int, int]
    enabled: bool
    reason: str = ""


@dataclass
class QRDetection:
    algorithm: str
    label: str
    text: str
    points: np.ndarray | None = None
    rect: tuple[int, int, int, int] | None = None


@dataclass
class RecognitionStats:
    attempts: int = 0
    hits: int = 0
    last_ms: float = 0.0

    def update(self, detections: list[QRDetection], elapsed_ms: float) -> None:
        self.attempts += 1
        self.last_ms = elapsed_ms
        if detections:
            self.hits += 1

    @property
    def rate(self) -> float:
        if self.attempts == 0:
            return 0.0
        return 100.0 * self.hits / self.attempts


class OpenCVQRDetector:
    key = "opencv"
    label = ALGORITHM_LABELS[key]

    def __init__(self):
        self.detector = cv2.QRCodeDetector()

    def detect(self, frame) -> list[QRDetection]:
        detections: list[QRDetection] = []
        try:
            retval, decoded_info, points, _ = self.detector.detectAndDecodeMulti(frame)
        except cv2.error:
            retval, decoded_info, points = False, [], None

        if retval and points is not None:
            points = np.asarray(points, dtype=np.int32)
            for text, point in zip(decoded_info, points):
                if text:
                    detections.append(
                        QRDetection(self.key, self.label, text, points=point)
                    )

        if detections:
            return detections

        text, points, _ = self.detector.detectAndDecode(frame)
        if text and points is not None:
            detections.append(
                QRDetection(
                    self.key,
                    self.label,
                    text,
                    points=np.asarray(points, dtype=np.int32).reshape(-1, 2),
                )
            )
        return detections


class WeChatQRDetector:
    key = "wechat"
    label = ALGORITHM_LABELS[key]

    def __init__(self, model_dir: Path):
        model_dir = Path(model_dir)
        self.detector = cv2.wechat_qrcode_WeChatQRCode(
            str(model_dir / "detect.prototxt"),
            str(model_dir / "detect.caffemodel"),
            str(model_dir / "sr.prototxt"),
            str(model_dir / "sr.caffemodel"),
        )

    def detect(self, frame) -> list[QRDetection]:
        results, points = self.detector.detectAndDecode(frame)
        if points is None:
            return []

        detections: list[QRDetection] = []
        for text, point in zip(results, points):
            if text:
                detections.append(
                    QRDetection(
                        self.key,
                        self.label,
                        text,
                        points=np.asarray(point, dtype=np.int32).reshape(-1, 2),
                    )
                )
        return detections


class PyzbarQRDetector:
    key = "pyzbar"
    label = ALGORITHM_LABELS[key]

    def __init__(self):
        from pyzbar.pyzbar import ZBarSymbol, decode

        self.decode = decode
        self.qr_symbol = ZBarSymbol.QRCODE

    def detect(self, frame) -> list[QRDetection]:
        detections: list[QRDetection] = []
        values = self.decode(frame, symbols=[self.qr_symbol])
        for qrcode in values:
            text = qrcode.data.decode("utf-8", errors="replace")
            if not text:
                continue
            points = None
            if getattr(qrcode, "polygon", None):
                points = np.asarray(
                    [(point.x, point.y) for point in qrcode.polygon],
                    dtype=np.int32,
                )
            rect = (
                qrcode.rect.left,
                qrcode.rect.top,
                qrcode.rect.width,
                qrcode.rect.height,
            )
            detections.append(
                QRDetection(self.key, self.label, text, points=points, rect=rect)
            )
        return detections


class QReaderQRDetector:
    key = "qreader"
    label = ALGORITHM_LABELS[key]

    def __init__(self, model_size: str):
        from qreader import QReader

        self.reader = QReader(model_size=model_size)

    def detect(self, frame) -> list[QRDetection]:
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = self.reader.detect_and_decode(
            image=rgb_frame,
            return_detections=True,
        )
        if isinstance(result, tuple) and len(result) == 2:
            decoded_texts, raw_detections = result
        else:
            decoded_texts = result
            raw_detections = [None] * len(decoded_texts)

        detections: list[QRDetection] = []
        for text, raw_detection in zip(decoded_texts, raw_detections):
            if not text:
                continue

            rect = None
            points = None
            if isinstance(raw_detection, dict):
                if "quad_xy" in raw_detection:
                    points = np.asarray(raw_detection["quad_xy"], dtype=np.int32)
                elif "polygon_xy" in raw_detection:
                    points = np.asarray(raw_detection["polygon_xy"], dtype=np.int32)
                elif "bbox_xyxy" in raw_detection:
                    x1, y1, x2, y2 = map(int, raw_detection["bbox_xyxy"])
                    rect = (x1, y1, max(1, x2 - x1), max(1, y2 - y1))

            detections.append(
                QRDetection(self.key, self.label, str(text), points=points, rect=rect)
            )
        return detections


class PipelineQRDetector:
    key = "pipeline"
    label = ALGORITHM_LABELS[key]

    def __init__(self, detectors):
        self.detectors = detectors
        self.label = "Pipeline " + " -> ".join(detector.label for detector in detectors)

    def detect(self, frame) -> list[QRDetection]:
        for detector in self.detectors:
            try:
                detections = detector.detect(frame)
            except Exception as exc:
                print(f"warning: {detector.label} failed in pipeline: {exc}")
                continue
            if detections:
                return detections
        return []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview a TIS GigE camera and overlay QR recognition results."
    )
    parser.add_argument(
        "--serial",
        default=None,
        help=(
            "Camera serial number. If omitted, the startup selector lets you choose "
            f"one (default candidate: {DEFAULT_SERIAL})."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["max", "fhd"],
        default="max",
        help="Capture mode to preview",
    )
    parser.add_argument(
        "--algorithm",
        choices=[
            "select",
            "pipeline",
            "opencv",
            "wechat",
            "pyzbar",
            "qreader",
            "all",
        ],
        default="select",
        help="QR algorithm. The default opens a startup button selector.",
    )
    parser.add_argument(
        "--qr-interval",
        type=int,
        default=3,
        help="Run QR inference every N frames and reuse cached boxes between runs.",
    )
    parser.add_argument(
        "--qreader-model-size",
        choices=["n", "s", "m", "l"],
        default="n",
        help="QReader model size. n is the fastest.",
    )
    parser.add_argument(
        "--wechat-model-dir",
        type=Path,
        default=DEFAULT_WECHAT_MODEL_DIR,
        help=f"WeChat QRCode model directory (default: {DEFAULT_WECHAT_MODEL_DIR})",
    )

    correction_group = parser.add_mutually_exclusive_group()
    correction_group.add_argument(
        "--fisheye",
        action="store_true",
        help="Apply fisheye correction before QR recognition/display/recording.",
    )
    correction_group.add_argument(
        "--rectify",
        action="store_true",
        help="Apply camera calibration YAML rectification before QR recognition/display/recording.",
    )
    parser.add_argument(
        "--compare-corrections",
        action="store_true",
        help="Show RAW, fisheye, and rectify views side by side and track QR hit rate for each.",
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
        help="Alpha for getOptimalNewCameraMatrix in [0, 1].",
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
        help="Scale factor applied only to the displayed image.",
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
    if args.qr_interval <= 0:
        parser.error("--qr-interval must be > 0")
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
    if args.compare_corrections and (args.fisheye or args.rectify):
        parser.error("--compare-corrections cannot be combined with --fisheye or --rectify")
    if (args.window_width is None) != (args.window_height is None):
        parser.error("--window-width and --window-height must be specified together")
    return args


def wechat_model_files(model_dir: Path) -> list[Path]:
    return [
        model_dir / "detect.prototxt",
        model_dir / "detect.caffemodel",
        model_dir / "sr.prototxt",
        model_dir / "sr.caffemodel",
    ]


def check_algorithm_statuses(model_dir: Path) -> dict[str, AlgorithmStatus]:
    statuses: dict[str, AlgorithmStatus] = {}
    statuses["opencv"] = AlgorithmStatus(
        "opencv",
        ALGORITHM_LABELS["opencv"],
        hasattr(cv2, "QRCodeDetector"),
        "OpenCV QRCodeDetector is not available",
    )

    missing_wechat_files = [
        path.name for path in wechat_model_files(model_dir) if not path.exists()
    ]
    statuses["wechat"] = AlgorithmStatus(
        "wechat",
        ALGORITHM_LABELS["wechat"],
        hasattr(cv2, "wechat_qrcode_WeChatQRCode") and not missing_wechat_files,
        "missing opencv_3rdparty models"
        if missing_wechat_files
        else "OpenCV WeChatQRCode is not available",
    )

    try:
        import pyzbar.pyzbar  # noqa: F401

        pyzbar_available = True
        pyzbar_reason = ""
    except ImportError:
        pyzbar_available = False
        pyzbar_reason = "pip install pyzbar and libzbar"
    statuses["pyzbar"] = AlgorithmStatus(
        "pyzbar",
        ALGORITHM_LABELS["pyzbar"],
        pyzbar_available,
        pyzbar_reason,
    )

    qreader_available = importlib.util.find_spec("qreader") is not None
    qreader_reason = "" if qreader_available else "pip install qreader"
    statuses["qreader"] = AlgorithmStatus(
        "qreader",
        ALGORITHM_LABELS["qreader"],
        qreader_available,
        qreader_reason,
    )

    pipeline_keys = [key for key in PIPELINE_ORDER if statuses[key].available]
    statuses["pipeline"] = AlgorithmStatus(
        "pipeline",
        ALGORITHM_LABELS["pipeline"],
        bool(pipeline_keys),
        "OpenCV, pyzbar, and WeChat are all unavailable",
    )
    return statuses


def make_button_specs(statuses: dict[str, AlgorithmStatus]) -> list[ButtonSpec]:
    specs = [
        ("pipeline", 50, 120),
        ("opencv", 50, 190),
        ("pyzbar", 50, 260),
        ("wechat", 390, 120),
        ("qreader", 390, 190),
    ]
    buttons = [
        ButtonSpec(
            key,
            statuses[key].label,
            (x, y, 280, 60),
            statuses[key].available,
            statuses[key].reason,
        )
        for key, x, y in specs
    ]
    any_available = any(statuses[key].available for key in DETECTOR_ORDER)
    buttons.append(
        ButtonSpec(
            "all",
            ALGORITHM_LABELS["all"],
            (390, 260, 280, 60),
            any_available,
            "no available algorithms",
        )
    )
    return buttons


def draw_algorithm_selector(
    buttons: list[ButtonSpec],
    selected_key: str | None,
) -> np.ndarray:
    canvas = np.full((380, 720, 3), (28, 30, 34), dtype=np.uint8)
    cv2.putText(
        canvas,
        "Select QR algorithm",
        (50, 54),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (245, 245, 245),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        canvas,
        "Click a button or press 1-6. Esc/q cancels.",
        (50, 86),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (190, 195, 200),
        1,
        cv2.LINE_AA,
    )

    for index, button in enumerate(buttons, start=1):
        x, y, w, h = button.rect
        if button.enabled:
            fill = (64, 86, 118)
            border = (120, 185, 255)
            text_color = (255, 255, 255)
        else:
            fill = (56, 56, 60)
            border = (85, 85, 90)
            text_color = (145, 145, 150)

        if selected_key == button.key:
            fill = (78, 110, 150)
            border = (0, 220, 255)

        cv2.rectangle(canvas, (x, y), (x + w, y + h), fill, -1, cv2.LINE_AA)
        cv2.rectangle(canvas, (x, y), (x + w, y + h), border, 2, cv2.LINE_AA)
        cv2.putText(
            canvas,
            f"{index}. {button.label}",
            (x + 16, y + 33),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.62,
            text_color,
            2,
            cv2.LINE_AA,
        )
        if not button.enabled and button.reason:
            cv2.putText(
                canvas,
                button.reason[:38],
                (x + 16, y + 50),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.42,
                (120, 125, 130),
                1,
                cv2.LINE_AA,
            )

    return canvas


def select_algorithm_with_buttons(statuses: dict[str, AlgorithmStatus]) -> str:
    buttons = make_button_specs(statuses)
    selected_key: str | None = None

    def on_mouse(event, x, y, _flags, _userdata):
        nonlocal selected_key
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        for button in buttons:
            bx, by, bw, bh = button.rect
            if button.enabled and bx <= x <= bx + bw and by <= y <= by + bh:
                selected_key = button.key
                return

    cv2.namedWindow(SELECT_WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(SELECT_WINDOW_NAME, 720, 380)
    cv2.setMouseCallback(SELECT_WINDOW_NAME, on_mouse)

    try:
        while selected_key is None:
            cv2.imshow(SELECT_WINDOW_NAME, draw_algorithm_selector(buttons, selected_key))
            key = cv2.waitKey(50) & 0xFF
            if key in (27, ord("q")):
                raise KeyboardInterrupt("algorithm selection cancelled")
            if ord("1") <= key <= ord("6"):
                index = key - ord("1")
                if index < len(buttons) and buttons[index].enabled:
                    selected_key = buttons[index].key
    finally:
        try:
            cv2.destroyWindow(SELECT_WINDOW_NAME)
        except cv2.error:
            pass

    return selected_key


def unique_preserve_order(values: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique_values.append(value)
    return unique_values


def discover_camera_serials() -> list[str]:
    try:
        result = subprocess.run(
            ["tcam-gigetool", "list", "--format", "s"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []

    if result.returncode != 0:
        return []

    serials = re.findall(r"\b\d{6,}\b", result.stdout)
    return unique_preserve_order(serials)


def make_startup_buttons(
    statuses: dict[str, AlgorithmStatus],
    serials: list[str],
) -> tuple[list[ButtonSpec], list[ButtonSpec]]:
    serial_buttons: list[ButtonSpec] = []
    for index, serial in enumerate(serials[:8]):
        y = 124 + index * 54
        serial_buttons.append(
            ButtonSpec(
                serial,
                serial,
                (48, y, 280, 44),
                True,
            )
        )

    algorithm_specs = [
        ("pipeline", 392, 124),
        ("opencv", 392, 178),
        ("pyzbar", 392, 232),
        ("wechat", 392, 286),
        ("qreader", 392, 340),
        ("all", 392, 394),
    ]
    algorithm_buttons = []
    for key, x, y in algorithm_specs:
        if key == "all":
            enabled = any(statuses[detector_key].available for detector_key in DETECTOR_ORDER)
            reason = "no available algorithms"
        else:
            enabled = statuses[key].available
            reason = statuses[key].reason
        algorithm_buttons.append(
            ButtonSpec(
                key,
                ALGORITHM_LABELS[key],
                (x, y, 280, 44),
                enabled,
                reason,
            )
        )

    return serial_buttons, algorithm_buttons


def draw_startup_selector(
    serial_buttons: list[ButtonSpec],
    algorithm_buttons: list[ButtonSpec],
    selected_serial: str | None,
    selected_algorithm: str | None,
) -> np.ndarray:
    canvas = np.full((600, 720, 3), (28, 30, 34), dtype=np.uint8)
    cv2.putText(
        canvas,
        "GigE QR Viewer",
        (48, 52),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (245, 245, 245),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        canvas,
        "Select camera serial and QR algorithm. Enter starts.",
        (48, 84),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (190, 195, 200),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        canvas,
        "Camera serial",
        (48, 112),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (220, 225, 230),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        canvas,
        "QR algorithm",
        (392, 112),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (220, 225, 230),
        1,
        cv2.LINE_AA,
    )

    def draw_button(button: ButtonSpec, selected: bool, shortcut: str):
        x, y, w, h = button.rect
        if button.enabled:
            fill = (64, 86, 118)
            border = (120, 185, 255)
            text_color = (255, 255, 255)
        else:
            fill = (56, 56, 60)
            border = (85, 85, 90)
            text_color = (145, 145, 150)

        if selected:
            fill = (78, 110, 150)
            border = (0, 220, 255)

        cv2.rectangle(canvas, (x, y), (x + w, y + h), fill, -1, cv2.LINE_AA)
        cv2.rectangle(canvas, (x, y), (x + w, y + h), border, 2, cv2.LINE_AA)
        cv2.putText(
            canvas,
            f"{shortcut}. {button.label}",
            (x + 14, y + 29),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            text_color,
            2,
            cv2.LINE_AA,
        )
        if not button.enabled and button.reason:
            cv2.putText(
                canvas,
                button.reason[:34],
                (x + 14, y + 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.34,
                (122, 126, 132),
                1,
                cv2.LINE_AA,
            )

    for index, button in enumerate(serial_buttons, start=1):
        draw_button(button, button.key == selected_serial, str(index))

    shortcut_keys = ["P", "A", "D", "S", "F", "G"]
    for shortcut, button in zip(shortcut_keys, algorithm_buttons):
        draw_button(button, button.key == selected_algorithm, shortcut)

    if selected_serial and selected_algorithm:
        cv2.putText(
            canvas,
            "Starting...",
            (48, 580),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 220, 255),
            1,
            cv2.LINE_AA,
        )
    else:
        cv2.putText(
            canvas,
            "Esc/q cancels.",
            (48, 580),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (170, 175, 180),
            1,
            cv2.LINE_AA,
        )

    return canvas


def select_startup_options(
    statuses: dict[str, AlgorithmStatus],
    requested_serial: str | None,
    requested_algorithm: str,
) -> tuple[str, str]:
    discovered_serials = discover_camera_serials()
    serials = unique_preserve_order(
        discovered_serials + list(KNOWN_CAMERA_SERIALS)
    )
    if requested_serial:
        serials = unique_preserve_order([requested_serial] + serials)

    serial_buttons, algorithm_buttons = make_startup_buttons(statuses, serials)
    selected_serial = requested_serial
    selected_algorithm = None if requested_algorithm == "select" else requested_algorithm

    if selected_serial and selected_algorithm:
        return selected_serial, selected_algorithm

    def on_mouse(event, x, y, _flags, _userdata):
        nonlocal selected_serial, selected_algorithm
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        for button in serial_buttons:
            bx, by, bw, bh = button.rect
            if bx <= x <= bx + bw and by <= y <= by + bh:
                selected_serial = button.key
                return
        for button in algorithm_buttons:
            bx, by, bw, bh = button.rect
            if button.enabled and bx <= x <= bx + bw and by <= y <= by + bh:
                selected_algorithm = button.key
                return

    cv2.namedWindow(SELECT_WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(SELECT_WINDOW_NAME, 720, 600)
    cv2.setMouseCallback(SELECT_WINDOW_NAME, on_mouse)

    try:
        while True:
            cv2.imshow(
                SELECT_WINDOW_NAME,
                draw_startup_selector(
                    serial_buttons,
                    algorithm_buttons,
                    selected_serial,
                    selected_algorithm,
                ),
            )
            key = cv2.waitKey(50) & 0xFF
            if key in (27, ord("q")):
                raise KeyboardInterrupt("startup selection cancelled")

            if ord("1") <= key <= ord("8"):
                index = key - ord("1")
                if index < len(serial_buttons):
                    selected_serial = serial_buttons[index].key

            algorithm_shortcuts = {
                ord("p"): "pipeline",
                ord("P"): "pipeline",
                ord("a"): "opencv",
                ord("A"): "opencv",
                ord("s"): "wechat",
                ord("S"): "wechat",
                ord("d"): "pyzbar",
                ord("D"): "pyzbar",
                ord("f"): "qreader",
                ord("F"): "qreader",
                ord("g"): "all",
                ord("G"): "all",
            }
            if key in algorithm_shortcuts:
                candidate = algorithm_shortcuts[key]
                for button in algorithm_buttons:
                    if button.key == candidate and button.enabled:
                        selected_algorithm = candidate
                        break

            if key in (10, 13) and selected_serial and selected_algorithm:
                break
            if selected_serial and selected_algorithm:
                break
    finally:
        try:
            cv2.destroyWindow(SELECT_WINDOW_NAME)
        except cv2.error:
            pass

    return selected_serial, selected_algorithm


def resolve_algorithm_keys(
    selected_algorithm: str,
    statuses: dict[str, AlgorithmStatus],
) -> list[str]:
    if selected_algorithm == "all":
        return [key for key in DETECTOR_ORDER if statuses[key].available]
    if selected_algorithm == "pipeline":
        if not statuses["pipeline"].available:
            raise RuntimeError(
                f"{ALGORITHM_LABELS['pipeline']} is unavailable: {statuses['pipeline'].reason}"
            )
        return ["pipeline"]

    status = statuses[selected_algorithm]
    if not status.available:
        raise RuntimeError(f"{status.label} is unavailable: {status.reason}")
    return [selected_algorithm]


def build_detector(key: str, args: argparse.Namespace):
    if key == "pipeline":
        statuses = check_algorithm_statuses(args.wechat_model_dir)
        detectors = [
            build_detector(detector_key, args)
            for detector_key in PIPELINE_ORDER
            if statuses[detector_key].available
        ]
        if not detectors:
            raise RuntimeError("Pipeline QR detector has no available stages.")
        return PipelineQRDetector(detectors)
    if key == "opencv":
        return OpenCVQRDetector()
    if key == "wechat":
        return WeChatQRDetector(args.wechat_model_dir)
    if key == "pyzbar":
        return PyzbarQRDetector()
    if key == "qreader":
        return QReaderQRDetector(args.qreader_model_size)
    raise ValueError(f"unknown algorithm: {key}")


def build_corrector(args: argparse.Namespace):
    if args.fisheye:
        return FisheyeCorrector(
            f_scale=args.fisheye_f_scale,
            zoom=args.fisheye_zoom,
            output_zoom=args.fisheye_output_zoom,
            resolution_scale=args.fisheye_resolution_scale,
        )
    if args.rectify:
        return RectifyCorrector(args.rectify_calib, args.rectify_alpha)
    return None


def build_fisheye_corrector(args: argparse.Namespace) -> FisheyeCorrector:
    return FisheyeCorrector(
        f_scale=args.fisheye_f_scale,
        zoom=args.fisheye_zoom,
        output_zoom=args.fisheye_output_zoom,
        resolution_scale=args.fisheye_resolution_scale,
    )


def build_compare_correctors(args: argparse.Namespace):
    return {
        "raw": None,
        "fisheye": build_fisheye_corrector(args),
        "rectify": RectifyCorrector(args.rectify_calib, args.rectify_alpha),
    }


def run_detectors(detectors, frame) -> tuple[list[QRDetection], float]:
    start = time.perf_counter()
    detections: list[QRDetection] = []
    for detector in detectors:
        try:
            detections.extend(detector.detect(frame))
        except Exception as exc:
            print(f"warning: {detector.label} failed: {exc}")
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return detections, elapsed_ms


def create_preview_window(width: int, height: int) -> None:
    flags = cv2.WINDOW_NORMAL
    if hasattr(cv2, "WINDOW_KEEPRATIO"):
        flags |= cv2.WINDOW_KEEPRATIO
    cv2.namedWindow(WINDOW_NAME, flags)
    cv2.resizeWindow(WINDOW_NAME, width, height)


def save_capture(frame) -> Path:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    save_path = CAPTURE_DIR / f"qr_capture_{timestamp}.png"
    ok = cv2.imwrite(str(save_path), frame)
    if not ok:
        raise RuntimeError(f"failed to save capture: {save_path}")
    return save_path


def make_video_writer(frame_shape, fps: float):
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    h, w = frame_shape[:2]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_path = CAPTURE_DIR / f"qr_record_{timestamp}.mp4"

    for fourcc_str, ext in (
        ("mp4v", ".mp4"),
        ("avc1", ".mp4"),
        ("XVID", ".avi"),
        ("MJPG", ".avi"),
    ):
        candidate_path = base_path.with_suffix(ext)
        fourcc = cv2.VideoWriter_fourcc(*fourcc_str)
        writer = cv2.VideoWriter(str(candidate_path), fourcc, fps, (w, h))
        if writer.isOpened():
            return writer, candidate_path, fourcc_str

    raise RuntimeError("failed to open VideoWriter with mp4v/avc1/XVID/MJPG")


def clip_text(text: str, max_chars: int = 78) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def put_outlined_text(frame, text: str, origin, scale, color, thickness=1):
    cv2.putText(
        frame,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (0, 0, 0),
        thickness + 3,
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        color,
        thickness,
        cv2.LINE_AA,
    )


def detection_anchor(detection: QRDetection) -> tuple[int, int, int, int] | None:
    if detection.points is not None and len(detection.points) >= 4:
        points = np.asarray(detection.points, dtype=np.int32).reshape(-1, 2)
        x = int(np.min(points[:, 0]))
        y = int(np.min(points[:, 1]))
        center_x = int(np.mean(points[:, 0]))
        center_y = int(np.mean(points[:, 1]))
        return x, y, center_x, center_y

    if detection.rect is not None:
        x, y, w, h = detection.rect
        return x, y, int(x + w / 2), int(y + h / 2)

    return None


def scale_detections(
    detections: list[QRDetection],
    scale: float,
) -> list[QRDetection]:
    if np.isclose(scale, 1.0):
        return detections

    scaled: list[QRDetection] = []
    for detection in detections:
        points = None
        rect = None
        if detection.points is not None:
            points = np.round(np.asarray(detection.points, dtype=np.float32) * scale).astype(
                np.int32
            )
        if detection.rect is not None:
            x, y, w, h = detection.rect
            rect = (
                int(round(x * scale)),
                int(round(y * scale)),
                max(1, int(round(w * scale))),
                max(1, int(round(h * scale))),
            )
        scaled.append(
            QRDetection(
                detection.algorithm,
                detection.label,
                detection.text,
                points=points,
                rect=rect,
            )
        )
    return scaled


def draw_detections(frame, detections: list[QRDetection]) -> None:
    fallback_y = 104
    stacked_counts: dict[tuple[str, int, int], int] = {}
    stack_spacing = 24

    for detection in detections:
        color = ALGORITHM_COLORS.get(detection.algorithm, (255, 255, 255))
        label = f"{detection.label}: {clip_text(detection.text)}"
        text_origin = (20, fallback_y)
        anchor = detection_anchor(detection)

        if anchor is not None and detection.points is not None and len(detection.points) >= 4:
            points = np.asarray(detection.points, dtype=np.int32).reshape(-1, 2)
            cv2.polylines(frame, [points], True, color, 2, cv2.LINE_AA)
        elif anchor is not None and detection.rect is not None:
            x, y, w, h = detection.rect
            cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2, cv2.LINE_AA)

        if anchor is not None:
            x, y, center_x, center_y = anchor
            stack_key = (detection.text, center_x // 80, center_y // 80)
            stack_index = stacked_counts.get(stack_key, 0)
            stacked_counts[stack_key] = stack_index + 1
            text_origin = (max(8, x), max(24, y - 10 - stack_index * stack_spacing))
        else:
            fallback_y += 26

        put_outlined_text(frame, label, text_origin, 0.56, color, 2)


def draw_status_bar(
    display_frame,
    selected_label: str,
    detector_labels: list[str],
    detections: list[QRDetection],
    recording: bool,
    interval: int,
    stats: RecognitionStats | None = None,
) -> None:
    h, w = display_frame.shape[:2]
    bar_h = min(86, max(64, h // 12))
    overlay = display_frame.copy()
    cv2.rectangle(overlay, (0, 0), (w, bar_h), (18, 20, 24), -1)
    cv2.addWeighted(overlay, 0.72, display_frame, 0.28, 0, display_frame)

    active_text = ", ".join(detector_labels)
    stats_text = ""
    if stats is not None and stats.attempts > 0:
        stats_text = (
            f"  rate: {stats.hits}/{stats.attempts} "
            f"({stats.rate:.1f}%)  det: {stats.last_ms:.1f}ms"
        )
    put_outlined_text(
        display_frame,
        f"QR: {selected_label}  active: {active_text}  interval: {interval}  hits: {len(detections)}{stats_text}",
        (18, 30),
        0.58,
        (245, 245, 245),
        1,
    )
    put_outlined_text(
        display_frame,
        "c: snapshot   r: record   e: stop record   q/esc: quit",
        (18, 62),
        0.5,
        (205, 215, 225),
        1,
    )
    if recording:
        put_outlined_text(display_frame, "REC", (w - 96, 36), 0.9, (0, 0, 255), 3)


def draw_compare_header(
    frame,
    title: str,
    selected_label: str,
    stats: RecognitionStats,
    detections: list[QRDetection],
) -> None:
    h, w = frame.shape[:2]
    bar_h = min(92, max(72, h // 11))
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (w, bar_h), (18, 20, 24), -1)
    cv2.addWeighted(overlay, 0.72, frame, 0.28, 0, frame)

    put_outlined_text(
        frame,
        f"{title} | {selected_label}",
        (16, 30),
        0.68,
        (245, 245, 245),
        2,
    )
    put_outlined_text(
        frame,
        (
            f"rate {stats.hits}/{stats.attempts} ({stats.rate:.1f}%) "
            f"hits {len(detections)} det {stats.last_ms:.1f}ms"
        ),
        (16, 62),
        0.52,
        (205, 215, 225),
        1,
    )


def stack_compare_frames(frames: list[np.ndarray]) -> np.ndarray:
    if not frames:
        raise ValueError("frames must not be empty")
    target_h = min(frame.shape[0] for frame in frames)
    resized = []
    for frame in frames:
        h, w = frame.shape[:2]
        if h != target_h:
            new_w = max(1, int(round(w * target_h / h)))
            frame = cv2.resize(frame, (new_w, target_h), interpolation=cv2.INTER_AREA)
        resized.append(frame)
    return np.hstack(resized)


def print_new_detections(
    detections: list[QRDetection],
    last_printed: set[tuple[str, str]],
    prefix: str = "",
) -> set[tuple[str, str]]:
    current = {(detection.algorithm, detection.text) for detection in detections}
    if current and current != last_printed:
        for algorithm, text in sorted(current):
            print(
                f"Detected {prefix}[{ALGORITHM_LABELS.get(algorithm, algorithm)}]: {text}"
            )
    return current


def print_stats_summary(stats_by_view: dict[str, RecognitionStats]) -> None:
    if not stats_by_view:
        return
    print("\n=== QR recognition summary ===")
    for view_key, stat in stats_by_view.items():
        if stat.attempts == 0:
            print(f"{view_key}: no inference samples")
            continue
        print(
            f"{view_key}: {stat.hits}/{stat.attempts} hits "
            f"({stat.rate:.1f}%), det={stat.last_ms:.1f}ms"
        )
    print("==============================\n")


def main() -> None:
    args = parse_args()
    statuses = check_algorithm_statuses(args.wechat_model_dir)

    try:
        selected_serial, selected_algorithm = select_startup_options(
            statuses,
            args.serial,
            args.algorithm,
        )
    except KeyboardInterrupt:
        print("startup selection cancelled")
        return

    detector_keys = resolve_algorithm_keys(selected_algorithm, statuses)
    detectors = [build_detector(key, args) for key in detector_keys]
    corrector = None if args.compare_corrections else build_corrector(args)
    compare_correctors = build_compare_correctors(args) if args.compare_corrections else None

    pipeline = make_pipeline(selected_serial, args.mode)
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)

    print("script_dir:", SCRIPT_DIR)
    print("capture_dir:", CAPTURE_DIR)
    print("pipeline:", pipeline)
    print("opened:", cap.isOpened())
    print("selected_serial:", selected_serial)
    print("selected_algorithm:", selected_algorithm)
    print("detectors:", ", ".join(detector.label for detector in detectors))
    print("qr_interval:", args.qr_interval)
    print("display_scale:", args.display_scale)
    if corrector is not None:
        print("correction:", "fisheye" if args.fisheye else "rectify")
    if compare_correctors is not None:
        print("correction_compare: raw, fisheye, rectify")

    if not cap.isOpened():
        raise RuntimeError("failed to open camera via GStreamer/tcamsrc")

    window_width = args.window_width or DEFAULT_WINDOW_WIDTH
    window_height = args.window_height or DEFAULT_WINDOW_HEIGHT
    create_preview_window(window_width, window_height)

    writer = None
    recording = False
    record_path = None
    record_codec = None
    fps = MODE_FPS[args.mode]
    frame_index = 0
    cached_detections: list[QRDetection] = []
    stats = RecognitionStats()
    last_printed: set[tuple[str, str]] = set()
    compare_cached = {key: [] for key in ("raw", "fisheye", "rectify")}
    compare_stats = {key: RecognitionStats() for key in ("raw", "fisheye", "rectify")}
    compare_last_printed = {key: set() for key in ("raw", "fisheye", "rectify")}

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                continue

            if compare_correctors is not None:
                variant_frames = {}
                for view_key, view_corrector in compare_correctors.items():
                    if view_corrector is None:
                        variant_frames[view_key] = frame
                    else:
                        variant_frames[view_key] = view_corrector.apply(frame)

                if frame_index % args.qr_interval == 0:
                    for view_key, view_frame in variant_frames.items():
                        detections, elapsed_ms = run_detectors(detectors, view_frame)
                        compare_cached[view_key] = detections
                        compare_stats[view_key].update(detections, elapsed_ms)
                        compare_last_printed[view_key] = print_new_detections(
                            detections,
                            compare_last_printed[view_key],
                            prefix=f"{view_key} ",
                        )

                frame_index += 1

                panels = []
                for view_key, title in (
                    ("raw", "RAW"),
                    ("fisheye", "FISHEYE"),
                    ("rectify", "RECTIFY"),
                ):
                    panel = resize_for_display(
                        variant_frames[view_key].copy(),
                        args.display_scale,
                    )
                    draw_detections(
                        panel,
                        scale_detections(compare_cached[view_key], args.display_scale),
                    )
                    draw_compare_header(
                        panel,
                        title,
                        ALGORITHM_LABELS[selected_algorithm],
                        compare_stats[view_key],
                        compare_cached[view_key],
                    )
                    panels.append(panel)

                annotated_frame = stack_compare_frames(panels)
            else:
                if corrector is not None:
                    frame = corrector.apply(frame)

                if frame_index % args.qr_interval == 0:
                    cached_detections, elapsed_ms = run_detectors(detectors, frame)
                    stats.update(cached_detections, elapsed_ms)
                    last_printed = print_new_detections(cached_detections, last_printed)

                frame_index += 1

                annotated_frame = frame.copy()
                draw_detections(annotated_frame, cached_detections)

            if recording and writer is not None:
                writer.write(annotated_frame)

            if compare_correctors is not None:
                display_frame = annotated_frame
                if recording:
                    put_outlined_text(
                        display_frame,
                        "REC",
                        (display_frame.shape[1] - 96, 36),
                        0.9,
                        (0, 0, 255),
                        3,
                    )
            else:
                display_frame = resize_for_display(annotated_frame, args.display_scale)
                draw_status_bar(
                    display_frame,
                    ALGORITHM_LABELS[selected_algorithm],
                    [detector.label for detector in detectors],
                    cached_detections,
                    recording,
                    args.qr_interval,
                    stats,
                )

            cv2.imshow(WINDOW_NAME, display_frame)
            key = cv2.waitKey(1) & 0xFF

            if key == ord("c"):
                save_path = save_capture(annotated_frame)
                print(f"saved image: {save_path}")

            elif key == ord("r"):
                if not recording:
                    writer, record_path, record_codec = make_video_writer(
                        annotated_frame.shape,
                        fps,
                    )
                    recording = True
                    print(
                        f"recording started: {record_path} "
                        f"(codec={record_codec}, fps={fps})"
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
        if compare_correctors is not None:
            print_stats_summary(compare_stats)
        else:
            print_stats_summary({"current": stats})
        if writer is not None:
            writer.release()
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
