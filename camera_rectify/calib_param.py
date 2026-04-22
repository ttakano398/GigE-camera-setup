#!/usr/bin/env python3
# calibrate_camera.py

import argparse
import glob
import os
from dataclasses import dataclass
from typing import List, Tuple, Optional

import cv2
import numpy as np


# ============================================================
# User-configurable parameters (edit here first)
# ============================================================
CHECKERBOARD: Tuple[int, int] = (10, 8)      # (cols, rows) inner corners
SQUARE_SIZE: float = 6.668 * 0.001           # meters
# ============================================================


@dataclass
class CalibResult:
    rms: float
    K: np.ndarray
    dist: np.ndarray
    rvecs: List[np.ndarray]
    tvecs: List[np.ndarray]
    image_size: Tuple[int, int]
    per_view_errors: List[float]
    used_paths: List[str]
    dropped_paths: List[str]


def collect_images(repo_dir: str, exts=(".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif")) -> List[str]:
    repo_dir = os.path.abspath(repo_dir)
    paths: List[str] = []
    for ext in exts:
        paths.extend(glob.glob(os.path.join(repo_dir, "**", f"*{ext}"), recursive=True))
        paths.extend(glob.glob(os.path.join(repo_dir, "**", f"*{ext.upper()}"), recursive=True))
    return sorted(list(set(paths)))


def make_object_points(board_size: Tuple[int, int], square_size: float) -> np.ndarray:
    cols, rows = board_size
    objp = np.zeros((rows * cols, 3), np.float32)
    objp[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)
    objp *= float(square_size)
    return objp


def preprocess_gray(gray: np.ndarray) -> np.ndarray:
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    return gray


def detect_corners(gray: np.ndarray, board_size: Tuple[int, int]) -> Optional[np.ndarray]:
    # SB (more robust)
    try:
        ret, corners = cv2.findChessboardCornersSB(gray, board_size)
        if ret:
            return corners.astype(np.float32)
    except Exception:
        pass

    # fallback
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    ret, corners = cv2.findChessboardCorners(gray, board_size, flags)
    if not ret:
        return None

    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1e-3)
    corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
    return corners.astype(np.float32)


def compute_reproj_errors_rms(
    objpoints: List[np.ndarray],
    imgpoints: List[np.ndarray],
    rvecs: List[np.ndarray],
    tvecs: List[np.ndarray],
    K: np.ndarray,
    dist: np.ndarray,
) -> List[float]:
    errs = []
    for i in range(len(objpoints)):
        proj, _ = cv2.projectPoints(objpoints[i], rvecs[i], tvecs[i], K, dist)
        e = cv2.norm(imgpoints[i], proj, cv2.NORM_L2)
        e = float(np.sqrt((e * e) / len(proj)))  # per-point RMS (px)
        errs.append(e)
    return errs


def visualize_worst_views(
    worst_paths: List[str],
    worst_corners: List[np.ndarray],
    board_size: Tuple[int, int],
    worst_errors: List[float],
):
    """
    Show worst views with drawn corners. Press 'q' to quit immediately.
    Any other key goes next image.
    """
    win = "worst_views (press any key for next, q to quit)"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)

    for p, corners, err in zip(worst_paths, worst_corners, worst_errors):
        img = cv2.imread(p, cv2.IMREAD_COLOR)
        if img is None:
            continue

        vis = img.copy()
        cv2.drawChessboardCorners(vis, board_size, corners, True)

        # overlay text
        h, w = vis.shape[:2]
        text1 = f"err={err:.4f} px"
        text2 = os.path.basename(p)
        cv2.putText(vis, text1, (10, 35), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2, cv2.LINE_AA)
        cv2.putText(vis, text2, (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(vis, "q: quit | other key: next", (10, h - 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)

        cv2.imshow(win, vis)
        key = cv2.waitKey(0) & 0xFF
        if key == ord("q"):
            break

    cv2.destroyWindow(win)


def calibrate_from_repo(
    repo_dir: str,
    board_size: Tuple[int, int],
    square_size: float,
    visualize: bool = False,
    save_vis_dir: Optional[str] = None,
    outlier_drop_ratio: float = 0.10,
    min_keep: int = 10,
    show_worst4: bool = True,
) -> CalibResult:
    img_paths = collect_images(repo_dir)
    if len(img_paths) == 0:
        raise RuntimeError(f"No images found under: {repo_dir}")

    objp = make_object_points(board_size, square_size)

    objpoints: List[np.ndarray] = []
    imgpoints: List[np.ndarray] = []
    used_paths: List[str] = []
    used_corners: List[np.ndarray] = []  # for later visualization

    image_size: Optional[Tuple[int, int]] = None  # (w, h)

    if visualize and save_vis_dir:
        os.makedirs(save_vis_dir, exist_ok=True)

    # ---- detect corners ----
    for p in img_paths:
        img = cv2.imread(p, cv2.IMREAD_COLOR)
        if img is None:
            print(f"[skip] cannot read: {p}")
            continue

        h, w = img.shape[:2]
        if image_size is None:
            image_size = (w, h)
        elif (w, h) != image_size:
            print(f"[skip] size mismatch: {p} ({w}x{h}) expected {image_size[0]}x{image_size[1]}")
            continue

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = preprocess_gray(gray)

        corners = detect_corners(gray, board_size)
        if corners is None:
            print(f"[fail] corners not found: {p}")
            continue

        objpoints.append(objp.copy())
        imgpoints.append(corners)
        used_paths.append(p)
        used_corners.append(corners)
        print(f"[ok]  corners: {p}")

        if visualize:
            vis = img.copy()
            cv2.drawChessboardCorners(vis, board_size, corners, True)
            if save_vis_dir:
                out = os.path.join(save_vis_dir, os.path.basename(p))
                cv2.imwrite(out, vis)
            else:
                cv2.imshow("corners", vis)
                cv2.waitKey(30)

    if visualize and not save_vis_dir:
        cv2.destroyAllWindows()

    if len(objpoints) < 5:
        raise RuntimeError(f"Not enough valid views for calibration: {len(objpoints)} (need ~>= 5-10)")

    assert image_size is not None

    # ---- 1st calibration ----
    rms1, K1, dist1, rvecs1, tvecs1 = cv2.calibrateCamera(objpoints, imgpoints, image_size, None, None)
    per1 = compute_reproj_errors_rms(objpoints, imgpoints, rvecs1, tvecs1, K1, dist1)

    # ---- outlier removal ----
    n = len(per1)
    drop_k = int(np.ceil(outlier_drop_ratio * n))
    drop_k = max(1, drop_k)
    if n - drop_k < min_keep:
        drop_k = max(0, n - min_keep)

    best = {
        "rms": float(rms1),
        "K": K1,
        "dist": dist1,
        "rvecs": list(rvecs1),
        "tvecs": list(tvecs1),
        "per": list(map(float, per1)),
        "paths": used_paths,
        "corners": used_corners,
        "dropped": [],
    }

    if drop_k > 0:
        worst_idx = np.argsort(per1)[-drop_k:]
        worst_set = set(int(i) for i in worst_idx)

        mask = [i not in worst_set for i in range(n)]
        obj2 = [objpoints[i] for i in range(n) if mask[i]]
        img2 = [imgpoints[i] for i in range(n) if mask[i]]
        paths2 = [used_paths[i] for i in range(n) if mask[i]]
        corners2 = [used_corners[i] for i in range(n) if mask[i]]
        dropped = [used_paths[i] for i in range(n) if not mask[i]]

        rms2, K2, dist2, rvecs2, tvecs2 = cv2.calibrateCamera(obj2, img2, image_size, None, None)
        per2 = compute_reproj_errors_rms(obj2, img2, rvecs2, tvecs2, K2, dist2)

        print("\n=== Outlier removal ===")
        print(f"Total views: {n}")
        print(f"Dropped (worst) views: {len(dropped)}  (ratio={outlier_drop_ratio})")
        print(f"RMS before: {rms1:.6f} | after: {rms2:.6f}")
        print(f"Per-view median before: {np.median(per1):.4f} | after: {np.median(per2):.4f}")
        print("=======================\n")

        if rms2 < rms1:
            best = {
                "rms": float(rms2),
                "K": K2,
                "dist": dist2,
                "rvecs": list(rvecs2),
                "tvecs": list(tvecs2),
                "per": list(map(float, per2)),
                "paths": paths2,
                "corners": corners2,
                "dropped": dropped,
            }
        else:
            best["dropped"] = dropped  # 落とした候補は表示用に保持

    # ---- visualize worst 4 (from final-used set) ----
    if show_worst4:
        per = np.array(best["per"], dtype=np.float32)
        m = len(per)
        topk = min(4, m)
        worst4_idx = np.argsort(per)[-topk:][::-1]  # desc
        worst_paths = [best["paths"][i] for i in worst4_idx]
        worst_corners = [best["corners"][i] for i in worst4_idx]
        worst_errors = [float(per[i]) for i in worst4_idx]

        print("=== Worst 4 views (final set) ===")
        for p, e in zip(worst_paths, worst_errors):
            print(f"{e:.4f} px  {p}")
        print("================================\n")

        visualize_worst_views(worst_paths, worst_corners, board_size, worst_errors)

    return CalibResult(
        rms=best["rms"],
        K=best["K"],
        dist=best["dist"],
        rvecs=best["rvecs"],
        tvecs=best["tvecs"],
        image_size=image_size,
        per_view_errors=best["per"],
        used_paths=best["paths"],
        dropped_paths=best["dropped"],
    )


def save_yaml(path: str, result: CalibResult):
    fs = cv2.FileStorage(path, cv2.FILE_STORAGE_WRITE)
    fs.write("rms", result.rms)
    fs.write("image_width", int(result.image_size[0]))
    fs.write("image_height", int(result.image_size[1]))
    fs.write("K", result.K)
    fs.write("dist", result.dist)
    fs.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="path to repository (root directory) containing calibration images")
    parser.add_argument("--vis", action="store_true", help="visualize detected corners (GUI) or save if --vis-dir is set")
    parser.add_argument("--vis-dir", default=None, help="if set, save visualization images here instead of showing GUI")
    parser.add_argument("--out-yaml", default="camera_calib.yaml", help="output YAML path")
    parser.add_argument("--out-npz", default="camera_calib.npz", help="output NPZ path")
    parser.add_argument("--drop-ratio", type=float, default=0.10, help="drop worst X ratio views by per-view error (default 0.10)")
    parser.add_argument("--min-keep", type=int, default=10, help="keep at least this many views after dropping (default 10)")
    parser.add_argument("--no-worst4", action="store_true", help="disable worst-4 visualization")
    args = parser.parse_args()

    print("=== Calibration settings ===")
    print(f"CHECKERBOARD (cols,rows): {CHECKERBOARD}")
    print(f"SQUARE_SIZE: {SQUARE_SIZE}")
    print(f"DROP_RATIO: {args.drop_ratio}")
    print(f"MIN_KEEP: {args.min_keep}")
    print("============================")

    res = calibrate_from_repo(
        repo_dir=args.repo,
        board_size=CHECKERBOARD,
        square_size=SQUARE_SIZE,
        visualize=args.vis,
        save_vis_dir=args.vis_dir,
        outlier_drop_ratio=args.drop_ratio,
        min_keep=args.min_keep,
        show_worst4=(not args.no_worst4),
    )

    save_yaml(args.out_yaml, res)
    np.savez(
        args.out_npz,
        rms=res.rms,
        image_width=res.image_size[0],
        image_height=res.image_size[1],
        K=res.K,
        dist=res.dist,
        per_view_errors=np.array(res.per_view_errors, dtype=np.float32),
        used_paths=np.array(res.used_paths),
        dropped_paths=np.array(res.dropped_paths),
    )

    print("\n=== Calibration result ===")
    print(f"RMS reprojection error: {res.rms:.6f} (px)")
    print(f"Image size: {res.image_size[0]}x{res.image_size[1]}")
    print("K (intrinsics):\n", res.K)
    print("dist (distortion):\n", res.dist.ravel())
    print(f"Views used: {len(res.per_view_errors)}")
    print(f"Dropped views: {len(res.dropped_paths)}")
    if len(res.dropped_paths) > 0:
        print("Dropped list (top 10):")
        for p in res.dropped_paths[:10]:
            print("  -", p)

    per = np.array(res.per_view_errors, dtype=np.float32)
    print(f"Per-view error (RMS px): mean={per.mean():.4f}, median={np.median(per):.4f}, max={per.max():.4f}")

    print(f"\nSaved: {args.out_yaml}")
    print(f"Saved: {args.out_npz}")


if __name__ == "__main__":
    main()