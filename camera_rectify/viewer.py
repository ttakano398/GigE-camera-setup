# viewer.py
import os
import time
import argparse
from datetime import datetime

import cv2
import numpy as np


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def try_set_capture(cap, width, height, fps):
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)

    w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    f = cap.get(cv2.CAP_PROP_FPS)
    print(f"[cap props] width={w:.0f} height={h:.0f} fps={f:.3f}")


def bayer_to_bgr_if_needed(frame):
    if frame is None:
        return None
    if len(frame.shape) == 2:
        return cv2.cvtColor(frame, cv2.COLOR_BayerGR2BGR)
    if len(frame.shape) == 3 and frame.shape[2] == 1:
        return cv2.cvtColor(frame[:, :, 0], cv2.COLOR_BayerGR2BGR)
    return frame


def load_calib_yaml(yaml_path: str):
    fs = cv2.FileStorage(yaml_path, cv2.FILE_STORAGE_READ)
    if not fs.isOpened():
        raise RuntimeError(f"Failed to open calib yaml: {yaml_path}")

    K_node = fs.getNode("K")
    dist_node = fs.getNode("dist")
    if K_node.empty() or dist_node.empty():
        fs.release()
        raise RuntimeError("Invalid yaml: 'K' or 'dist' not found.")

    K = np.array(K_node.mat(), dtype=np.float64)
    dist = np.array(dist_node.mat(), dtype=np.float64).reshape(-1, 1)
    fs.release()
    return K, dist


def build_undistort_maps(K, dist, image_size, alpha=0.0):
    w, h = image_size
    newK, _ = cv2.getOptimalNewCameraMatrix(K, dist, (w, h), alpha, (w, h))
    map1, map2 = cv2.initUndistortRectifyMap(
        K, dist, R=None, newCameraMatrix=newK, size=(w, h), m1type=cv2.CV_16SC2
    )
    return newK, map1, map2


class ManualWarp:
    """
    角点等を使わない、等距離モデル風の手動ワープ（見た目調整用）。
    """
    def __init__(self, f_scale=0.14, zoom=3.0, output_zoom=1.0, resolution_scale=1.0):
        self.f_scale = float(f_scale)
        self.zoom = float(zoom)
        self.output_zoom = float(output_zoom)
        self.resolution_scale = float(resolution_scale)

        self.map_x = None
        self.map_y = None
        self.current_config = None  # (w,h,res,f_scale,zoom,out_zoom)

    def _calculate_maps(self, w: int, h: int):
        out_w = max(1, int(np.round(w * self.resolution_scale)))
        out_h = max(1, int(np.round(h * self.resolution_scale)))

        cx, cy = w / 2.0, h / 2.0

        x = np.linspace(0, w - 1, out_w)
        y = np.linspace(0, h - 1, out_h)
        xx, yy = np.meshgrid(x, y)

        dx = xx - cx
        dy = yy - cy
        r = np.sqrt(dx**2 + dy**2)

        f = self.f_scale * min(w, h)
        theta = np.arctan2(r, f)
        cv_map = f * theta

        eps = 1e-8
        ix = cx + (dx * self.zoom / (r + eps)) * cv_map
        iy = cy + (dy * self.zoom / (r + eps)) * cv_map

        self.map_x = ix.astype(np.float32)
        self.map_y = iy.astype(np.float32)

    def ensure_maps(self, w: int, h: int):
        cfg = (w, h, self.resolution_scale, self.f_scale, self.zoom, self.output_zoom)
        if self.current_config == cfg and self.map_x is not None and self.map_y is not None:
            return
        self._calculate_maps(w, h)
        self.current_config = cfg

    def apply(self, frame_bgr: np.ndarray) -> np.ndarray:
        h, w = frame_bgr.shape[:2]
        self.ensure_maps(w, h)
        warped = cv2.remap(
            frame_bgr, self.map_x, self.map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT
        )
        if self.output_zoom != 1.0:
            warped = self._apply_output_zoom(warped)
        return warped

    def _apply_output_zoom(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        z = self.output_zoom
        if z == 1.0:
            return frame

        cx, cy = w // 2, h // 2
        if z > 1.0:
            new_w = max(1, int(w / z))
            new_h = max(1, int(h / z))
            x1 = max(0, cx - new_w // 2)
            y1 = max(0, cy - new_h // 2)
            x2 = min(w, x1 + new_w)
            y2 = min(h, y1 + new_h)
            crop = frame[y1:y2, x1:x2]
            return cv2.resize(crop, (w, h), interpolation=cv2.INTER_LINEAR)
        else:
            new_w = max(1, int(w * z))
            new_h = max(1, int(h * z))
            small = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            out = np.zeros_like(frame)
            sx = (w - new_w) // 2
            sy = (h - new_h) // 2
            out[sy:sy + new_h, sx:sx + new_w] = small
            return out


def stack_vertical(frames: list) -> np.ndarray:
    widths = [im.shape[1] for im in frames]
    target_w = max(widths)
    resized = []
    for im in frames:
        h, w = im.shape[:2]
        if w != target_w:
            new_h = max(1, int(h * target_w / w))
            im = cv2.resize(im, (target_w, new_h), interpolation=cv2.INTER_LINEAR)
        resized.append(im)
    return np.vstack(resized)


def put_label(img, text, y=40):
    cv2.putText(img, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)


def build_views(raw_bgr, use_calib, map1, map2, use_warp, warper, args):
    labeled = []

    h, w = raw_bgr.shape[:2]
    raw_l = raw_bgr.copy()
    put_label(raw_l, "RAW (before)")
    cv2.putText(raw_l, f"Resolution: {w}x{h}", (10, 75),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    labeled.append(raw_l)

    if use_calib and map1 is not None and map2 is not None:
        und = cv2.remap(raw_bgr, map1, map2, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        und_l = und.copy()
        put_label(und_l, "UNDISTORT (OpenCV calib)")
        labeled.append(und_l)

    if use_warp and warper is not None:
        wrp = warper.apply(raw_bgr)
        wrp_l = wrp.copy()
        put_label(wrp_l, f"WARP (manual) f_scale={args.warp_f_scale:.3f} zoom={args.warp_zoom:.2f}")
        labeled.append(wrp_l)

    return stack_vertical(labeled)


def main():
    parser = argparse.ArgumentParser()

    # input selection
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--video", type=str, default=None, help="path to .mp4 (if set, video mode)")

    # camera desired settings (only meaningful for camera mode)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=15)

    # OpenCV undistort
    parser.add_argument("--calib", type=str, default=None, help="path to camera_calib.yaml")
    parser.add_argument("--alpha", type=float, default=0.0, help="undistort alpha in [0,1]")

    # manual warp
    parser.add_argument("--warp", action="store_true", help="also show manual warp view")
    parser.add_argument("--warp-f-scale", type=float, default=0.14)
    parser.add_argument("--warp-zoom", type=float, default=3.0)
    parser.add_argument("--warp-output-zoom", type=float, default=1.0)
    parser.add_argument("--warp-resolution-scale", type=float, default=1.0)

    # misc
    parser.add_argument("--loop", action="store_true", help="loop video playback (video mode)")
    args = parser.parse_args()

    # window
    window = "viewer"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)

    # output dir (capture is only meaningful for camera mode; in video mode we save current frame)
    out_dir = "captures"
    ensure_dir(out_dir)

    # open input
    video_mode = args.video is not None
    if video_mode:
        cap = cv2.VideoCapture(args.video)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open video: {args.video}")
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        vid_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if total_frames <= 0:
            # some codecs report 0; still allow playback, but disable seekbar max correctness
            total_frames = 1
        print(f"[video] {args.video}")
        print(f"[video] fps={vid_fps:.3f} total_frames={total_frames}")
    else:
        cap = cv2.VideoCapture(args.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open camera: VideoCapture({args.device}) failed.")
        try_set_capture(cap, args.width, args.height, args.fps)
        total_frames = None
        vid_fps = float(args.fps)

    # optional calib
    use_calib = args.calib is not None
    map1 = map2 = None
    if use_calib:
        K, dist = load_calib_yaml(args.calib)
        print("[calib] loaded")
        print("K:\n", K)
        print("dist:", dist.ravel())

    # optional warp
    use_warp = bool(args.warp)
    warper = None
    if use_warp:
        warper = ManualWarp(
            f_scale=args.warp_f_scale,
            zoom=args.warp_zoom,
            output_zoom=args.warp_output_zoom,
            resolution_scale=args.warp_resolution_scale,
        )
        print("[warp] enabled:",
              f"f_scale={args.warp_f_scale}, zoom={args.warp_zoom}, "
              f"output_zoom={args.warp_output_zoom}, res_scale={args.warp_resolution_scale}")

    # read first frame to build maps / validate
    ok, frame = cap.read()
    if not ok:
        raise RuntimeError("Failed to read first frame.")
    frame_bgr = bayer_to_bgr_if_needed(frame) if not video_mode else frame
    if frame_bgr is None:
        raise RuntimeError("First frame is None.")
    h0, w0 = frame_bgr.shape[:2]
    print("[first frame]", frame_bgr.shape)

    if use_calib:
        _, map1, map2 = build_undistort_maps(K, dist, (w0, h0), alpha=args.alpha)

    # video mode UI: seekbar + play toggle
    play_state = 1  # 1=play, 0=pause
    updating = {"seek": False}

    def on_seek(pos):
        if not video_mode:
            return
        if updating["seek"]:
            return
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(pos))

    def on_play(v):
        # v: 0/1
        nonlocal play_state
        play_state = 1 if int(v) != 0 else 0

    if video_mode:
        # recreate first frame position to 0
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        cv2.createTrackbar("Play(0/1)", window, 1, 1, on_play)
        cv2.createTrackbar("Seek(frame)", window, 0, max(1, total_frames - 1), on_seek)

    status_msg = ""
    status_ts = 0.0
    last_sec = None
    sec_counter = 0

    # main loop
    last_frame_idx = 0
    last_tick = time.time()

    while True:
        if video_mode:
            # decide if we fetch next frame
            if play_state == 1:
                ok, frame = cap.read()
                if not ok:
                    if args.loop:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ok, frame = cap.read()
                    if not ok:
                        break
            else:
                # paused: just re-use current position frame by grabbing without advancing is hard with VideoCapture
                # so we read current frame index, set back one, read again to display stable.
                pos = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
                pos = max(0, pos - 1)
                cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
                ok, frame = cap.read()
                if not ok:
                    break

            frame_bgr = frame  # video is already BGR

            # update seekbar to current position (avoid callback recursion)
            cur = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
            cur = max(0, cur - 1)  # after read, POS_FRAMES points to next
            if cur != last_frame_idx:
                updating["seek"] = True
                cv2.setTrackbarPos("Seek(frame)", window, int(np.clip(cur, 0, total_frames - 1)))
                updating["seek"] = False
                last_frame_idx = cur

        else:
            ok, frame = cap.read()
            if not ok:
                print("Failed to read frame.")
                break
            frame_bgr = bayer_to_bgr_if_needed(frame)

        vis = build_views(frame_bgr, use_calib, map1, map2, use_warp, warper, args)

        # overlay controls
        vh, vw = vis.shape[:2]
        if video_mode:
            cv2.putText(vis, "Space/p: play/pause | <-/->: step | q/Esc: quit | c: save frame",
                        (10, vh - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
        else:
            cv2.putText(vis, "c: capture (RAW) | q/Esc: quit",
                        (10, vh - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)

        if status_msg and (time.time() - status_ts) < 3.0:
            cv2.putText(vis, status_msg, (10, vh - 15),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)

        cv2.imshow(window, vis)

        # playback pacing (video only)
        wait_ms = 1
        if video_mode and vid_fps > 0:
            # when playing, approximate fps
            if play_state == 1:
                wait_ms = max(1, int(1000.0 / vid_fps))
            else:
                wait_ms = 30

        key = cv2.waitKey(wait_ms) & 0xFF

        # key actions
        if key == ord("q") or key == 27:
            break

        if video_mode and (key == ord(" ") or key == ord("p")):
            play_state = 0 if play_state == 1 else 1
            cv2.setTrackbarPos("Play(0/1)", window, play_state)

        # Arrow keys in OpenCV: 81/83 (left/right) may appear as 0xFF?? depends on backend
        # We'll handle common codes: 81 left, 83 right.
        if video_mode and key in (81, 83):
            # pause when stepping
            play_state = 0
            cv2.setTrackbarPos("Play(0/1)", window, play_state)
            pos = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
            pos = max(0, pos - 1)
            if key == 81:
                pos = max(0, pos - 1)
            else:
                pos = min(total_frames - 1, pos + 1)
            cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
            updating["seek"] = True
            cv2.setTrackbarPos("Seek(frame)", window, pos)
            updating["seek"] = False

        if key == ord("c"):
            now = datetime.now()
            sec = now.strftime("%Y%m%d_%H%M%S")

            if sec != last_sec:
                last_sec = sec
                sec_counter = 0
            else:
                sec_counter += 1

            base = "frame" if video_mode else "cap"
            fname = f"{base}_{sec}_{sec_counter:02d}.png"
            path = os.path.join(out_dir, fname)

            ok_write = cv2.imwrite(path, frame_bgr)
            status_msg = f"Saved: {path}" if ok_write else f"Save failed: {path}"
            status_ts = time.time()

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()