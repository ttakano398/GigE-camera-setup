#!/usr/bin/env python3
"""
Calibrate a camera from ChArUco board images captured by ../viewer.py.

Workflow:
1. Edit the board settings in the "User-configurable board parameters" block.
2. Run ../viewer.py without --rectify/--fisheye and press "c" to save board images.
3. Run this script. It writes camera_calib.yaml in the format used by ../viewer.py.
4. Start ../viewer.py with --rectify.

Visualization:
- --save-vis
    Save detected ArUco markers and ChArUco corners.

- --save-reproj-vis
    Save reprojection-error visualizations after final calibration:
      * Per-image observed/projected corner overlay
      * Reprojection error vectors
      * Large-error corner highlighting
      * Per-view error summary CSV
      * Aggregate image-space reprojection error heatmap
"""

import argparse
import csv
import glob
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_IMAGE_DIR = SCRIPT_DIR / "capture"

DEFAULT_OUT_YAML = SCRIPT_DIR / "camera_calib-ch-ratthin.yaml"
DEFAULT_OUT_NPZ = SCRIPT_DIR / "camera_calib-ch-ratthin.npz"

DEFAULT_VIS_DIR = SCRIPT_DIR / "charuco_detected-ratthin"
DEFAULT_REPROJ_VIS_DIR = SCRIPT_DIR / "reprojection_errors-ratthin"


# ============================================================
# User-configurable board parameters
# ============================================================

# Number of chessboard squares, not the number of inner corners.
SQUARES_X = 7
SQUARES_Y = 5

# Physical board dimensions.
# Use the same unit for both values.
SQUARE_LENGTH = 0.0287
MARKER_LENGTH = 0.021

# Dictionary used when the physical board was generated/printed.
ARUCO_DICT_NAME = "DICT_4X4_50"

# Minimum ChArUco chessboard corners required in each captured image.
MIN_CHARUCO_CORNERS = 8

# OpenCV calibration flags.
CALIBRATION_FLAGS = (
    cv2.CALIB_RATIONAL_MODEL
    | cv2.CALIB_THIN_PRISM_MODEL
)

# Examples:
#
# Standard model:
# CALIBRATION_FLAGS = 0
#
# Rational model:
# CALIBRATION_FLAGS = cv2.CALIB_RATIONAL_MODEL
#
# Rational + Thin Prism:
# CALIBRATION_FLAGS = (
#     cv2.CALIB_RATIONAL_MODEL
#     | cv2.CALIB_THIN_PRISM_MODEL
# )

# ============================================================


IMAGE_EXTS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tiff",
    ".tif",
)


@dataclass
class Detection:
    path: str
    charuco_corners: np.ndarray
    charuco_ids: np.ndarray
    marker_corners: Optional[list]
    marker_ids: Optional[np.ndarray]


@dataclass
class CalibResult:
    rms: float
    K: np.ndarray
    dist: np.ndarray

    rvecs: list[np.ndarray]
    tvecs: list[np.ndarray]

    image_size: tuple[int, int]

    per_view_errors: list[float]

    used_paths: list[str]
    dropped_paths: list[str]


# ============================================================
# ArUco / ChArUco compatibility helpers
# ============================================================


def require_aruco_module():
    if not hasattr(cv2, "aruco"):
        raise RuntimeError(
            "cv2.aruco is not available. "
            "Install/build OpenCV with opencv-contrib."
        )

    return cv2.aruco


def create_dictionary(dict_name: str):
    aruco = require_aruco_module()

    if not hasattr(aruco, dict_name):
        known = sorted(
            name
            for name in dir(aruco)
            if name.startswith("DICT_")
        )

        raise RuntimeError(
            f"Unknown ArUco dictionary: {dict_name}. "
            f"Known examples: {known[:12]}"
        )

    dict_id = getattr(aruco, dict_name)

    if hasattr(aruco, "getPredefinedDictionary"):
        return aruco.getPredefinedDictionary(dict_id)

    if hasattr(aruco, "Dictionary_get"):
        return aruco.Dictionary_get(dict_id)

    raise RuntimeError(
        "No usable ArUco dictionary factory "
        "in this OpenCV runtime."
    )


def create_charuco_board(
    squares_x: int,
    squares_y: int,
    square_length: float,
    marker_length: float,
    dictionary,
):
    aruco = require_aruco_module()

    if squares_x < 2 or squares_y < 2:
        raise ValueError(
            "SQUARES_X and SQUARES_Y must both be >= 2."
        )

    if square_length <= 0.0:
        raise ValueError(
            "SQUARE_LENGTH must be > 0."
        )

    if marker_length <= 0.0 or marker_length >= square_length:
        raise ValueError(
            "MARKER_LENGTH must be > 0 "
            "and smaller than SQUARE_LENGTH."
        )

    if hasattr(aruco, "CharucoBoard_create"):
        return aruco.CharucoBoard_create(
            squares_x,
            squares_y,
            square_length,
            marker_length,
            dictionary,
        )

    if hasattr(aruco, "CharucoBoard"):
        return aruco.CharucoBoard(
            (squares_x, squares_y),
            square_length,
            marker_length,
            dictionary,
        )

    raise RuntimeError(
        "No usable ChArUco board factory "
        "in this OpenCV runtime."
    )


def create_detector_parameters():
    aruco = require_aruco_module()

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


def detect_markers(
    gray: np.ndarray,
    dictionary,
    parameters,
):
    aruco = require_aruco_module()

    if hasattr(aruco, "ArucoDetector"):
        detector = (
            aruco.ArucoDetector(
                dictionary,
                parameters,
            )
            if parameters is not None
            else aruco.ArucoDetector(
                dictionary
            )
        )

        return detector.detectMarkers(gray)

    if hasattr(aruco, "detectMarkers"):
        if parameters is not None:
            return aruco.detectMarkers(
                gray,
                dictionary,
                parameters=parameters,
            )

        return aruco.detectMarkers(
            gray,
            dictionary,
        )

    attrs = [
        name
        for name in dir(aruco)
        if "Detector" in name
        or "detect" in name
    ]

    raise RuntimeError(
        "No usable ArUco detector API. "
        f"cv2={cv2.__version__}, attrs={attrs}"
    )


def detect_charuco(
    gray: np.ndarray,
    board,
    dictionary,
) -> tuple[
    Optional[np.ndarray],
    Optional[np.ndarray],
    Optional[list],
    Optional[np.ndarray],
]:
    aruco = require_aruco_module()

    # New OpenCV API
    if hasattr(aruco, "CharucoDetector"):
        try:
            detector = aruco.CharucoDetector(board)
            result = detector.detectBoard(gray)

            if len(result) == 4:
                (
                    charuco_corners,
                    charuco_ids,
                    marker_corners,
                    marker_ids,
                ) = result

                return (
                    charuco_corners,
                    charuco_ids,
                    marker_corners,
                    marker_ids,
                )

        except Exception:
            pass

    # Old OpenCV API
    parameters = create_detector_parameters()

    marker_corners, marker_ids, _ = detect_markers(
        gray,
        dictionary,
        parameters,
    )

    if (
        marker_ids is None
        or len(marker_ids) == 0
    ):
        return (
            None,
            None,
            marker_corners,
            marker_ids,
        )

    if not hasattr(
        aruco,
        "interpolateCornersCharuco",
    ):
        raise RuntimeError(
            "cv2.aruco.interpolateCornersCharuco "
            "is not available."
        )

    (
        _,
        charuco_corners,
        charuco_ids,
    ) = aruco.interpolateCornersCharuco(
        marker_corners,
        marker_ids,
        gray,
        board,
    )

    return (
        charuco_corners,
        charuco_ids,
        marker_corners,
        marker_ids,
    )


# ============================================================
# Image collection
# ============================================================


def collect_images(
    image_dir: Path,
    recursive: bool,
) -> list[str]:

    image_dir = (
        Path(image_dir)
        .expanduser()
        .resolve()
    )

    if not image_dir.exists():
        raise RuntimeError(
            f"Image directory does not exist: "
            f"{image_dir}"
        )

    paths: list[str] = []

    for ext in IMAGE_EXTS:

        pattern = (
            f"**/*{ext}"
            if recursive
            else f"*{ext}"
        )

        paths.extend(
            glob.glob(
                str(image_dir / pattern),
                recursive=recursive,
            )
        )

        pattern_upper = (
            f"**/*{ext.upper()}"
            if recursive
            else f"*{ext.upper()}"
        )

        paths.extend(
            glob.glob(
                str(image_dir / pattern_upper),
                recursive=recursive,
            )
        )

    return sorted(set(paths))


def read_gray(
    path: str,
) -> tuple[np.ndarray, np.ndarray]:

    image = cv2.imread(
        path,
        cv2.IMREAD_COLOR,
    )

    if image is None:
        raise RuntimeError(
            f"Cannot read image: {path}"
        )

    gray = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2GRAY,
    )

    return image, gray


# ============================================================
# Detection visualization
# ============================================================


def draw_detection(
    image: np.ndarray,
    detection: Detection,
) -> np.ndarray:

    aruco = require_aruco_module()

    vis = image.copy()

    if (
        detection.marker_ids is not None
        and detection.marker_corners is not None
        and hasattr(
            aruco,
            "drawDetectedMarkers",
        )
    ):
        aruco.drawDetectedMarkers(
            vis,
            detection.marker_corners,
            detection.marker_ids,
        )

    if (
        detection.charuco_ids is not None
        and detection.charuco_corners is not None
        and hasattr(
            aruco,
            "drawDetectedCornersCharuco",
        )
    ):
        aruco.drawDetectedCornersCharuco(
            vis,
            detection.charuco_corners,
            detection.charuco_ids,
            (0, 255, 0),
        )

    cv2.putText(
        vis,
        (
            "charuco corners: "
            f"{len(detection.charuco_ids)}"
        ),
        (16, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )

    return vis


# ============================================================
# Board point helpers
# ============================================================


def board_object_corners(
    board,
) -> np.ndarray:

    if hasattr(
        board,
        "getChessboardCorners",
    ):
        return np.asarray(
            board.getChessboardCorners(),
            dtype=np.float32,
        )

    if hasattr(
        board,
        "chessboardCorners",
    ):
        return np.asarray(
            board.chessboardCorners,
            dtype=np.float32,
        )

    raise RuntimeError(
        "Cannot read ChArUco board "
        "chessboard object corners."
    )


def object_points_for_ids(
    board,
    charuco_ids: np.ndarray,
) -> np.ndarray:

    corners_3d = board_object_corners(
        board
    )

    ids = np.asarray(
        charuco_ids,
        dtype=np.int32,
    ).reshape(-1)

    return (
        corners_3d[ids]
        .reshape(-1, 1, 3)
        .astype(np.float32)
    )


def image_points_from_detection(
    detection: Detection,
) -> np.ndarray:

    return np.asarray(
        detection.charuco_corners,
        dtype=np.float32,
    ).reshape(-1, 1, 2)


# ============================================================
# Calibration
# ============================================================


def calibrate_detections(
    detections: list[Detection],
    board,
    image_size: tuple[int, int],
    flags: int,
) -> tuple[
    float,
    np.ndarray,
    np.ndarray,
    list[np.ndarray],
    list[np.ndarray],
]:

    aruco = require_aruco_module()

    charuco_corners = [
        image_points_from_detection(det)
        for det in detections
    ]

    charuco_ids = [
        np.asarray(
            det.charuco_ids,
            dtype=np.int32,
        ).reshape(-1, 1)
        for det in detections
    ]

    if hasattr(
        aruco,
        "calibrateCameraCharucoExtended",
    ):
        try:

            result = (
                aruco.calibrateCameraCharucoExtended(
                    charuco_corners,
                    charuco_ids,
                    board,
                    image_size,
                    None,
                    None,
                    flags=flags,
                )
            )

            (
                rms,
                K,
                dist,
                rvecs,
                tvecs,
            ) = result[:5]

            return (
                float(rms),
                K,
                dist,
                list(rvecs),
                list(tvecs),
            )

        except Exception as exc:
            print(
                "[warn] "
                "calibrateCameraCharucoExtended "
                "failed; fallback will be tried: "
                f"{exc}"
            )

    if hasattr(
        aruco,
        "calibrateCameraCharuco",
    ):
        try:

            (
                rms,
                K,
                dist,
                rvecs,
                tvecs,
            ) = aruco.calibrateCameraCharuco(
                charuco_corners,
                charuco_ids,
                board,
                image_size,
                None,
                None,
                flags=flags,
            )

            return (
                float(rms),
                K,
                dist,
                list(rvecs),
                list(tvecs),
            )

        except Exception as exc:

            print(
                "[warn] "
                "calibrateCameraCharuco failed; "
                "falling back to "
                "cv2.calibrateCamera: "
                f"{exc}"
            )

    objpoints = [
        object_points_for_ids(
            board,
            det.charuco_ids,
        )
        for det in detections
    ]

    imgpoints = [
        image_points_from_detection(det)
        for det in detections
    ]

    (
        rms,
        K,
        dist,
        rvecs,
        tvecs,
    ) = cv2.calibrateCamera(
        objpoints,
        imgpoints,
        image_size,
        None,
        None,
        flags=flags,
    )

    return (
        float(rms),
        K,
        dist,
        list(rvecs),
        list(tvecs),
    )


# ============================================================
# Reprojection error computation
# ============================================================


def compute_view_reprojection(
    detection: Detection,
    board,
    rvec: np.ndarray,
    tvec: np.ndarray,
    K: np.ndarray,
    dist: np.ndarray,
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    float,
]:

    objp = object_points_for_ids(
        board,
        detection.charuco_ids,
    )

    observed = (
        image_points_from_detection(
            detection
        )
        .reshape(-1, 2)
    )

    projected, _ = cv2.projectPoints(
        objp,
        rvec,
        tvec,
        K,
        dist,
    )

    projected = projected.reshape(-1, 2)

    # Vector from observed point to projected point.
    residual_vectors = (
        projected
        - observed
    )

    point_errors = np.linalg.norm(
        residual_vectors,
        axis=1,
    )

    rmse = float(
        np.sqrt(
            np.mean(
                point_errors ** 2
            )
        )
    )

    return (
        observed,
        projected,
        residual_vectors,
        point_errors,
        rmse,
    )


def compute_reprojection_errors(
    detections: list[Detection],
    board,
    rvecs: list[np.ndarray],
    tvecs: list[np.ndarray],
    K: np.ndarray,
    dist: np.ndarray,
) -> list[float]:

    errors: list[float] = []

    for (
        det,
        rvec,
        tvec,
    ) in zip(
        detections,
        rvecs,
        tvecs,
    ):

        (
            _,
            _,
            _,
            _,
            rmse,
        ) = compute_view_reprojection(
            det,
            board,
            rvec,
            tvec,
            K,
            dist,
        )

        errors.append(rmse)

    return errors


# ============================================================
# Reprojection visualization
# ============================================================


def get_error_colormap():
    """
    TURBO is visually easier to read than JET.
    Fall back to JET for older OpenCV versions.
    """

    if hasattr(
        cv2,
        "COLORMAP_TURBO",
    ):
        return cv2.COLORMAP_TURBO

    return cv2.COLORMAP_JET


def error_values_to_colors(
    errors: np.ndarray,
    vmax: float,
) -> np.ndarray:

    errors = np.asarray(
        errors,
        dtype=np.float32,
    ).reshape(-1)

    vmax = max(
        float(vmax),
        1e-6,
    )

    normalized = np.clip(
        errors / vmax,
        0.0,
        1.0,
    )

    values = np.round(
        normalized * 255.0
    ).astype(np.uint8)

    colors = cv2.applyColorMap(
        values.reshape(-1, 1),
        get_error_colormap(),
    )

    return colors.reshape(-1, 3)


def draw_reprojection_visualization(
    image: np.ndarray,
    detection: Detection,
    observed: np.ndarray,
    projected: np.ndarray,
    residual_vectors: np.ndarray,
    point_errors: np.ndarray,
    rmse: float,
    highlight_threshold: float,
    vector_scale: float,
) -> np.ndarray:

    vis = image.copy()

    charuco_ids = np.asarray(
        detection.charuco_ids,
        dtype=np.int32,
    ).reshape(-1)

    max_error = float(
        np.max(point_errors)
    )

    mean_error = float(
        np.mean(point_errors)
    )

    median_error = float(
        np.median(point_errors)
    )

    large_count = int(
        np.count_nonzero(
            point_errors
            >= highlight_threshold
        )
    )

    # Draw smaller errors first.
    # Large errors are rendered last and remain visible.
    draw_order = np.argsort(
        point_errors
    )

    for idx in draw_order:

        obs = observed[idx]
        proj = projected[idx]
        residual = residual_vectors[idx]
        error = float(point_errors[idx])

        obs_pt = tuple(
            np.round(obs)
            .astype(int)
        )

        proj_pt = tuple(
            np.round(proj)
            .astype(int)
        )

        is_large = (
            error
            >= highlight_threshold
        )

        # ----------------------------------------------------
        # Actual detected point
        # Green filled circle
        # ----------------------------------------------------

        cv2.circle(
            vis,
            obs_pt,
            5 if not is_large else 7,
            (0, 255, 0),
            -1,
            cv2.LINE_AA,
        )

        # ----------------------------------------------------
        # Actual reprojected point
        # Red outlined circle
        # ----------------------------------------------------

        cv2.circle(
            vis,
            proj_pt,
            5 if not is_large else 7,
            (0, 0, 255),
            2,
            cv2.LINE_AA,
        )

        # ----------------------------------------------------
        # Actual error segment
        # ----------------------------------------------------

        actual_line_color = (
            (0, 0, 255)
            if is_large
            else (0, 255, 255)
        )

        cv2.line(
            vis,
            obs_pt,
            proj_pt,
            actual_line_color,
            3 if is_large else 1,
            cv2.LINE_AA,
        )

        # ----------------------------------------------------
        # Magnified error vector
        #
        # Useful because several-pixel errors are difficult
        # to see on a 2592x1944 image.
        # ----------------------------------------------------

        if vector_scale > 1.0:

            scaled_endpoint = (
                obs
                + residual * vector_scale
            )

            scaled_pt = tuple(
                np.round(
                    scaled_endpoint
                ).astype(int)
            )

            vector_color = (
                (255, 0, 255)
                if is_large
                else (255, 255, 0)
            )

            cv2.arrowedLine(
                vis,
                obs_pt,
                scaled_pt,
                vector_color,
                3 if is_large else 1,
                cv2.LINE_AA,
                tipLength=0.20,
            )

        # ----------------------------------------------------
        # Error label for large-error corners
        # ----------------------------------------------------

        if is_large:

            corner_id = int(
                charuco_ids[idx]
            )

            text = (
                f"id={corner_id} "
                f"{error:.1f}px"
            )

            text_pos = (
                obs_pt[0] + 10,
                obs_pt[1] - 10,
            )

            # Black border for readability
            cv2.putText(
                vis,
                text,
                text_pos,
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (0, 0, 0),
                4,
                cv2.LINE_AA,
            )

            cv2.putText(
                vis,
                text,
                text_pos,
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

    # ========================================================
    # Summary box
    # ========================================================

    text_lines = [
        f"RMSE   : {rmse:.3f} px",
        f"Mean   : {mean_error:.3f} px",
        f"Median : {median_error:.3f} px",
        f"Max    : {max_error:.3f} px",
        (
            f">= {highlight_threshold:.1f}px : "
            f"{large_count}/{len(point_errors)}"
        ),
        (
            "Green=observed  "
            "Red=projected"
        ),
        (
            "Cyan/Magenta arrows: "
            f"error vector x{vector_scale:.1f}"
        ),
    ]

    box_x = 20
    box_y = 20
    line_height = 34

    box_width = 720
    box_height = (
        len(text_lines)
        * line_height
        + 25
    )

    overlay = vis.copy()

    cv2.rectangle(
        overlay,
        (box_x, box_y),
        (
            box_x + box_width,
            box_y + box_height,
        ),
        (0, 0, 0),
        -1,
    )

    cv2.addWeighted(
        overlay,
        0.65,
        vis,
        0.35,
        0,
        vis,
    )

    for i, text in enumerate(
        text_lines
    ):

        y = (
            box_y
            + 35
            + i * line_height
        )

        cv2.putText(
            vis,
            text,
            (box_x + 15, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

    return vis


def append_error_colorbar(
    image: np.ndarray,
    vmax: float,
) -> np.ndarray:

    h, w = image.shape[:2]

    panel_width = 190

    output = np.full(
        (
            h,
            w + panel_width,
            3,
        ),
        30,
        dtype=np.uint8,
    )

    output[:, :w] = image

    bar_width = 40

    top_margin = 120
    bottom_margin = 120

    bar_height = max(
        1,
        h
        - top_margin
        - bottom_margin,
    )

    # High value at top.
    gradient = np.linspace(
        255,
        0,
        bar_height,
        dtype=np.uint8,
    ).reshape(-1, 1)

    gradient = np.repeat(
        gradient,
        bar_width,
        axis=1,
    )

    gradient_bgr = cv2.applyColorMap(
        gradient,
        get_error_colormap(),
    )

    x0 = w + 30
    x1 = x0 + bar_width

    y0 = top_margin
    y1 = y0 + bar_height

    output[
        y0:y1,
        x0:x1,
    ] = gradient_bgr

    cv2.rectangle(
        output,
        (x0, y0),
        (x1, y1),
        (255, 255, 255),
        1,
    )

    cv2.putText(
        output,
        "Error",
        (w + 20, 45),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )

    cv2.putText(
        output,
        "[px]",
        (w + 30, 80),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )

    tick_values = np.linspace(
        vmax,
        0.0,
        6,
    )

    for i, value in enumerate(
        tick_values
    ):

        ratio = (
            i
            / (
                len(tick_values)
                - 1
            )
        )

        y = int(
            round(
                y0
                + ratio
                * bar_height
            )
        )

        cv2.line(
            output,
            (x1 + 3, y),
            (x1 + 12, y),
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )

        cv2.putText(
            output,
            f"{value:.1f}",
            (
                x1 + 18,
                y + 6,
            ),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )

    return output


def save_aggregate_error_heatmap(
    image_size: tuple[int, int],
    all_observed_points: list[np.ndarray],
    all_point_errors: list[np.ndarray],
    out_path: Path,
    highlight_threshold: float,
    sigma: float,
    heatmap_vmax: float,
) -> None:

    width, height = image_size

    if len(all_observed_points) == 0:
        print(
            "[warn] No reprojection points "
            "available for aggregate heatmap."
        )
        return

    points = np.concatenate(
        all_observed_points,
        axis=0,
    )

    errors = np.concatenate(
        all_point_errors,
        axis=0,
    )

    # ========================================================
    # Sparse accumulation
    # ========================================================

    error_sum = np.zeros(
        (height, width),
        dtype=np.float32,
    )

    sample_count = np.zeros(
        (height, width),
        dtype=np.float32,
    )

    for point, error in zip(
        points,
        errors,
    ):

        x = int(
            round(
                float(point[0])
            )
        )

        y = int(
            round(
                float(point[1])
            )
        )

        if (
            0 <= x < width
            and 0 <= y < height
        ):

            error_sum[y, x] += float(
                error
            )

            sample_count[y, x] += 1.0

    # ========================================================
    # Spatial smoothing
    #
    # Blur numerator and denominator independently,
    # then divide them.
    #
    # This produces a local weighted average error rather
    # than simply blurring raw error magnitudes.
    # ========================================================

    if sigma > 0.0:

        blurred_error = cv2.GaussianBlur(
            error_sum,
            (0, 0),
            sigmaX=sigma,
            sigmaY=sigma,
        )

        blurred_count = cv2.GaussianBlur(
            sample_count,
            (0, 0),
            sigmaX=sigma,
            sigmaY=sigma,
        )

    else:

        blurred_error = error_sum
        blurred_count = sample_count

    average_error = np.zeros_like(
        blurred_error
    )

    valid = (
        blurred_count
        > 1e-8
    )

    average_error[valid] = (
        blurred_error[valid]
        / blurred_count[valid]
    )

    # ========================================================
    # Determine color scale
    # ========================================================

    if heatmap_vmax > 0.0:

        vmax = float(
            heatmap_vmax
        )

        scale_description = (
            f"fixed vmax={vmax:.2f}px"
        )

    else:

        # Robust scale:
        # top 5% are saturated.
        vmax = float(
            np.percentile(
                errors,
                95.0,
            )
        )

        vmax = max(
            vmax,
            1e-6,
        )

        scale_description = (
            f"vmax=P95={vmax:.2f}px"
        )

    normalized = np.clip(
        average_error / vmax,
        0.0,
        1.0,
    )

    normalized_u8 = np.round(
        normalized * 255.0
    ).astype(np.uint8)

    heat_color = cv2.applyColorMap(
        normalized_u8,
        get_error_colormap(),
    )

    # ========================================================
    # Density-dependent alpha
    #
    # Areas that were never observed remain dark.
    # ========================================================

    max_density = float(
        np.max(
            blurred_count
        )
    )

    if max_density > 0.0:

        density = (
            blurred_count
            / max_density
        )

    else:

        density = np.zeros_like(
            blurred_count
        )

    alpha = np.clip(
        density * 4.0,
        0.0,
        0.90,
    )

    alpha = alpha[..., None]

    background = np.full(
        (
            height,
            width,
            3,
        ),
        25,
        dtype=np.uint8,
    )

    heatmap = (
        background.astype(
            np.float32
        )
        * (
            1.0 - alpha
        )
        + heat_color.astype(
            np.float32
        )
        * alpha
    ).astype(np.uint8)

    # ========================================================
    # Plot actual observed corner samples
    #
    # Large errors receive a white outline.
    # ========================================================

    point_colors = error_values_to_colors(
        errors,
        vmax,
    )

    for (
        point,
        error,
        color,
    ) in zip(
        points,
        errors,
        point_colors,
    ):

        x = int(
            round(
                float(point[0])
            )
        )

        y = int(
            round(
                float(point[1])
            )
        )

        if not (
            0 <= x < width
            and 0 <= y < height
        ):
            continue

        color_tuple = tuple(
            int(v)
            for v in color
        )

        if (
            float(error)
            >= highlight_threshold
        ):

            cv2.circle(
                heatmap,
                (x, y),
                9,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

            cv2.circle(
                heatmap,
                (x, y),
                6,
                color_tuple,
                -1,
                cv2.LINE_AA,
            )

        else:

            cv2.circle(
                heatmap,
                (x, y),
                3,
                color_tuple,
                -1,
                cv2.LINE_AA,
            )

    # ========================================================
    # Statistics
    # ========================================================

    mean_error = float(
        np.mean(errors)
    )

    median_error = float(
        np.median(errors)
    )

    p95_error = float(
        np.percentile(
            errors,
            95.0,
        )
    )

    max_error = float(
        np.max(errors)
    )

    large_count = int(
        np.count_nonzero(
            errors
            >= highlight_threshold
        )
    )

    lines = [
        "Aggregate reprojection error heatmap",
        f"Samples : {len(errors)}",
        f"Mean    : {mean_error:.3f} px",
        f"Median  : {median_error:.3f} px",
        f"P95     : {p95_error:.3f} px",
        f"Max     : {max_error:.3f} px",
        (
            f">= {highlight_threshold:.1f}px : "
            f"{large_count}"
        ),
        f"Sigma   : {sigma:.1f}px",
        f"Scale   : {scale_description}",
        (
            "White ring = large-error "
            "sample"
        ),
    ]

    panel_x = 20
    panel_y = 20

    line_height = 32

    panel_width = 650

    panel_height = (
        len(lines)
        * line_height
        + 20
    )

    overlay = heatmap.copy()

    cv2.rectangle(
        overlay,
        (panel_x, panel_y),
        (
            panel_x + panel_width,
            panel_y + panel_height,
        ),
        (0, 0, 0),
        -1,
    )

    cv2.addWeighted(
        overlay,
        0.68,
        heatmap,
        0.32,
        0,
        heatmap,
    )

    for i, text in enumerate(
        lines
    ):

        y = (
            panel_y
            + 32
            + i * line_height
        )

        cv2.putText(
            heatmap,
            text,
            (
                panel_x + 12,
                y,
            ),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.70,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

    heatmap_with_bar = (
        append_error_colorbar(
            heatmap,
            vmax,
        )
    )

    out_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    ok = cv2.imwrite(
        str(out_path),
        heatmap_with_bar,
    )

    if not ok:
        raise RuntimeError(
            "Failed to save aggregate "
            f"error heatmap: {out_path}"
        )


def save_reprojection_visualizations(
    detections: list[Detection],
    board,
    rvecs: list[np.ndarray],
    tvecs: list[np.ndarray],
    K: np.ndarray,
    dist: np.ndarray,
    image_size: tuple[int, int],
    out_dir: Path,
    highlight_threshold: float,
    vector_scale: float,
    heatmap_sigma: float,
    heatmap_vmax: float,
) -> None:

    out_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    view_dir = (
        out_dir
        / "views"
    )

    view_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    all_observed_points: list[
        np.ndarray
    ] = []

    all_point_errors: list[
        np.ndarray
    ] = []

    rows: list[dict] = []

    for (
        index,
        (
            det,
            rvec,
            tvec,
        ),
    ) in enumerate(
        zip(
            detections,
            rvecs,
            tvecs,
        )
    ):

        image = cv2.imread(
            det.path,
            cv2.IMREAD_COLOR,
        )

        if image is None:

            print(
                "[warn] "
                "Cannot read image for "
                "reprojection visualization: "
                f"{det.path}"
            )

            continue

        (
            observed,
            projected,
            residual_vectors,
            point_errors,
            rmse,
        ) = compute_view_reprojection(
            det,
            board,
            rvec,
            tvec,
            K,
            dist,
        )

        all_observed_points.append(
            observed
        )

        all_point_errors.append(
            point_errors
        )

        mean_error = float(
            np.mean(
                point_errors
            )
        )

        median_error = float(
            np.median(
                point_errors
            )
        )

        max_error = float(
            np.max(
                point_errors
            )
        )

        large_count = int(
            np.count_nonzero(
                point_errors
                >= highlight_threshold
            )
        )

        vis = (
            draw_reprojection_visualization(
                image=image,
                detection=det,
                observed=observed,
                projected=projected,
                residual_vectors=(
                    residual_vectors
                ),
                point_errors=point_errors,
                rmse=rmse,
                highlight_threshold=(
                    highlight_threshold
                ),
                vector_scale=(
                    vector_scale
                ),
            )
        )

        output_name = (
            f"{index:03d}_"
            f"rmse{rmse:.2f}_"
            f"max{max_error:.2f}_"
            f"{Path(det.path).stem}.png"
        )

        output_path = (
            view_dir
            / output_name
        )

        ok = cv2.imwrite(
            str(output_path),
            vis,
        )

        if not ok:
            print(
                "[warn] "
                "Failed to save: "
                f"{output_path}"
            )

        rows.append(
            {
                "index": index,
                "path": det.path,
                "corner_count": (
                    len(
                        point_errors
                    )
                ),
                "rmse_px": rmse,
                "mean_px": (
                    mean_error
                ),
                "median_px": (
                    median_error
                ),
                "max_px": (
                    max_error
                ),
                (
                    "large_error_"
                    f"count_ge_"
                    f"{highlight_threshold:.1f}px"
                ): large_count,
            }
        )

    # ========================================================
    # Save ranked CSV
    # ========================================================

    rows_sorted = sorted(
        rows,
        key=lambda row: (
            row["rmse_px"]
        ),
        reverse=True,
    )

    csv_path = (
        out_dir
        / "reprojection_summary.csv"
    )

    if rows_sorted:

        fieldnames = list(
            rows_sorted[0].keys()
        )

        with open(
            csv_path,
            "w",
            newline="",
            encoding="utf-8",
        ) as f:

            writer = (
                csv.DictWriter(
                    f,
                    fieldnames=fieldnames,
                )
            )

            writer.writeheader()

            writer.writerows(
                rows_sorted
            )

    # ========================================================
    # Save aggregate heatmap
    # ========================================================

    heatmap_path = (
        out_dir
        / "aggregate_error_heatmap.png"
    )

    save_aggregate_error_heatmap(
        image_size=image_size,
        all_observed_points=(
            all_observed_points
        ),
        all_point_errors=(
            all_point_errors
        ),
        out_path=heatmap_path,
        highlight_threshold=(
            highlight_threshold
        ),
        sigma=heatmap_sigma,
        heatmap_vmax=(
            heatmap_vmax
        ),
    )

    print(
        "\n=== Reprojection visualization ==="
    )

    print(
        f"Per-view images : {view_dir}"
    )

    print(
        f"Summary CSV     : {csv_path}"
    )

    print(
        f"Aggregate map   : {heatmap_path}"
    )

    print(
        "==================================\n"
    )


# ============================================================
# Detection from images
# ============================================================


def detect_from_images(
    image_dir: Path,
    board,
    dictionary,
    recursive: bool,
    min_corners: int,
    save_vis_dir: Optional[Path],
) -> tuple[
    list[Detection],
    tuple[int, int],
]:

    paths = collect_images(
        image_dir,
        recursive=recursive,
    )

    if len(paths) == 0:

        raise RuntimeError(
            "No calibration images found "
            f"under: {image_dir}"
        )

    if save_vis_dir is not None:

        save_vis_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

    detections: list[
        Detection
    ] = []

    image_size: Optional[
        tuple[int, int]
    ] = None

    for path in paths:

        try:

            image, gray = (
                read_gray(path)
            )

        except RuntimeError as exc:

            print(
                f"[skip] {exc}"
            )

            continue

        h, w = image.shape[:2]

        if image_size is None:

            image_size = (
                w,
                h,
            )

        elif image_size != (
            w,
            h,
        ):

            print(
                "[skip] size mismatch: "
                f"{path} ({w}x{h}) "
                "expected "
                f"{image_size[0]}x"
                f"{image_size[1]}"
            )

            continue

        (
            charuco_corners,
            charuco_ids,
            marker_corners,
            marker_ids,
        ) = detect_charuco(
            gray,
            board,
            dictionary,
        )

        marker_count = (
            0
            if marker_ids is None
            else len(marker_ids)
        )

        corner_count = (
            0
            if charuco_ids is None
            else len(charuco_ids)
        )

        if (
            charuco_ids is None
            or charuco_corners is None
            or corner_count
            < min_corners
        ):

            print(
                "[fail] "
                f"charuco corners="
                f"{corner_count} "
                f"markers="
                f"{marker_count}: "
                f"{path}"
            )

            continue

        det = Detection(
            path=path,
            charuco_corners=np.asarray(
                charuco_corners,
                dtype=np.float32,
            ),
            charuco_ids=np.asarray(
                charuco_ids,
                dtype=np.int32,
            ),
            marker_corners=(
                marker_corners
            ),
            marker_ids=(
                marker_ids
            ),
        )

        detections.append(det)

        print(
            "[ok]   "
            f"charuco corners="
            f"{corner_count} "
            f"markers="
            f"{marker_count}: "
            f"{path}"
        )

        if save_vis_dir is not None:

            vis = draw_detection(
                image,
                det,
            )

            out_name = (
                Path(path).stem
                + "_charuco.png"
            )

            cv2.imwrite(
                str(
                    save_vis_dir
                    / out_name
                ),
                vis,
            )

    if image_size is None:

        raise RuntimeError(
            "No readable images found."
        )

    return (
        detections,
        image_size,
    )


# ============================================================
# Outlier removal
# ============================================================


def maybe_drop_outliers(
    detections: list[Detection],
    board,
    image_size: tuple[int, int],
    flags: int,
    drop_ratio: float,
    min_keep: int,
) -> CalibResult:

    (
        rms1,
        K1,
        dist1,
        rvecs1,
        tvecs1,
    ) = calibrate_detections(
        detections,
        board,
        image_size,
        flags,
    )

    per1 = (
        compute_reprojection_errors(
            detections,
            board,
            rvecs1,
            tvecs1,
            K1,
            dist1,
        )
    )

    best_detections = detections

    best = (
        rms1,
        K1,
        dist1,
        rvecs1,
        tvecs1,
        per1,
    )

    dropped_paths: list[
        str
    ] = []

    if drop_ratio > 0.0:

        n = len(
            detections
        )

        drop_k = int(
            np.ceil(
                drop_ratio
                * n
            )
        )

        drop_k = max(
            1,
            drop_k,
        )

        if (
            n - drop_k
            < min_keep
        ):

            drop_k = max(
                0,
                n - min_keep,
            )

        if drop_k > 0:

            worst_idx = np.argsort(
                per1
            )[-drop_k:]

            worst_set = set(
                int(idx)
                for idx
                in worst_idx
            )

            kept = [
                det
                for idx, det
                in enumerate(
                    detections
                )
                if idx
                not in worst_set
            ]

            dropped = [
                det
                for idx, det
                in enumerate(
                    detections
                )
                if idx
                in worst_set
            ]

            (
                rms2,
                K2,
                dist2,
                rvecs2,
                tvecs2,
            ) = calibrate_detections(
                kept,
                board,
                image_size,
                flags,
            )

            per2 = (
                compute_reprojection_errors(
                    kept,
                    board,
                    rvecs2,
                    tvecs2,
                    K2,
                    dist2,
                )
            )

            print(
                "\n=== Outlier removal ==="
            )

            print(
                f"Total views: {n}"
            )

            print(
                "Dropped candidate views: "
                f"{len(dropped)} "
                f"(ratio={drop_ratio})"
            )

            print(
                f"RMS before: "
                f"{rms1:.6f} "
                "| after: "
                f"{rms2:.6f}"
            )

            print(
                "Median per-view before: "
                f"{np.median(per1):.4f} "
                "| after: "
                f"{np.median(per2):.4f}"
            )

            print(
                "=======================\n"
            )

            if rms2 <= rms1:

                best_detections = kept

                best = (
                    rms2,
                    K2,
                    dist2,
                    rvecs2,
                    tvecs2,
                    per2,
                )

                dropped_paths = [
                    det.path
                    for det
                    in dropped
                ]

            else:

                print(
                    "[info] "
                    "Outlier removal did not "
                    "improve RMS; "
                    "keeping all views."
                )

    (
        rms,
        K,
        dist,
        rvecs,
        tvecs,
        per_view,
    ) = best

    return CalibResult(
        rms=float(rms),
        K=K,
        dist=dist,
        rvecs=rvecs,
        tvecs=tvecs,
        image_size=image_size,
        per_view_errors=list(
            map(
                float,
                per_view,
            )
        ),
        used_paths=[
            det.path
            for det
            in best_detections
        ],
        dropped_paths=(
            dropped_paths
        ),
    )


# ============================================================
# Save calibration result
# ============================================================


def save_yaml(
    path: Path,
    result: CalibResult,
) -> None:

    fs = cv2.FileStorage(
        str(path),
        cv2.FILE_STORAGE_WRITE,
    )

    if not fs.isOpened():

        raise RuntimeError(
            "Failed to open output YAML: "
            f"{path}"
        )

    try:

        fs.write(
            "rms",
            float(result.rms),
        )

        fs.write(
            "image_width",
            int(
                result.image_size[0]
            ),
        )

        fs.write(
            "image_height",
            int(
                result.image_size[1]
            ),
        )

        fs.write(
            "K",
            result.K,
        )

        fs.write(
            "dist",
            result.dist,
        )

        fs.write(
            "board_squares_x",
            int(SQUARES_X),
        )

        fs.write(
            "board_squares_y",
            int(SQUARES_Y),
        )

        fs.write(
            "board_square_length",
            float(
                SQUARE_LENGTH
            ),
        )

        fs.write(
            "board_marker_length",
            float(
                MARKER_LENGTH
            ),
        )

        fs.write(
            "board_dictionary",
            ARUCO_DICT_NAME,
        )

    finally:

        fs.release()


def save_npz(
    path: Path,
    result: CalibResult,
) -> None:

    np.savez(
        str(path),
        rms=result.rms,
        image_width=(
            result.image_size[0]
        ),
        image_height=(
            result.image_size[1]
        ),
        K=result.K,
        dist=result.dist,
        per_view_errors=(
            np.asarray(
                result.per_view_errors,
                dtype=np.float32,
            )
        ),
        used_paths=(
            np.asarray(
                result.used_paths
            )
        ),
        dropped_paths=(
            np.asarray(
                result.dropped_paths
            )
        ),
        board_squares_x=(
            SQUARES_X
        ),
        board_squares_y=(
            SQUARES_Y
        ),
        board_square_length=(
            SQUARE_LENGTH
        ),
        board_marker_length=(
            MARKER_LENGTH
        ),
        board_dictionary=(
            ARUCO_DICT_NAME
        ),
    )


# ============================================================
# Board export
# ============================================================


def export_board_png(
    board,
    out_path: Path,
    pixels_per_square: int,
) -> None:

    if pixels_per_square <= 0:

        raise ValueError(
            "pixels_per_square must be > 0."
        )

    out_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    size = (
        int(
            SQUARES_X
            * pixels_per_square
        ),
        int(
            SQUARES_Y
            * pixels_per_square
        ),
    )

    if hasattr(
        board,
        "generateImage",
    ):

        image = (
            board.generateImage(
                size
            )
        )

    elif hasattr(
        board,
        "draw",
    ):

        image = board.draw(
            size
        )

    else:

        raise RuntimeError(
            "This OpenCV runtime "
            "cannot render "
            "ChArUco boards."
        )

    ok = cv2.imwrite(
        str(out_path),
        image,
    )

    if not ok:

        raise RuntimeError(
            "Failed to write board "
            f"image: {out_path}"
        )


# ============================================================
# Settings
# ============================================================


def print_settings(
    args: argparse.Namespace,
) -> None:

    print(
        "=== ChArUco calibration settings ==="
    )

    print(
        "Images          : "
        f"{Path(args.images).expanduser()}"
    )

    print(
        f"Recursive       : "
        f"{args.recursive}"
    )

    print(
        f"SQUARES_X/Y     : "
        f"{SQUARES_X} x {SQUARES_Y}"
    )

    print(
        f"SQUARE_LENGTH   : "
        f"{SQUARE_LENGTH}"
    )

    print(
        f"MARKER_LENGTH   : "
        f"{MARKER_LENGTH}"
    )

    print(
        f"ARUCO_DICT_NAME : "
        f"{ARUCO_DICT_NAME}"
    )

    print(
        f"MIN_CORNERS     : "
        f"{args.min_corners}"
    )

    print(
        f"DROP_RATIO      : "
        f"{args.drop_ratio}"
    )

    print(
        f"MIN_KEEP        : "
        f"{args.min_keep}"
    )

    print(
        f"CALIB_FLAGS     : "
        f"{CALIBRATION_FLAGS}"
    )

    print(
        f"SAVE_DET_VIS    : "
        f"{args.save_vis}"
    )

    print(
        f"SAVE_REPROJ_VIS : "
        f"{args.save_reproj_vis}"
    )

    if args.save_reproj_vis:

        print(
            "HIGHLIGHT_ERROR : "
            f"{args.highlight_threshold} px"
        )

        print(
            "VECTOR_SCALE    : "
            f"{args.reproj_vector_scale}"
        )

        print(
            "HEATMAP_SIGMA   : "
            f"{args.heatmap_sigma}"
        )

        print(
            "HEATMAP_VMAX    : "
            f"{args.heatmap_vmax}"
        )

    print(
        "===================================="
    )


# ============================================================
# Arguments
# ============================================================


def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        description=(
            "Calibrate GigE camera intrinsics "
            "from captured ChArUco board images."
        )
    )

    parser.add_argument(
        "--images",
        type=Path,
        default=DEFAULT_IMAGE_DIR,
        help=(
            "Directory containing raw "
            "board captures "
            f"(default: {DEFAULT_IMAGE_DIR})"
        ),
    )

    parser.add_argument(
        "--recursive",
        action="store_true",
        help=(
            "Search image directory "
            "recursively."
        ),
    )

    parser.add_argument(
        "--out-yaml",
        type=Path,
        default=DEFAULT_OUT_YAML,
        help=(
            "Output YAML path read by "
            "viewer.py --rectify "
            f"(default: {DEFAULT_OUT_YAML})"
        ),
    )

    parser.add_argument(
        "--out-npz",
        type=Path,
        default=DEFAULT_OUT_NPZ,
        help=(
            "Output NPZ path "
            f"(default: {DEFAULT_OUT_NPZ})"
        ),
    )

    # --------------------------------------------------------
    # Detection visualization
    # --------------------------------------------------------

    parser.add_argument(
        "--save-vis",
        action="store_true",
        help=(
            "Save ChArUco detection "
            "overlay images."
        ),
    )

    parser.add_argument(
        "--vis-dir",
        type=Path,
        default=DEFAULT_VIS_DIR,
        help=(
            "Detection overlay output "
            "directory "
            f"(default: {DEFAULT_VIS_DIR})"
        ),
    )

    # --------------------------------------------------------
    # Reprojection error visualization
    # --------------------------------------------------------

    parser.add_argument(
        "--save-reproj-vis",
        action="store_true",
        help=(
            "Save per-image reprojection "
            "error overlays, summary CSV, "
            "and aggregate error heatmap."
        ),
    )

    parser.add_argument(
        "--reproj-vis-dir",
        type=Path,
        default=DEFAULT_REPROJ_VIS_DIR,
        help=(
            "Reprojection visualization "
            "output directory "
            f"(default: "
            f"{DEFAULT_REPROJ_VIS_DIR})"
        ),
    )

    parser.add_argument(
        "--highlight-threshold",
        type=float,
        default=5.0,
        help=(
            "Highlight individual corners "
            "whose reprojection error is "
            "at least this many pixels "
            "(default: 5.0)."
        ),
    )

    parser.add_argument(
        "--reproj-vector-scale",
        type=float,
        default=8.0,
        help=(
            "Scale factor used only when "
            "drawing reprojection error "
            "vectors. "
            "The actual projected point "
            "is still drawn separately "
            "(default: 8.0)."
        ),
    )

    parser.add_argument(
        "--heatmap-sigma",
        type=float,
        default=80.0,
        help=(
            "Gaussian smoothing sigma in "
            "pixels for aggregate spatial "
            "error heatmap "
            "(default: 80.0)."
        ),
    )

    parser.add_argument(
        "--heatmap-vmax",
        type=float,
        default=0.0,
        help=(
            "Maximum error represented by "
            "the heatmap color scale. "
            "Use 0 to automatically use "
            "the 95th percentile "
            "(default: 0)."
        ),
    )

    # --------------------------------------------------------
    # Outlier removal
    # --------------------------------------------------------

    parser.add_argument(
        "--drop-ratio",
        type=float,
        default=0.10,
        help=(
            "Drop this ratio of worst views "
            "by reprojection error. "
            "Use 0 to disable."
        ),
    )

    parser.add_argument(
        "--min-keep",
        type=int,
        default=12,
        help=(
            "Keep at least this many views "
            "after outlier removal."
        ),
    )

    parser.add_argument(
        "--min-corners",
        type=int,
        default=MIN_CHARUCO_CORNERS,
        help=(
            "Minimum ChArUco corners "
            "required per image."
        ),
    )

    # --------------------------------------------------------
    # Board export
    # --------------------------------------------------------

    parser.add_argument(
        "--export-board-png",
        type=Path,
        default=None,
        help=(
            "Optional path to export "
            "the configured board image "
            "as PNG."
        ),
    )

    parser.add_argument(
        "--board-pixels-per-square",
        type=int,
        default=200,
        help=(
            "Pixel size per square when "
            "exporting --export-board-png."
        ),
    )

    parser.add_argument(
        "--only-export-board",
        action="store_true",
        help=(
            "Export the board PNG and exit."
        ),
    )

    args = parser.parse_args()

    if (
        args.drop_ratio < 0.0
        or args.drop_ratio >= 1.0
    ):

        parser.error(
            "--drop-ratio must be in [0, 1)."
        )

    if args.min_keep < 1:

        parser.error(
            "--min-keep must be >= 1."
        )

    if args.min_corners < 4:

        parser.error(
            "--min-corners must be >= 4."
        )

    if (
        args.highlight_threshold
        < 0.0
    ):

        parser.error(
            "--highlight-threshold "
            "must be >= 0."
        )

    if (
        args.reproj_vector_scale
        <= 0.0
    ):

        parser.error(
            "--reproj-vector-scale "
            "must be > 0."
        )

    if args.heatmap_sigma < 0.0:

        parser.error(
            "--heatmap-sigma "
            "must be >= 0."
        )

    if args.heatmap_vmax < 0.0:

        parser.error(
            "--heatmap-vmax "
            "must be >= 0."
        )

    if (
        args.only_export_board
        and args.export_board_png
        is None
    ):

        parser.error(
            "--only-export-board requires "
            "--export-board-png."
        )

    return args


# ============================================================
# Main
# ============================================================


def main() -> None:

    args = parse_args()

    dictionary = (
        create_dictionary(
            ARUCO_DICT_NAME
        )
    )

    board = (
        create_charuco_board(
            SQUARES_X,
            SQUARES_Y,
            SQUARE_LENGTH,
            MARKER_LENGTH,
            dictionary,
        )
    )

    # ========================================================
    # Optional board export
    # ========================================================

    if (
        args.export_board_png
        is not None
    ):

        export_board_png(
            board,
            args.export_board_png,
            args.board_pixels_per_square,
        )

        print(
            "Exported board image: "
            f"{args.export_board_png}"
        )

        if args.only_export_board:
            return

    # ========================================================
    # Detection
    # ========================================================

    print_settings(args)

    save_vis_dir = (
        args.vis_dir
        if args.save_vis
        else None
    )

    (
        detections,
        image_size,
    ) = detect_from_images(
        args.images,
        board,
        dictionary,
        recursive=args.recursive,
        min_corners=(
            args.min_corners
        ),
        save_vis_dir=(
            save_vis_dir
        ),
    )

    if len(detections) < 5:

        raise RuntimeError(
            "Not enough valid "
            "ChArUco views: "
            f"{len(detections)}. "
            "Capture roughly 20-40 "
            "varied board poses."
        )

    # ========================================================
    # Calibration
    # ========================================================

    result = maybe_drop_outliers(
        detections,
        board,
        image_size,
        flags=(
            CALIBRATION_FLAGS
        ),
        drop_ratio=(
            args.drop_ratio
        ),
        min_keep=(
            args.min_keep
        ),
    )

    # ========================================================
    # Save calibration
    # ========================================================

    args.out_yaml.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    args.out_npz.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    save_yaml(
        args.out_yaml,
        result,
    )

    save_npz(
        args.out_npz,
        result,
    )

    # ========================================================
    # Final reprojection visualization
    #
    # rvecs/tvecs correspond exactly to result.used_paths.
    # Therefore reorder detections using result.used_paths.
    # ========================================================

    if args.save_reproj_vis:

        detection_by_path = {
            det.path: det
            for det
            in detections
        }

        used_detections = [
            detection_by_path[path]
            for path
            in result.used_paths
        ]

        save_reprojection_visualizations(
            detections=(
                used_detections
            ),
            board=board,
            rvecs=result.rvecs,
            tvecs=result.tvecs,
            K=result.K,
            dist=result.dist,
            image_size=(
                result.image_size
            ),
            out_dir=(
                args.reproj_vis_dir
            ),
            highlight_threshold=(
                args.highlight_threshold
            ),
            vector_scale=(
                args.reproj_vector_scale
            ),
            heatmap_sigma=(
                args.heatmap_sigma
            ),
            heatmap_vmax=(
                args.heatmap_vmax
            ),
        )

    # ========================================================
    # Print result
    # ========================================================

    per = np.asarray(
        result.per_view_errors,
        dtype=np.float32,
    )

    print(
        "\n=== Calibration result ==="
    )

    print(
        "RMS reprojection error: "
        f"{result.rms:.6f} px"
    )

    print(
        "Image size: "
        f"{result.image_size[0]}x"
        f"{result.image_size[1]}"
    )

    print(
        "K:\n",
        result.K,
    )

    print(
        "dist:",
        result.dist.ravel(),
    )

    print(
        f"Views used: "
        f"{len(result.used_paths)}"
    )

    print(
        f"Dropped views: "
        f"{len(result.dropped_paths)}"
    )

    print(
        "Per-view error: "
        f"mean={per.mean():.4f} "
        f"median={np.median(per):.4f} "
        f"max={per.max():.4f} px"
    )

    if result.dropped_paths:

        print(
            "Dropped list:"
        )

        for path in (
            result.dropped_paths
        ):

            print(
                "  -",
                path,
            )

    print(
        f"\nSaved: "
        f"{args.out_yaml}"
    )

    print(
        f"Saved: "
        f"{args.out_npz}"
    )

    if save_vis_dir is not None:

        print(
            "Saved detection "
            "visualizations: "
            f"{save_vis_dir}"
        )

    if args.save_reproj_vis:

        print(
            "Saved reprojection "
            "visualizations: "
            f"{args.reproj_vis_dir}"
        )


if __name__ == "__main__":
    main()