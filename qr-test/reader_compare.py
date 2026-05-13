import cv2
import numpy as np
import sys
import argparse
import os
import datetime
import shutil

# -----------------------------------------------------------
# Optional Libraries Import
# -----------------------------------------------------------
try:
    from qreader import QReader
    has_qreader = True
except ImportError:
    has_qreader = False

try:
    from pyzbar.pyzbar import decode, ZBarSymbol
    has_pyzbar = True
except ImportError:
    has_pyzbar = False

def main():
    # -----------------------------------------------------------
    # 引数解析
    # -----------------------------------------------------------
    parser = argparse.ArgumentParser(description='QR Code Scanner: Compare All Models with Stats')
    parser.add_argument('--video', type=str, help='Path to video file', default=None)
    parser.add_argument('--mode', type=str, default='compare_all', 
                        choices=['wechat', 'cv2', 'compare', 'compare_all'],
                        help='Detection mode')
    parser.add_argument('--interval', type=int, default=5, 
                        help='Inference interval frames (Default: 5). Increase if slow.')
    args = parser.parse_args()

    use_wechat = args.mode in ['wechat', 'compare', 'compare_all']
    use_cv2_std = args.mode in ['cv2', 'compare', 'compare_all']
    use_yolo = args.mode in ['compare_all']
    use_pyzbar = args.mode in ['compare_all']

    if use_yolo and not has_qreader:
        print("Error: 'compare_all' requires 'pip install qreader'")
        sys.exit()
    if use_pyzbar and not has_pyzbar:
        print("Error: 'compare_all' requires 'pip install pyzbar'")
        sys.exit()

    # -----------------------------------------------------------
    # モデルの準備
    # -----------------------------------------------------------
    detector_wechat = None
    detector_cv2 = None
    detector_qreader = None

    if use_wechat:
        model_dir = "opencv_3rdparty"
        # 実行環境に合わせてパスを調整してください
        detect_proto = os.path.join(model_dir, "detect.prototxt")
        detect_model = os.path.join(model_dir, "detect.caffemodel")
        sr_proto = os.path.join(model_dir, "sr.prototxt")
        sr_model = os.path.join(model_dir, "sr.caffemodel")
        if os.path.exists(detect_proto):
            try:
                detector_wechat = cv2.wechat_qrcode_WeChatQRCode(detect_proto, detect_model, sr_proto, sr_model)
                print("Model Loaded: WeChat QRCode")
            except: pass

    if use_cv2_std:
        detector_cv2 = cv2.QRCodeDetector()
        print("Model Loaded: Standard CV2")

    if use_yolo:
        print("Loading QReader (YOLO nano)...")
        detector_qreader = QReader(model_size="n")
        print("Model Loaded: QReader (YOLO)")
        
    if use_pyzbar:
        print("Model Loaded: pyzbar")

    # -----------------------------------------------------------
    # 入力ソース
    # -----------------------------------------------------------
    is_video_mode = args.video is not None
    if is_video_mode:
        if not os.path.exists(args.video):
            print(f"Error: Not found {args.video}")
            return
        cap = cv2.VideoCapture(args.video)
        window_name = f"Scanner: {args.mode} (Int: {args.interval})"
    else:
        cap = cv2.VideoCapture(0)
        window_name = f"Scanner: {args.mode} (Camera)"

    if not cap.isOpened(): return

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if is_video_mode else 0
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps == 0: fps = 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    # -----------------------------------------------------------
    # 開始位置選択 (動画モード)
    # -----------------------------------------------------------
    start_frame_pos = 0
    if is_video_mode:
        print("=== Select Start Position and Press Enter ===")
        def on_seek_setup(val):
            nonlocal start_frame_pos
            start_frame_pos = val
            cap.set(cv2.CAP_PROP_POS_FRAMES, val)
        
        cv2.createTrackbar("Seek", window_name, 0, total_frames, on_seek_setup)
        while True:
            ret, frame = cap.read()
            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            cv2.imshow(window_name, frame)
            if cv2.waitKey(30) == 13: break

    # -----------------------------------------------------------
    # 録画用変数 & 統計用カウンタ
    # -----------------------------------------------------------
    temp_video_name = "temp_recording.mp4"
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    if os.path.exists(temp_video_name): os.remove(temp_video_name)
    writer = cv2.VideoWriter(temp_video_name, fourcc, fps, (width, height))

    # カウンタ初期化 (録画セグメントごとの統計)
    rec_total_frames = 0
    rec_count_wechat = 0
    rec_count_cv2 = 0
    rec_count_yolo = 0
    rec_count_pyzbar = 0

    # キャッシュ用変数
    cached_wechat = []
    cached_cv2 = []
    cached_yolo = []
    cached_pyzbar = []
    
    frame_counter = 0

    # -----------------------------------------------------------
    # メインループ
    # -----------------------------------------------------------
    is_playing = True
    
    def on_seek_main(val):
        cap.set(cv2.CAP_PROP_POS_FRAMES, val)
        # シーク時にキャッシュと統計は... 動画解析的にはリセットしたほうが自然？
        # ここでは描画キャッシュのみクリアし、録画統計はそのままにします（録画自体は続いているため）
        nonlocal cached_wechat, cached_cv2, cached_yolo, cached_pyzbar
        cached_wechat, cached_cv2, cached_yolo, cached_pyzbar = [], [], [], []

    if is_video_mode:
        cv2.createTrackbar("Seek", window_name, start_frame_pos, total_frames, on_seek_main)

    while True:
        t_start = cv2.getTickCount()
        current_pos = int(cap.get(cv2.CAP_PROP_POS_FRAMES)) if is_video_mode else 0

        if is_video_mode and is_playing:
             cv2.setTrackbarPos("Seek", window_name, current_pos)

        ret, frame = cap.read()
        if not ret:
            if is_video_mode:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                is_playing = False
                continue
            else:
                break
        
        draw_frame = frame.copy()

        # =======================================================
        # 推論ロジック (Intervalごと)
        # =======================================================
        if frame_counter % args.interval == 0:
            
            # 1. WeChat
            if use_wechat and detector_wechat:
                cached_wechat = []
                try:
                    res, points = detector_wechat.detectAndDecode(frame)
                    for i, info in enumerate(res):
                        if info: cached_wechat.append((points[i].astype(int), info))
                except: pass

            # 2. CV2 Std
            if use_cv2_std and detector_cv2:
                cached_cv2 = []
                try:
                    retval, decoded_info, points, _ = detector_cv2.detectAndDecodeMulti(frame)
                    if retval:
                        points = points.astype(np.int32)
                        for i, info in enumerate(decoded_info):
                            if info: cached_cv2.append((points[i], info))
                except: pass

            # 3. YOLO
            if use_yolo and detector_qreader:
                cached_yolo = []
                try:
                    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    decoded_texts, detections = detector_qreader.detect_and_decode(image=rgb_frame, return_detections=True)
                    for text, detection in zip(decoded_texts, detections):
                        if text:
                            bbox = list(map(int, detection['bbox_xyxy']))
                            cached_yolo.append((bbox, text))
                except: pass
            
            # 4. pyzbar
            if use_pyzbar and has_pyzbar:
                cached_pyzbar = []
                try:
                    value = decode(frame, symbols=[ZBarSymbol.QRCODE])
                    if value:
                        for qrcode in value:
                            rect = qrcode.rect
                            dec_inf = qrcode.data.decode("utf-8")
                            cached_pyzbar.append((rect, dec_inf))
                except: pass

        frame_counter += 1

        # =======================================================
        # 統計更新 (録画セグメント用)
        # =======================================================
        # キャッシュに中身があれば「検出された」とみなしてカウント
        rec_total_frames += 1
        if len(cached_wechat) > 0: rec_count_wechat += 1
        if len(cached_cv2) > 0: rec_count_cv2 += 1
        if len(cached_yolo) > 0: rec_count_yolo += 1
        if len(cached_pyzbar) > 0: rec_count_pyzbar += 1

        # =======================================================
        # 描画ロジック
        # =======================================================
        
        # WeChat (Green)
        for pt, info in cached_wechat:
            cv2.polylines(draw_frame, [pt], True, (0, 255, 0), 2)
            cv2.putText(draw_frame, f"WeChat: {info}", (pt[0][0], pt[0][1] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

        # CV2 (Red)
        for pt, info in cached_cv2:
            cv2.polylines(draw_frame, [pt], True, (0, 0, 255), 2)
            offset_y = 25 if args.mode in ['compare', 'compare_all'] else 10
            cv2.putText(draw_frame, f"CV2: {info}", (pt[0][0], pt[0][1] - offset_y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

        # YOLO (Blue)
        for bbox, info in cached_yolo:
            x1, y1, x2, y2 = bbox
            cv2.rectangle(draw_frame, (x1, y1), (x2, y2), (255, 0, 0), 2)
            cv2.putText(draw_frame, f"YOLO: {info}", (x1, y1 - 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 2)
            
        # pyzbar (Cyan)
        for rect, info in cached_pyzbar:
            x, y, w, h = rect
            cv2.rectangle(draw_frame, (x, y), (x + w, y + h), (255, 255, 0), 2)
            cv2.putText(draw_frame, f"pyzbar: {info}", (x, y - 55),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 2)

        # =======================================================
        # UI & 凡例 (統計情報付き)
        # =======================================================
        
        if args.mode == 'compare_all':
            x_legend = 20
            y_base = 130
            # 背景の半透明ボックス（読みやすくするため）
            # cv2.rectangle(draw_frame, (10, 110), (350, 240), (0,0,0), -1) 
            # ※OpenCVのみで透過は重いので、シンプルな黒ボックスにするか、そのまま描画します。
            # 今回は文字縁取り等はせず、視認性のため文字色を維持します。

            # 統計文字列の作成
            # 表示形式: "Label: (Detected / Total)"
            def get_stat_str(label, count, total):
                percent = (count / total * 100) if total > 0 else 0
                return f"{label}: ({count}/{total} - {percent:.1f}%)"

            str_wechat = get_stat_str("Green:  WeChat", rec_count_wechat, rec_total_frames)
            str_cv2    = get_stat_str("Red:    CV2 Std", rec_count_cv2, rec_total_frames)
            str_yolo   = get_stat_str("Blue:   YOLO", rec_count_yolo, rec_total_frames)
            str_pyzbar = get_stat_str("Cyan:   pyzbar", rec_count_pyzbar, rec_total_frames)

            cv2.putText(draw_frame, str_wechat, (x_legend, y_base), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            cv2.putText(draw_frame, str_cv2, (x_legend, y_base + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            cv2.putText(draw_frame, str_yolo, (x_legend, y_base + 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)
            cv2.putText(draw_frame, str_pyzbar, (x_legend, y_base + 75), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)

        # ヘッダーUI
        if is_video_mode:
            duration = total_frames / fps
            curr = current_pos / fps
            status = "PLAY" if is_playing else "PAUSE"
            cv2.rectangle(draw_frame, (0, 0), (draw_frame.shape[1], 100), (0, 0, 0), -1)
            cv2.putText(draw_frame, f"[{status}] {curr:.1f}s / {duration:.1f}s (Int: {args.interval})", (20, 30), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            cv2.putText(draw_frame, "[Spc]:Play [S]:Snap [R]:Save Segment [Esc]:Quit", (20, 60), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)

        # =======================================================
        # 書き込み & 表示
        # =======================================================
        if writer: writer.write(draw_frame)
        cv2.imshow(window_name, draw_frame)

        # =======================================================
        # 待機・キー処理
        # =======================================================
        t_end = cv2.getTickCount()
        process_time = (t_end - t_start) / cv2.getTickFrequency() * 1000
        
        base_wait = 1000 / fps
        wait = int(base_wait - process_time)
        
        if not is_video_mode: wait = 1
        else:
            if not is_playing: wait = 30
            elif wait < 1: wait = 1

        key = cv2.waitKey(wait) & 0xFF
        if key == 27 or key == ord('q'): break
        elif is_video_mode and key == 32: is_playing = not is_playing
        elif key == ord('s'):
            fname = f"snap_{datetime.datetime.now().strftime('%H%M%S')}.png"
            cv2.imwrite(fname, draw_frame)
            print(f"Snap: {fname}")
        elif key == ord('r'):
            if writer:
                # 1. 完了処理
                writer.release()
                writer = None
                sname = f"output/rec_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
                if os.path.exists(temp_video_name): os.rename(temp_video_name, sname)
                
                # 2. コンソールに最終結果を表示
                print(f"\n=== Saved: {sname} ===")
                print(f"Total Frames: {rec_total_frames}")
                print(f"WeChat: {rec_count_wechat} ({rec_count_wechat/rec_total_frames*100:.1f}%)")
                print(f"CV2:    {rec_count_cv2} ({rec_count_cv2/rec_total_frames*100:.1f}%)")
                print(f"YOLO:   {rec_count_yolo} ({rec_count_yolo/rec_total_frames*100:.1f}%)")
                print(f"pyzbar: {rec_count_pyzbar} ({rec_count_pyzbar/rec_total_frames*100:.1f}%)")
                print("========================\n")

                # 3. 画面通知
                cv2.putText(draw_frame, "VIDEO SAVED & STATS RESET", (50, 300), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0,255,255), 3)
                cv2.imshow(window_name, draw_frame)
                cv2.waitKey(800)

                # 4. 次のセグメントの準備 (カウンタリセット)
                writer = cv2.VideoWriter(temp_video_name, fourcc, fps, (width, height))
                rec_total_frames = 0
                rec_count_wechat = 0
                rec_count_cv2 = 0
                rec_count_yolo = 0
                rec_count_pyzbar = 0

        if is_video_mode and not is_playing:
            cap.set(cv2.CAP_PROP_POS_FRAMES, current_pos)

    if writer: writer.release()
    if os.path.exists(temp_video_name): os.remove(temp_video_name)
    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()