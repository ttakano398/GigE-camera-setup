import cv2
import numpy as np
import logging
import argparse
import time
from dataclasses import dataclass, field
from collections import deque

import sys

print("python exe :", sys.executable)
print("cv2 file   :", cv2.__file__)
print("cv2 ver    :", cv2.__version__)
print("aruco attrs:", [x for x in dir(cv2.aruco) if "Detector" in x or "detect" in x or "Parameters" in x])

# ==========================================
# ログ設定
# ==========================================
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

DEFAULT_SERIAL = "08520932"
WINDOW_NAME = "Calibration"

# デフォルトは ArUco
ARUCO_DICT_TYPE = cv2.aruco.DICT_4X4_50

# ==========================================
# 固定パラメータ
# ==========================================
TARGET_BRIGHTNESS = 119
THRESHOLD = 5
DEFAULT_CONTROLLER_GAIN = 0.5

MIN_EXPOSURE_LIMIT = 100          # us
MAX_EXPOSURE_LIMIT = 1_000_000    # us

MIN_WB_LIMIT = 0.1
MAX_WB_LIMIT = 8.0

SMOOTHING_WINDOW = 5
SKIP_FRAMES_AFTER_CMD = 3
REOPEN_SLEEP_SEC = 0.10

INITIAL_WINDOW_WIDTH = 1280
INITIAL_WINDOW_HEIGHT = 720

H_REF = 1000.0
MARKER_WIDTH = H_REF * 0.85
OFFSET = (H_REF - MARKER_WIDTH) / 2
VIRTUAL_MARKER_PTS = None
VIRTUAL_ROI_PTS = None

CURRENT_DIRECTION_IDX = 1
ID_MODE_IDX = 0
ID_TARGET_SETS = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [0]]

SEP_EXP_FINE, SEP_WB_RED, SEP_WB_BLUE, SEP_DONE = 0, 1, 2, 3


# ==========================================
# モード / 状態
# ==========================================
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
    mode: CameraMode = field(default_factory=lambda: MODE_MAX)

    exposure_auto: bool = False
    gain_auto: bool = False
    wb_auto: bool = False

    exposure_us: int = 15000
    gain_db: float = 0.0
    wb_red: float = 1.0
    wb_green: float = 1.0
    wb_blue: float = 1.0


# ==========================================
# ArUco 互換層
# ==========================================
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


def detect_markers(gray, aruco_dict):
    aruco = cv2.aruco
    params = create_aruco_parameters()

    # OpenCV 4.7+ 系
    if hasattr(aruco, "ArucoDetector"):
        if params is not None:
            detector = aruco.ArucoDetector(aruco_dict, params)
        else:
            detector = aruco.ArucoDetector(aruco_dict)
        return detector.detectMarkers(gray)

    # 旧API
    if hasattr(aruco, "detectMarkers"):
        if params is not None:
            return aruco.detectMarkers(gray, aruco_dict, parameters=params)
        return aruco.detectMarkers(gray, aruco_dict)

    attrs = [x for x in dir(aruco) if "Detector" in x or "detect" in x or "Parameters" in x]
    raise RuntimeError(
        f"No usable ArUco API in this runtime. cv2={cv2.__version__}, attrs={attrs}"
    )


# ==========================================
# ユーティリティ
# ==========================================
def clamp(val, min_v, max_v):
    return max(min_v, min(val, max_v))


def build_tcam_properties(state: CameraState) -> str:
    props = [
        f"ExposureAuto={'On' if state.exposure_auto else 'Off'}",
        f"GainAuto={'On' if state.gain_auto else 'Off'}",
        f"BalanceWhiteAuto={'On' if state.wb_auto else 'Off'}",
    ]

    if not state.exposure_auto:
        props.append(f"ExposureTime={int(state.exposure_us)}")

    if not state.gain_auto:
        props.append(f"Gain={float(state.gain_db):.2f}")

    if not state.wb_auto:
        props.append(f"BalanceWhiteRed={float(state.wb_red):.3f}")
        props.append(f"BalanceWhiteGreen={float(state.wb_green):.3f}")
        props.append(f"BalanceWhiteBlue={float(state.wb_blue):.3f}")

    return "tcam," + ",".join(props)


def make_pipeline(state: CameraState) -> str:
    props = build_tcam_properties(state)
    mode = state.mode
    return (
        f'tcammainsrc serial="{state.serial}" tcam-properties="{props}" ! '
        f"video/x-bayer,format=grbg,width={mode.width},height={mode.height},framerate={mode.fps}/1 ! "
        "bayer2rgb ! "
        "videoconvert ! "
        "video/x-raw,format=BGR ! "
        "appsink sync=false drop=true max-buffers=1"
    )


def open_capture(state: CameraState) -> cv2.VideoCapture:
    pipeline = make_pipeline(state)
    logging.info("Opening pipeline:")
    logging.info(pipeline)
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        raise RuntimeError("failed to open camera via GStreamer/tcammainsrc")
    return cap


def reopen_capture(cap: cv2.VideoCapture, state: CameraState) -> cv2.VideoCapture:
    cap.release()
    time.sleep(REOPEN_SLEEP_SEC)
    return open_capture(state)


def init_camera_settings(state: CameraState):
    logging.info("初期設定を適用中...")
    state.exposure_auto = False
    state.gain_auto = False
    state.wb_auto = False
    state.exposure_us = 15000
    state.gain_db = 0.0
    state.wb_red = 1.0
    state.wb_green = 1.0
    state.wb_blue = 1.0


def print_camera_state(state: CameraState):
    logging.info(
        "CameraState | mode=%dx%d@%dfps | AE=%s AGC=%s AWB=%s | Exp(us)=%d Gain(dB)=%.2f | WB(R,G,B)=(%.3f, %.3f, %.3f)",
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


def generate_apply_summary(state: CameraState):
    print(f"\n{'='*60}")
    print("[現在設定]")
    print(f"serial            : {state.serial}")
    print(f"resolution        : {state.mode.width}x{state.mode.height}")
    print(f"fps               : {state.mode.fps}")
    print(f"ExposureAuto      : {'On' if state.exposure_auto else 'Off'}")
    print(f"GainAuto          : {'On' if state.gain_auto else 'Off'}")
    print(f"BalanceWhiteAuto  : {'On' if state.wb_auto else 'Off'}")
    print(f"ExposureTime(us)  : {state.exposure_us}")
    print(f"Gain(dB)          : {state.gain_db:.2f}")
    print(f"BalanceWhiteRed   : {state.wb_red:.3f}")
    print(f"BalanceWhiteGreen : {state.wb_green:.3f}")
    print(f"BalanceWhiteBlue  : {state.wb_blue:.3f}")
    print("\n[tcam-properties]")
    print(build_tcam_properties(state))
    print(f"{'='*60}\n")


def update_roi_geometry():
    global MARKER_WIDTH, OFFSET, VIRTUAL_MARKER_PTS, VIRTUAL_ROI_PTS

    MARKER_WIDTH = H_REF * 0.85
    OFFSET = (H_REF - MARKER_WIDTH) / 2

    VIRTUAL_MARKER_PTS = np.array(
        [
            [OFFSET, OFFSET],
            [OFFSET + MARKER_WIDTH, OFFSET],
            [OFFSET + MARKER_WIDTH, OFFSET + MARKER_WIDTH],
            [OFFSET, OFFSET + MARKER_WIDTH],
        ],
        dtype=np.float32,
    )

    dir_map = {
        0: (1.0, 3.0, 0.0, 1.0),   # Right
        1: (0.0, 1.0, 1.0, 3.0),   # Down
        2: (-2.0, 0.0, 0.0, 1.0),  # Left
        3: (0.0, 1.0, -2.0, 0.0),  # Up
    }
    xm_s, xm_e, ym_s, ym_e = dir_map[CURRENT_DIRECTION_IDX]
    margin = H_REF * 0.25

    x_s = (H_REF * xm_s) + margin
    x_e = (H_REF * xm_e) - margin
    y_s = (H_REF * ym_s) + margin
    y_e = (H_REF * ym_e) - margin

    pts = np.array([[x_s, y_s], [x_e, y_s], [x_e, y_e], [x_s, y_e]], dtype=np.float32)
    VIRTUAL_ROI_PTS = pts.reshape(-1, 1, 2)

    logging.info(
        f"Direction Updated: {['Right', 'Down', 'Left', 'Up'][CURRENT_DIRECTION_IDX]}"
    )


def adjust_param(diff, gain, current_param_val, min_limit, max_limit):
    abs_diff = abs(diff)

    if abs_diff <= THRESHOLD:
        return False, current_param_val

    step = max(1e-6, abs_diff * gain)
    new_val = current_param_val - step if diff > 0 else current_param_val + step
    new_val = clamp(new_val, min_limit, max_limit)

    if abs(new_val - current_param_val) > 1e-9:
        return True, new_val

    return False, current_param_val


# ==========================================
# 最適化ロジック (SEP)
# ==========================================
def auto_calibrate_sep(bgr, params, sep_state, global_bri=0):
    exp = params["exposure"]
    phase = sep_state["phase"]

    if "gain" not in sep_state:
        sep_state.update({"gain": DEFAULT_CONTROLLER_GAIN, "prev_sign": 0})

    if bgr is None:
        if global_bri > 200 and exp > MIN_EXPOSURE_LIMIT:
            new_exp = max(MIN_EXPOSURE_LIMIT, int(exp - 5000))
            params["exposure"] = new_exp
            return True, params, "RECOVERY (BRIGHT)", sep_state
        elif global_bri < 30 and exp < MAX_EXPOSURE_LIMIT:
            new_exp = min(MAX_EXPOSURE_LIMIT, int(exp + 5000))
            params["exposure"] = new_exp
            return True, params, "RECOVERY (DARK)", sep_state
        return False, params, "NO MARKER", sep_state

    b, g, r = bgr

    exp_ok = abs(g - TARGET_BRIGHTNESS) <= THRESHOLD
    red_ok = abs(r - g) <= THRESHOLD
    blue_ok = abs(b - g) <= THRESHOLD

    if phase == SEP_DONE:
        if not exp_ok:
            sep_state["phase"] = SEP_EXP_FINE
        elif not red_ok:
            sep_state["phase"] = SEP_WB_RED
        elif not blue_ok:
            sep_state["phase"] = SEP_WB_BLUE
        else:
            return False, params, "HOLD", sep_state
        phase = sep_state["phase"]

    if phase == SEP_EXP_FINE:
        diff = g - TARGET_BRIGHTNESS

        if abs(diff) > THRESHOLD:
            current_sign = 1 if diff > 0 else -1
            if sep_state["prev_sign"] != 0 and sep_state["prev_sign"] != current_sign:
                sep_state["gain"] = max(0.01, sep_state["gain"] * 0.5)
                logging.info(f"Damping: Gain -> {sep_state['gain']:.3f}")
            sep_state["prev_sign"] = current_sign

            step = int(abs(diff) * sep_state["gain"] * 100.0)
            max_rel_step = max(100, int(exp * 0.3))
            step = min(step, max_rel_step)
            step = max(100, step)

            new_exp = exp - step if diff > 0 else exp + step
            new_exp = clamp(new_exp, MIN_EXPOSURE_LIMIT, MAX_EXPOSURE_LIMIT)

            if new_exp != exp:
                params["exposure"] = int(new_exp)
                return True, params, "EXP_FINE", sep_state

        sep_state["phase"] = SEP_WB_RED
        return False, params, "EXP_OK->RED", sep_state

    if phase == SEP_WB_RED:
        updated, val = adjust_param(
            r - g, 0.002, params["red"], MIN_WB_LIMIT, MAX_WB_LIMIT
        )
        if updated:
            params["red"] = float(val)
            return True, params, "WB_RED", sep_state

        sep_state["phase"] = SEP_WB_BLUE
        return False, params, "RED_OK->BLUE", sep_state

    if phase == SEP_WB_BLUE:
        updated, val = adjust_param(
            b - g, 0.002, params["blue"], MIN_WB_LIMIT, MAX_WB_LIMIT
        )
        if updated:
            params["blue"] = float(val)
            return True, params, "WB_BLUE", sep_state

        sep_state["phase"] = SEP_DONE
        return False, params, "BLUE_OK->DONE", sep_state

    return False, params, "HOLD", sep_state


# ==========================================
# Main
# ==========================================
def main(args):
    global CURRENT_DIRECTION_IDX, ID_MODE_IDX, ARUCO_DICT_TYPE

    if args.marker == "apriltag":
        ARUCO_DICT_TYPE = cv2.aruco.DICT_APRILTAG_36h11
    else:
        ARUCO_DICT_TYPE = cv2.aruco.DICT_4X4_50

    state = CameraState(serial=args.serial)
    state.mode = MODE_MAX if args.mode == "max" else MODE_FHD
    init_camera_settings(state)

    cap = open_capture(state)
    full_width, full_height = None, None

    try:
        update_roi_geometry()

        aruco_dict = cv2.aruco.getPredefinedDictionary(ARUCO_DICT_TYPE)
        font = cv2.FONT_HERSHEY_SIMPLEX

        params = {
            "exposure": state.exposure_us,
            "red": state.wb_red,
            "blue": state.wb_blue,
        }

        calib_active = False
        bgr_hist = deque(maxlen=SMOOTHING_WINDOW)
        sep_state = {"phase": SEP_EXP_FINE, "gain": DEFAULT_CONTROLLER_GAIN, "prev_sign": 0}
        opt_steps = 0
        skip_frames = 0

        marker_lbl = "APRILTAG" if args.marker == "apriltag" else "ARUCO"
        logging.info(f"cv2 version: {cv2.__version__}")
        logging.info(f"aruco attrs: {[x for x in dir(cv2.aruco) if 'Detector' in x or 'detect' in x or 'Parameters' in x]}")
        logging.info(f"Start: {marker_lbl} | Algo: SEP | Mode:{args.mode}")
        print_camera_state(state)

        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WINDOW_NAME, INITIAL_WINDOW_WIDTH, INITIAL_WINDOW_HEIGHT)

        while True:
            ret, frame = cap.read()
            if not ret:
                continue

            if full_width is None or full_height is None:
                full_height, full_width = frame.shape[:2]
                logging.info(f"camera resolution: {full_width} x {full_height}")

            if skip_frames > 0:
                skip_frames -= 1
                cv2.imshow(WINDOW_NAME, frame)
                cv2.waitKey(1)
                continue

            h, w = frame.shape[:2]
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            mean_bri = float(np.mean(gray))

            corners, ids, _ = detect_markers(gray, aruco_dict)

            target_bgr = None
            current_targets = ID_TARGET_SETS[ID_MODE_IDX]
            roi_bgrs = []

            if ids is not None:
                for i, mid in enumerate(ids.flatten()):
                    if mid in current_targets:
                        try:
                            M = cv2.getPerspectiveTransform(
                                VIRTUAL_MARKER_PTS, corners[i][0].astype(np.float32)
                            )
                            dst_roi = (
                                cv2.perspectiveTransform(VIRTUAL_ROI_PTS, M)
                                .reshape(-1, 2)
                                .astype(np.int32)
                            )

                            mask = np.zeros((h, w), dtype=np.uint8)
                            cv2.fillPoly(mask, [dst_roi], 255)
                            roi_bgrs.append(cv2.mean(frame, mask=mask)[:3])
                            cv2.polylines(
                                frame, [dst_roi], True, (0, 255, 0), 2, cv2.LINE_AA
                            )
                        except Exception:
                            pass

                cv2.aruco.drawDetectedMarkers(frame, corners, ids)

            if roi_bgrs:
                bgr_hist.append(np.mean(roi_bgrs, axis=0))
                target_bgr = np.mean(bgr_hist, axis=0)
            elif bgr_hist:
                bgr_hist.popleft()

            txt, msg, col = "OFF", "", (200, 200, 200)

            if calib_active:
                opt_steps += 1

                upd, params, msg, sep_state = auto_calibrate_sep(
                    target_bgr, params, sep_state, mean_bri
                )

                if upd:
                    state.exposure_auto = False
                    state.gain_auto = False
                    state.wb_auto = False
                    state.exposure_us = int(params["exposure"])
                    state.wb_red = float(params["red"])
                    state.wb_blue = float(params["blue"])

                    try:
                        cap = reopen_capture(cap, state)
                        full_width, full_height = None, None
                        skip_frames = SKIP_FRAMES_AFTER_CMD
                        bgr_hist.clear()
                    except Exception as e:
                        logging.error(f"Failed to reopen capture: {e}")

                    if target_bgr is None:
                        txt, col = "RECOVERY", (255, 100, 100)
                    else:
                        txt, col = "ADJUST (SEP)", (0, 165, 255)
                else:
                    if target_bgr is None:
                        txt, col = "NO MARKER", (0, 0, 255)
                    else:
                        txt, col = "MONITORING", (0, 255, 0)

                if target_bgr is not None:
                    cv2.putText(
                        frame,
                        f"RGB: {int(target_bgr[2])} {int(target_bgr[1])} {int(target_bgr[0])}",
                        (20, 120),
                        font,
                        0.7,
                        (255, 255, 255),
                        2,
                    )

            step_str = f" Steps:{opt_steps}" if calib_active else ""
            cv2.putText(frame, f"AUTO: {txt} {msg}", (20, 40), font, 0.8, col, 2)
            cv2.putText(
                frame,
                f"Marker:{marker_lbl} ID:{current_targets} Dir:{['R','D','L','U'][CURRENT_DIRECTION_IDX]}",
                (20, 80),
                font,
                0.6,
                (255, 255, 0),
                2,
            )
            cv2.putText(
                frame,
                f"Exp(us):{state.exposure_us} Bri:{int(mean_bri)} SEP{step_str}",
                (20, h - 20),
                font,
                0.6,
                (200, 200, 200),
                1,
            )

            cv2.imshow(WINDOW_NAME, frame)

            k = cv2.waitKey(1) & 0xFF
            if k == ord("q"):
                generate_apply_summary(state)
                break

            elif k == ord("c"):
                calib_active = not calib_active
                logging.info(f"Calib: {calib_active}")
                if calib_active:
                    bgr_hist.clear()
                    opt_steps = 0
                    skip_frames = 0
                    sep_state = {
                        "phase": SEP_EXP_FINE,
                        "gain": DEFAULT_CONTROLLER_GAIN,
                        "prev_sign": 0,
                    }

            elif k == ord("r"):
                init_camera_settings(state)
                params = {
                    "exposure": state.exposure_us,
                    "red": state.wb_red,
                    "blue": state.wb_blue,
                }
                calib_active = False
                bgr_hist.clear()
                opt_steps = 0
                sep_state = {
                    "phase": SEP_EXP_FINE,
                    "gain": DEFAULT_CONTROLLER_GAIN,
                    "prev_sign": 0,
                }
                cap = reopen_capture(cap, state)
                full_width, full_height = None, None
                print_camera_state(state)

            elif k == ord("d"):
                CURRENT_DIRECTION_IDX = (CURRENT_DIRECTION_IDX + 1) % 4
                update_roi_geometry()

            elif k == ord("i"):
                ID_MODE_IDX = (ID_MODE_IDX + 1) % len(ID_TARGET_SETS)
                logging.info(f"Target IDs: {ID_TARGET_SETS[ID_MODE_IDX]}")
                bgr_hist.clear()

            elif k == ord("1"):
                state.mode = MODE_MAX
                cap = reopen_capture(cap, state)
                full_width, full_height = None, None
                print_camera_state(state)

            elif k == ord("2"):
                state.mode = MODE_FHD
                cap = reopen_capture(cap, state)
                full_width, full_height = None, None
                print_camera_state(state)

            elif k == ord("p"):
                print_camera_state(state)

            elif k == ord("o"):
                if full_width is not None and full_height is not None:
                    cv2.resizeWindow(WINDOW_NAME, full_width, full_height)

    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--mode", choices=["max", "fhd"], default="fhd")
    parser.add_argument("--marker", choices=["aruco", "apriltag"], default="aruco")
    args = parser.parse_args()

    update_roi_geometry()
    main(args)