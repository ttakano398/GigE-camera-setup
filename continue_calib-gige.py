import argparse
import logging
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

import cv2
import numpy as np

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Tcam", "1.0")

from gi.repository import GLib, Gst, Tcam  # noqa: F401


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


DEFAULT_SERIAL = "08520932"
WINDOW_NAME = "Calibration"

TARGET_BRIGHTNESS = 119
THRESHOLD = 5
DEFAULT_CONTROLLER_GAIN = 0.5

MIN_EXPOSURE_LIMIT = 100.0
MAX_EXPOSURE_LIMIT = 10_000_000.0

MIN_WB_LIMIT = 0.0
MAX_WB_LIMIT = 3.984375

INITIAL_EXPOSURE_US = 15_000.0
INITIAL_GAIN_DB = 0.0
INITIAL_WB_RED = 1.55
INITIAL_WB_GREEN = 1.0
INITIAL_WB_BLUE = 1.55

SMOOTHING_WINDOW = 5
SKIP_FRAMES_AFTER_CMD = 3

INITIAL_WINDOW_WIDTH = 1280
INITIAL_WINDOW_HEIGHT = 720

FISHEYE_DEFAULT_F_SCALE = 0.14
FISHEYE_DEFAULT_ZOOM = 3.0
FISHEYE_DEFAULT_OUTPUT_ZOOM = 2.0
FISHEYE_DEFAULT_RESOLUTION_SCALE = 1.0

H_REF = 1000.0
MARKER_WIDTH = H_REF * 0.85
OFFSET = (H_REF - MARKER_WIDTH) / 2

DEFAULT_DIRECTION_IDX = 1
DEFAULT_ID_MODE_IDX = 0
ID_TARGET_SETS = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [0]]
DIRECTION_LABELS = ["Right", "Down", "Left", "Up"]
DIRECTION_SHORT_LABELS = ["R", "D", "L", "U"]

SEP_EXP_FINE, SEP_WB_RED, SEP_WB_BLUE, SEP_DONE = 0, 1, 2, 3
SEP_PHASE_LABELS = {
    SEP_EXP_FINE: "EXP",
    SEP_WB_RED: "WB_R",
    SEP_WB_BLUE: "WB_B",
    SEP_DONE: "DONE",
}

MARKER_DICTS = {
    "aruco": cv2.aruco.DICT_4X4_50,
    "apriltag": cv2.aruco.DICT_APRILTAG_36h11,
}


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
        radius = np.sqrt(dx ** 2 + dy ** 2)

        focal = self.f_scale * min(width, height)
        theta = np.arctan2(radius, focal)
        cv_map = focal * theta

        eps = 1e-8
        map_x = (cx + (dx * self.zoom / (radius + eps)) * cv_map).astype(np.float32)
        map_y = (cy + (dy * self.zoom / (radius + eps)) * cv_map).astype(np.float32)
        return map_x, map_y

    def _apply_output_zoom(self, frame: np.ndarray) -> np.ndarray:
        if self.output_zoom <= 1.0:
            return frame
        h, w = frame.shape[:2]
        crop_w = max(1, int(round(w / self.output_zoom)))
        crop_h = max(1, int(round(h / self.output_zoom)))
        x0 = max(0, (w - crop_w) // 2)
        y0 = max(0, (h - crop_h) // 2)
        cropped = frame[y0:y0 + crop_h, x0:x0 + crop_w]
        return cv2.resize(cropped, (w, h), interpolation=cv2.INTER_LINEAR)

    def apply(self, frame: np.ndarray) -> np.ndarray:
        if frame is None:
            return frame
        h, w = frame.shape[:2]
        config = (w, h, self.resolution_scale)
        if self.map_x is None or self.current_config != config:
            self.map_x, self.map_y = self._calculate_maps(w, h)
            self.current_config = config
        corrected = cv2.remap(frame, self.map_x, self.map_y, interpolation=cv2.INTER_LINEAR)
        return self._apply_output_zoom(corrected)


def create_bgr_history() -> Deque[np.ndarray]:
    return deque(maxlen=SMOOTHING_WINDOW)


@dataclass
class CameraMode:
    width: int
    height: int
    fps: int


MODE_MAX = CameraMode(width=2592, height=1944, fps=22)
MODE_FHD = CameraMode(width=1920, height=1080, fps=30)


@dataclass
class CameraState:
    serial: str = DEFAULT_SERIAL
    mode: CameraMode = field(default_factory=lambda: MODE_FHD)

    exposure_auto: bool = False
    gain_auto: bool = False
    wb_auto: bool = False

    exposure_us: float = INITIAL_EXPOSURE_US
    gain_db: float = INITIAL_GAIN_DB
    wb_red: float = INITIAL_WB_RED
    wb_green: float = INITIAL_WB_GREEN
    wb_blue: float = INITIAL_WB_BLUE


@dataclass
class CalibrationState:
    calib_active: bool = False
    sep_phase: int = SEP_EXP_FINE
    controller_gain: float = DEFAULT_CONTROLLER_GAIN
    prev_sign: int = 0
    opt_steps: int = 0
    bgr_hist: Deque[np.ndarray] = field(default_factory=create_bgr_history)
    current_direction_idx: int = DEFAULT_DIRECTION_IDX
    id_mode_idx: int = DEFAULT_ID_MODE_IDX


def clamp(val: float, min_v: float, max_v: float) -> float:
    return max(min_v, min(val, max_v))


def approx_equal(lhs: float, rhs: float, epsilon: float = 1e-6) -> bool:
    return abs(lhs - rhs) <= epsilon


def create_aruco_parameters():
    aruco = cv2.aruco

    if hasattr(aruco, "DetectorParameters"):
        try:
            return aruco.DetectorParameters()
        except Exception:
            pass

    if hasattr(aruco, "DetectorParameters_create"):
        try:
            return aruco.DetectorParameters_create()
        except Exception:
            pass

    return None


def detect_markers(gray: np.ndarray, aruco_dict):
    aruco = cv2.aruco
    params = create_aruco_parameters()

    if hasattr(aruco, "ArucoDetector"):
        detector = aruco.ArucoDetector(aruco_dict, params) if params is not None else aruco.ArucoDetector(aruco_dict)
        return detector.detectMarkers(gray)

    if hasattr(aruco, "detectMarkers"):
        if params is not None:
            return aruco.detectMarkers(gray, aruco_dict, parameters=params)
        return aruco.detectMarkers(gray, aruco_dict)

    attrs = [x for x in dir(aruco) if "Detector" in x or "detect" in x or "Parameters" in x]
    raise RuntimeError(
        f"No usable ArUco API in this runtime. cv2={cv2.__version__}, attrs={attrs}"
    )


def adjust_param(diff: float, gain: float, current_param_val: float, min_limit: float, max_limit: float):
    abs_diff = abs(diff)

    if abs_diff <= THRESHOLD:
        return False, current_param_val

    step = max(1e-6, abs_diff * gain)
    new_val = current_param_val - step if diff > 0 else current_param_val + step
    new_val = clamp(new_val, min_limit, max_limit)

    if not approx_equal(new_val, current_param_val):
        return True, new_val

    return False, current_param_val


def init_camera_settings(state: CameraState) -> None:
    state.exposure_auto = False
    state.gain_auto = False
    state.wb_auto = False
    state.exposure_us = INITIAL_EXPOSURE_US
    state.gain_db = INITIAL_GAIN_DB
    state.wb_red = INITIAL_WB_RED
    state.wb_green = INITIAL_WB_GREEN
    state.wb_blue = INITIAL_WB_BLUE


def build_tcam_property_summary(state: CameraState) -> str:
    props = [
        f"ExposureAuto={'Continuous' if state.exposure_auto else 'Off'}",
        f"GainAuto={'Continuous' if state.gain_auto else 'Off'}",
        f"BalanceWhiteAuto={'Continuous' if state.wb_auto else 'Off'}",
    ]

    if not state.exposure_auto:
        props.append(f"ExposureTime={state.exposure_us:.3f}")

    if not state.gain_auto:
        props.append(f"Gain={state.gain_db:.3f}")

    if not state.wb_auto:
        props.append(f"BalanceWhiteRed={state.wb_red:.6f}")
        props.append(f"BalanceWhiteGreen={state.wb_green:.6f}")
        props.append(f"BalanceWhiteBlue={state.wb_blue:.6f}")

    return "tcam," + ",".join(props)


def print_camera_state(state: CameraState) -> None:
    logging.info(
        "CameraState | mode=%dx%d@%dfps | AE=%s AGC=%s AWB=%s | Exp(us)=%.3f Gain(dB)=%.3f | WB(R,G,B)=(%.6f, %.6f, %.6f)",
        state.mode.width,
        state.mode.height,
        state.mode.fps,
        state.exposure_auto,
        state.gain_auto,
        state.wb_auto,
        state.exposure_us,
        state.gain_db,
        state.wb_red,
        state.wb_green,
        state.wb_blue,
    )


def generate_apply_summary(state: CameraState) -> None:
    print(f"\n{'=' * 60}")
    print("[現在設定]")
    print(f"serial            : {state.serial}")
    print(f"resolution        : {state.mode.width}x{state.mode.height}")
    print(f"fps               : {state.mode.fps}")
    print(f"ExposureAuto      : {'Continuous' if state.exposure_auto else 'Off'}")
    print(f"GainAuto          : {'Continuous' if state.gain_auto else 'Off'}")
    print(f"BalanceWhiteAuto  : {'Continuous' if state.wb_auto else 'Off'}")
    print(f"ExposureTime(us)  : {state.exposure_us:.3f}")
    print(f"Gain(dB)          : {state.gain_db:.3f}")
    print(f"BalanceWhiteRed   : {state.wb_red:.6f}")
    print(f"BalanceWhiteGreen : {state.wb_green:.6f}")
    print(f"BalanceWhiteBlue  : {state.wb_blue:.6f}")
    print("\n[tcam-properties]")
    print(build_tcam_property_summary(state))
    print(f"{'=' * 60}\n")


class GigECameraController:
    def __init__(self, state: CameraState):
        Gst.init(None)
        self.state = state
        self.pipeline: Optional[Gst.Pipeline] = None
        self.source = None
        self.appsink = None
        self.bus = None

    def describe_pipeline(self) -> str:
        mode = self.state.mode
        return (
            f'tcamsrc serial="{self.state.serial}" type="aravis" ! '
            f"video/x-bayer,format=grbg,width={mode.width},height={mode.height},framerate={mode.fps}/1 ! "
            "bayer2rgb ! videoconvert ! video/x-raw,format=BGR ! appsink sync=false drop=true max-buffers=1"
        )

    def _make_element(self, factory: str, name: str):
        element = Gst.ElementFactory.make(factory, name)
        if element is None:
            raise RuntimeError(f"failed to create GStreamer element: {factory}")
        return element

    def _wait_for_state(self, target: Gst.State, timeout_sec: float = 5.0) -> None:
        if self.pipeline is None:
            raise RuntimeError("pipeline is not initialized")

        result, current, pending = self.pipeline.get_state(int(timeout_sec * Gst.SECOND))
        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"failed to reach GStreamer state {target}")

        if current != target and pending != target:
            raise RuntimeError(
                f"unexpected GStreamer state. current={current}, pending={pending}, target={target}"
            )

    def _create_pipeline(self) -> None:
        mode = self.state.mode
        pipeline = Gst.Pipeline.new("gige-calibration-pipeline")
        if pipeline is None:
            raise RuntimeError("failed to create GStreamer pipeline")

        source = self._make_element("tcamsrc", "source")
        bayer_caps = self._make_element("capsfilter", "bayer_caps")
        debayer = self._make_element("bayer2rgb", "debayer")
        convert = self._make_element("videoconvert", "convert")
        bgr_caps = self._make_element("capsfilter", "bgr_caps")
        appsink = self._make_element("appsink", "sink")

        source.set_property("serial", self.state.serial)
        source.set_property("type", "aravis")

        bayer_caps.set_property(
            "caps",
            Gst.Caps.from_string(
                f"video/x-bayer,format=grbg,width={mode.width},height={mode.height},framerate={mode.fps}/1"
            ),
        )
        bgr_caps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=BGR"))

        appsink.set_property("sync", False)
        appsink.set_property("drop", True)
        appsink.set_property("max-buffers", 1)
        appsink.set_property("emit-signals", False)

        for element in (source, bayer_caps, debayer, convert, bgr_caps, appsink):
            pipeline.add(element)

        if not Gst.Element.link_many(source, bayer_caps, debayer, convert, bgr_caps, appsink):
            raise RuntimeError("failed to link GStreamer pipeline")

        self.pipeline = pipeline
        self.source = source
        self.appsink = appsink
        self.bus = pipeline.get_bus()

    def _drain_bus(self) -> None:
        if self.bus is None:
            return

        mask = Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING
        while True:
            message = self.bus.timed_pop_filtered(0, mask)
            if message is None:
                break

            if message.type == Gst.MessageType.WARNING:
                err, debug = message.parse_warning()
                logging.warning("GStreamer warning: %s | debug=%s", err, debug)
                continue

            if message.type == Gst.MessageType.EOS:
                raise RuntimeError("GStreamer pipeline reached EOS unexpectedly")

            if message.type == Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                raise RuntimeError(f"GStreamer error: {err} | debug={debug}")

    def start(self) -> None:
        self.stop()
        self._create_pipeline()
        logging.info("Opening pipeline:")
        logging.info(self.describe_pipeline())

        if self.pipeline.set_state(Gst.State.READY) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("failed to set pipeline to READY")
        self._wait_for_state(Gst.State.READY)

        if not self.apply_manual_state():
            raise RuntimeError("failed to apply initial camera state")

        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("failed to set pipeline to PLAYING")
        self._wait_for_state(Gst.State.PLAYING)
        self._drain_bus()

    def stop(self) -> None:
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.source = None
            self.appsink = None
            self.bus = None

    def restart(self) -> None:
        self.start()

    def switch_mode(self, mode: CameraMode) -> None:
        self.state.mode = mode
        logging.info("Switching mode to %dx%d@%dfps", mode.width, mode.height, mode.fps)
        self.restart()

    def _set_enum(self, name: str, value: str) -> None:
        if self.source is None:
            raise RuntimeError("source is not initialized")
        self.source.set_tcam_enumeration(name, value)

    def _set_float(self, name: str, value: float) -> None:
        if self.source is None:
            raise RuntimeError("source is not initialized")
        self.source.set_tcam_float(name, float(value))

    def apply_manual_state(self) -> bool:
        if self.source is None:
            raise RuntimeError("source is not initialized")

        try:
            self._set_enum("ExposureAuto", "Continuous" if self.state.exposure_auto else "Off")
            self._set_enum("GainAuto", "Continuous" if self.state.gain_auto else "Off")
            self._set_enum("BalanceWhiteAuto", "Continuous" if self.state.wb_auto else "Off")

            if not self.state.exposure_auto:
                self._set_float("ExposureTime", self.state.exposure_us)
                logging.info("ExposureTime -> %.3f us", self.state.exposure_us)

            if not self.state.gain_auto:
                self._set_float("Gain", self.state.gain_db)
                logging.info("Gain -> %.3f dB", self.state.gain_db)

            if not self.state.wb_auto:
                self._set_float("BalanceWhiteRed", self.state.wb_red)
                self._set_float("BalanceWhiteGreen", self.state.wb_green)
                self._set_float("BalanceWhiteBlue", self.state.wb_blue)
                logging.info(
                    "WhiteBalance -> R=%.6f G=%.6f B=%.6f",
                    self.state.wb_red,
                    self.state.wb_green,
                    self.state.wb_blue,
                )

            self._drain_bus()
            return True
        except GLib.Error as exc:
            logging.error("Failed to apply camera properties: %s", exc)
            return False

    def read_frame(self, timeout_ms: int = 250) -> Optional[np.ndarray]:
        if self.appsink is None:
            raise RuntimeError("appsink is not initialized")

        self._drain_bus()
        sample = self.appsink.emit("try-pull-sample", timeout_ms * 1_000_000)
        if sample is None:
            return None

        buffer = sample.get_buffer()
        caps = sample.get_caps()
        if buffer is None or caps is None:
            return None

        structure = caps.get_structure(0)
        width = structure.get_value("width")
        height = structure.get_value("height")
        channels = 3

        ok, map_info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            logging.warning("Failed to map GStreamer buffer")
            return None

        try:
            frame = np.frombuffer(map_info.data, dtype=np.uint8)
            expected = int(width) * int(height) * channels
            if frame.size < expected:
                logging.warning(
                    "Unexpected frame size. expected=%d actual=%d", expected, frame.size
                )
                return None
            frame = frame[:expected].reshape((int(height), int(width), channels)).copy()
            return frame
        finally:
            buffer.unmap(map_info)


class CalibrationEngine:
    def __init__(self, marker_type: str):
        self.marker_type = marker_type
        self.marker_label = "APRILTAG" if marker_type == "apriltag" else "ARUCO"
        self.aruco_dict = cv2.aruco.getPredefinedDictionary(MARKER_DICTS[marker_type])
        self.font = cv2.FONT_HERSHEY_SIMPLEX
        self.state = CalibrationState()
        self.virtual_marker_pts = None
        self.virtual_roi_pts = None
        self.update_roi_geometry()

    def update_roi_geometry(self) -> None:
        self.virtual_marker_pts = np.array(
            [
                [OFFSET, OFFSET],
                [OFFSET + MARKER_WIDTH, OFFSET],
                [OFFSET + MARKER_WIDTH, OFFSET + MARKER_WIDTH],
                [OFFSET, OFFSET + MARKER_WIDTH],
            ],
            dtype=np.float32,
        )

        dir_map = {
            0: (1.0, 3.0, 0.0, 1.0),
            1: (0.0, 1.0, 1.0, 3.0),
            2: (-2.0, 0.0, 0.0, 1.0),
            3: (0.0, 1.0, -2.0, 0.0),
        }
        xm_s, xm_e, ym_s, ym_e = dir_map[self.state.current_direction_idx]
        margin = H_REF * 0.25

        x_s = (H_REF * xm_s) + margin
        x_e = (H_REF * xm_e) - margin
        y_s = (H_REF * ym_s) + margin
        y_e = (H_REF * ym_e) - margin

        pts = np.array([[x_s, y_s], [x_e, y_s], [x_e, y_e], [x_s, y_e]], dtype=np.float32)
        self.virtual_roi_pts = pts.reshape(-1, 1, 2)

        logging.info("Direction Updated: %s", self.current_direction_label())

    def current_direction_label(self) -> str:
        return DIRECTION_LABELS[self.state.current_direction_idx]

    def current_direction_short(self) -> str:
        return DIRECTION_SHORT_LABELS[self.state.current_direction_idx]

    def current_target_ids(self):
        return ID_TARGET_SETS[self.state.id_mode_idx]

    def cycle_direction(self) -> None:
        self.state.current_direction_idx = (self.state.current_direction_idx + 1) % 4
        self.update_roi_geometry()
        self.clear_bgr_history()

    def cycle_target_ids(self) -> None:
        self.state.id_mode_idx = (self.state.id_mode_idx + 1) % len(ID_TARGET_SETS)
        logging.info("Target IDs: %s", self.current_target_ids())
        self.clear_bgr_history()

    def clear_bgr_history(self) -> None:
        self.state.bgr_hist.clear()

    def reset_runtime(self, keep_switches: bool = True) -> None:
        direction_idx = self.state.current_direction_idx if keep_switches else DEFAULT_DIRECTION_IDX
        id_mode_idx = self.state.id_mode_idx if keep_switches else DEFAULT_ID_MODE_IDX
        self.state = CalibrationState(
            current_direction_idx=direction_idx,
            id_mode_idx=id_mode_idx,
        )
        self.update_roi_geometry()

    def toggle_calibration(self) -> bool:
        self.state.calib_active = not self.state.calib_active
        logging.info("Calib: %s", self.state.calib_active)
        if self.state.calib_active:
            self.clear_bgr_history()
            self.state.opt_steps = 0
            self.state.sep_phase = SEP_EXP_FINE
            self.state.controller_gain = DEFAULT_CONTROLLER_GAIN
            self.state.prev_sign = 0
        else:
            self.stop_calibration()
        return self.state.calib_active

    def stop_calibration(self) -> None:
        self.state.calib_active = False
        self.clear_bgr_history()
        self.state.opt_steps = 0
        self.state.sep_phase = SEP_EXP_FINE
        self.state.controller_gain = DEFAULT_CONTROLLER_GAIN
        self.state.prev_sign = 0

    def analyze_frame(self, frame: np.ndarray):
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean_bri = float(np.mean(gray))
        corners, ids, _ = detect_markers(gray, self.aruco_dict)

        target_bgr = None
        roi_bgrs = []
        current_targets = self.current_target_ids()

        if ids is not None:
            for i, marker_id in enumerate(ids.flatten()):
                if marker_id not in current_targets:
                    continue

                try:
                    transform = cv2.getPerspectiveTransform(
                        self.virtual_marker_pts, corners[i][0].astype(np.float32)
                    )
                    dst_roi = (
                        cv2.perspectiveTransform(self.virtual_roi_pts, transform)
                        .reshape(-1, 2)
                        .astype(np.int32)
                    )

                    mask = np.zeros((h, w), dtype=np.uint8)
                    cv2.fillPoly(mask, [dst_roi], 255)
                    roi_bgrs.append(cv2.mean(frame, mask=mask)[:3])
                    cv2.polylines(frame, [dst_roi], True, (0, 255, 0), 2, cv2.LINE_AA)
                except Exception as exc:
                    logging.debug("ROI transform failed for marker %s: %s", marker_id, exc)

            cv2.aruco.drawDetectedMarkers(frame, corners, ids)

        if roi_bgrs:
            self.state.bgr_hist.append(np.mean(roi_bgrs, axis=0))
            target_bgr = np.mean(self.state.bgr_hist, axis=0)
        elif self.state.bgr_hist:
            self.state.bgr_hist.popleft()

        return mean_bri, target_bgr, current_targets

    def auto_calibrate_sep(self, bgr, camera_state: CameraState, global_bri: float = 0.0):
        exp = camera_state.exposure_us
        phase = self.state.sep_phase

        if bgr is None:
            if global_bri > 200 and exp > MIN_EXPOSURE_LIMIT:
                camera_state.exposure_us = max(MIN_EXPOSURE_LIMIT, exp - 5000.0)
                logging.info("marker loss recovery: bright -> ExposureTime %.3f", camera_state.exposure_us)
                return True, "RECOVERY (BRIGHT)"
            if global_bri < 30 and exp < MAX_EXPOSURE_LIMIT:
                camera_state.exposure_us = min(MAX_EXPOSURE_LIMIT, exp + 5000.0)
                logging.info("marker loss recovery: dark -> ExposureTime %.3f", camera_state.exposure_us)
                return True, "RECOVERY (DARK)"
            return False, "NO MARKER"

        b, g, r = bgr

        exp_ok = abs(g - TARGET_BRIGHTNESS) <= THRESHOLD
        red_ok = abs(r - g) <= THRESHOLD
        blue_ok = abs(b - g) <= THRESHOLD

        if phase == SEP_DONE:
            if not exp_ok:
                self._set_phase(SEP_EXP_FINE)
            elif not red_ok:
                self._set_phase(SEP_WB_RED)
            elif not blue_ok:
                self._set_phase(SEP_WB_BLUE)
            else:
                return False, "HOLD"
            phase = self.state.sep_phase

        if phase == SEP_EXP_FINE:
            diff = g - TARGET_BRIGHTNESS

            if abs(diff) > THRESHOLD:
                current_sign = 1 if diff > 0 else -1
                if self.state.prev_sign != 0 and self.state.prev_sign != current_sign:
                    self.state.controller_gain = max(0.01, self.state.controller_gain * 0.5)
                    logging.info("Damping: controller_gain -> %.3f", self.state.controller_gain)
                self.state.prev_sign = current_sign

                step = int(abs(diff) * self.state.controller_gain * 100.0)
                max_rel_step = max(100, int(exp * 0.3))
                step = min(step, max_rel_step)
                step = max(100, step)

                new_exp = exp - step if diff > 0 else exp + step
                new_exp = clamp(new_exp, MIN_EXPOSURE_LIMIT, MAX_EXPOSURE_LIMIT)

                if not approx_equal(new_exp, exp):
                    camera_state.exposure_us = float(new_exp)
                    return True, "EXP_FINE"

            self._set_phase(SEP_WB_RED)
            return False, "EXP_OK->RED"

        if phase == SEP_WB_RED:
            updated, val = adjust_param(
                r - g, 0.002, camera_state.wb_red, MIN_WB_LIMIT, MAX_WB_LIMIT
            )
            if updated:
                camera_state.wb_red = float(val)
                return True, "WB_RED"

            self._set_phase(SEP_WB_BLUE)
            return False, "RED_OK->BLUE"

        if phase == SEP_WB_BLUE:
            updated, val = adjust_param(
                b - g, 0.002, camera_state.wb_blue, MIN_WB_LIMIT, MAX_WB_LIMIT
            )
            if updated:
                camera_state.wb_blue = float(val)
                return True, "WB_BLUE"

            self._set_phase(SEP_DONE)
            return False, "BLUE_OK->DONE"

        return False, "HOLD"

    def _set_phase(self, phase: int) -> None:
        if self.state.sep_phase != phase:
            logging.info(
                "Phase: %s -> %s",
                SEP_PHASE_LABELS.get(self.state.sep_phase, str(self.state.sep_phase)),
                SEP_PHASE_LABELS.get(phase, str(phase)),
            )
        self.state.sep_phase = phase


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live auto-calibration for TIS GigE cameras via tcamsrc + Gst appsink."
    )
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--mode", choices=["max", "fhd"], default="fhd")
    parser.add_argument("--marker", choices=["aruco", "apriltag"], default="aruco")
    parser.add_argument("--fisheye", action="store_true", help="Apply fisheye correction before calibration/visualization")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    state = CameraState(serial=args.serial)
    state.mode = MODE_MAX if args.mode == "max" else MODE_FHD
    init_camera_settings(state)

    controller = GigECameraController(state)
    engine = CalibrationEngine(args.marker)
    fisheye_corrector = FisheyeCorrector() if args.fisheye else None
    if fisheye_corrector is not None:
        logging.info(
            "fisheye enabled: f_scale=%.2f zoom=%.1f output_zoom=%.1f resolution_scale=%.1f",
            FISHEYE_DEFAULT_F_SCALE,
            FISHEYE_DEFAULT_ZOOM,
            FISHEYE_DEFAULT_OUTPUT_ZOOM,
            FISHEYE_DEFAULT_RESOLUTION_SCALE,
        )

    print("python exe :", sys.executable)
    print("cv2 file   :", cv2.__file__)
    print("cv2 ver    :", cv2.__version__)
    print(
        "aruco attrs:",
        [x for x in dir(cv2.aruco) if "Detector" in x or "detect" in x or "Parameters" in x],
    )

    logging.info("Python executable: %s", sys.executable)
    logging.info("cv2 version: %s", cv2.__version__)
    logging.info(
        "aruco attrs: %s",
        [x for x in dir(cv2.aruco) if "Detector" in x or "detect" in x or "Parameters" in x],
    )
    logging.info("Serial: %s", state.serial)
    logging.info("Mode: %s", args.mode)
    logging.info("Marker: %s", engine.marker_label)
    print_camera_state(state)

    controller.start()

    full_width, full_height = None, None
    skip_frames = 0

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, INITIAL_WINDOW_WIDTH, INITIAL_WINDOW_HEIGHT)

    try:
        while True:
            frame = controller.read_frame(timeout_ms=250)
            if frame is None:
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    generate_apply_summary(state)
                    break
                continue
            if fisheye_corrector is not None:
                frame = fisheye_corrector.apply(frame)

            if full_width is None or full_height is None:
                full_height, full_width = frame.shape[:2]
                logging.info("camera resolution: %d x %d", full_width, full_height)

            if skip_frames > 0:
                skip_frames -= 1
                cv2.imshow(WINDOW_NAME, frame)
                key = cv2.waitKey(1) & 0xFF
            else:
                mean_bri, target_bgr, current_targets = engine.analyze_frame(frame)

                txt, msg, col = "OFF", "", (200, 200, 200)

                if engine.state.calib_active:
                    engine.state.opt_steps += 1
                    updated, msg = engine.auto_calibrate_sep(target_bgr, state, mean_bri)

                    if updated:
                        state.exposure_auto = False
                        state.gain_auto = False
                        state.wb_auto = False
                        applied = controller.apply_manual_state()
                        if applied:
                            skip_frames = SKIP_FRAMES_AFTER_CMD
                            engine.clear_bgr_history()

                        txt, col = (
                            ("RECOVERY", (255, 100, 100))
                            if target_bgr is None
                            else ("ADJUST (SEP)", (0, 165, 255))
                        )
                    else:
                        txt, col = (
                            ("NO MARKER", (0, 0, 255))
                            if target_bgr is None
                            else ("MONITORING", (0, 255, 0))
                        )

                    if target_bgr is not None:
                        cv2.putText(
                            frame,
                            f"RGB: {int(target_bgr[2])} {int(target_bgr[1])} {int(target_bgr[0])}",
                            (20, 120),
                            engine.font,
                            0.7,
                            (255, 255, 255),
                            2,
                        )

                step_str = f" Steps:{engine.state.opt_steps}" if engine.state.calib_active else ""
                cv2.putText(frame, f"AUTO: {txt} {msg}", (20, 40), engine.font, 0.8, col, 2)
                cv2.putText(
                    frame,
                    f"Marker:{engine.marker_label} ID:{current_targets} Dir:{engine.current_direction_short()}",
                    (20, 80),
                    engine.font,
                    0.6,
                    (255, 255, 0),
                    2,
                )
                cv2.putText(
                    frame,
                    (
                        f"Exp(us):{state.exposure_us:.0f} Gain:{state.gain_db:.2f} "
                        f"WB:{state.wb_red:.2f}/{state.wb_green:.2f}/{state.wb_blue:.2f}"
                    ),
                    (20, frame.shape[0] - 48),
                    engine.font,
                    0.55,
                    (200, 200, 200),
                    1,
                )
                cv2.putText(
                    frame,
                    f"Bri:{int(mean_bri)} Phase:{SEP_PHASE_LABELS[engine.state.sep_phase]}{step_str}",
                    (20, frame.shape[0] - 20),
                    engine.font,
                    0.55,
                    (200, 200, 200),
                    1,
                )

                cv2.imshow(WINDOW_NAME, frame)
                key = cv2.waitKey(1) & 0xFF

            if key == ord("q"):
                generate_apply_summary(state)
                break

            if key == ord("c"):
                engine.toggle_calibration()
                skip_frames = 0
                continue

            if key == ord("r"):
                init_camera_settings(state)
                controller.apply_manual_state()
                engine.stop_calibration()
                print_camera_state(state)
                skip_frames = SKIP_FRAMES_AFTER_CMD
                continue

            if key == ord("d"):
                engine.cycle_direction()
                continue

            if key == ord("i"):
                engine.cycle_target_ids()
                continue

            if key == ord("1"):
                controller.switch_mode(MODE_MAX)
                engine.clear_bgr_history()
                skip_frames = SKIP_FRAMES_AFTER_CMD
                full_width, full_height = None, None
                print_camera_state(state)
                continue

            if key == ord("2"):
                controller.switch_mode(MODE_FHD)
                engine.clear_bgr_history()
                skip_frames = SKIP_FRAMES_AFTER_CMD
                full_width, full_height = None, None
                print_camera_state(state)
                continue

            if key == ord("p"):
                print_camera_state(state)
                continue

            if key == ord("o") and full_width is not None and full_height is not None:
                cv2.resizeWindow(WINDOW_NAME, full_width, full_height)

    finally:
        controller.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
