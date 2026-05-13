import cv2
import numpy as np
import sys
import argparse
import os
import datetime

def main():
    # -----------------------------------------------------------
    # 引数解析 (--video PATH)
    # -----------------------------------------------------------
    parser = argparse.ArgumentParser(description='WeChat QRCode Scanner with Video Player')
    parser.add_argument('--video', type=str, help='Path to video file', default=None)
    args = parser.parse_args()

    # -----------------------------------------------------------
    # WeChat QRCode Scanner 設定
    # -----------------------------------------------------------
    # パス設定（ユーザー様の環境に合わせています）
    model_dir = "opencv_3rdparty"
    detect_proto = os.path.join(model_dir, "detect.prototxt")
    detect_model = os.path.join(model_dir, "detect.caffemodel")
    sr_proto = os.path.join(model_dir, "sr.prototxt")
    sr_model = os.path.join(model_dir, "sr.caffemodel")

    # ファイル存在確認（簡易）
    if not os.path.exists(detect_proto):
        print(f"Warning: モデルファイルが見つかりません ({model_dir})。パスを確認してください。")

    try:
        detector = cv2.wechat_qrcode_WeChatQRCode(
            detect_proto, detect_model, sr_proto, sr_model
        )
    except Exception as e:
        print("モデルの読み込みに失敗しました。")
        print(f"Error: {e}")
        sys.exit()

    # -----------------------------------------------------------
    # 入力ソースの切り替え
    # -----------------------------------------------------------
    is_video_mode = args.video is not None
    
    if is_video_mode:
        if not os.path.exists(args.video):
            print(f"Error: 動画ファイルが見つかりません: {args.video}")
            return
        cap = cv2.VideoCapture(args.video)
        window_name = "WeChat QRCode - Video Mode"
    else:
        cap = cv2.VideoCapture(0)
        window_name = "WeChat QRCode - Camera Mode"

    if not cap.isOpened():
        print("Error: 映像ソースを開けませんでした。")
        return

    # 動画情報の取得
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if is_video_mode else 0
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps == 0: fps = 30.0

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    # -----------------------------------------------------------
    # 【機能1】開始位置選択モード (動画モードのみ)
    # -----------------------------------------------------------
    start_frame_pos = 0

    if is_video_mode:
        print("=== 開始位置を選択して Enter キーを押してください ===")
        
        def on_seek_setup(val):
            nonlocal start_frame_pos
            start_frame_pos = val
            cap.set(cv2.CAP_PROP_POS_FRAMES, val)
        
        cv2.createTrackbar("Seek", window_name, 0, total_frames, on_seek_setup)

        while True:
            # トラックバー操作で cap.set されている前提で read
            ret, frame = cap.read()
            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            
            # ガイド表示
            display_frame = frame.copy()
            cv2.rectangle(display_frame, (0, 0), (display_frame.shape[1], 80), (0, 0, 0), -1)
            cv2.putText(display_frame, "STEP 1: Select Start Position", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.putText(display_frame, "[Enter]: Start Detection", (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            
            cv2.imshow(window_name, display_frame)
            
            # トラックバー操作の反応を良くするため、キー待ち時間を短く設定
            # Enter(13)で決定
            key = cv2.waitKey(30) & 0xFF
            if key == 13: 
                break
            elif key == 27: # Esc
                cap.release()
                cv2.destroyAllWindows()
                return

    # -----------------------------------------------------------
    # 【機能2】推論 & 再生ループ
    # -----------------------------------------------------------
    is_playing = True # 再生フラグ
    
    # メインループ用のシークバーコールバック
    def on_seek_main(val):
        cap.set(cv2.CAP_PROP_POS_FRAMES, val)

    # 動画モードならシークバーを再設定（コールバックを差し替え）
    if is_video_mode:
        cv2.createTrackbar("Seek", window_name, start_frame_pos, total_frames, on_seek_main)

    while True:
        # 現在位置取得
        current_pos = int(cap.get(cv2.CAP_PROP_POS_FRAMES)) if is_video_mode else 0

        # 動画モードかつ再生中のみトラックバー更新
        if is_video_mode and is_playing:
             cv2.setTrackbarPos("Seek", window_name, current_pos)

        ret, frame = cap.read()
        if not ret:
            if is_video_mode:
                # 動画終了時はループさせるか、停止させる
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                is_playing = False
                continue
            else:
                break

        # --- QRコード検出処理 (元の機能) ---
        try:
            res, points = detector.detectAndDecode(frame)
            for i, info in enumerate(res):
                # 描画
                pt = points[i].astype(int)
                cv2.polylines(frame, [pt], True, (0, 255, 0), 2)
                cv2.putText(frame, info, (pt[0][0], pt[0][1] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
                print(f"Detected: {info}")
        except Exception as e:
            pass

        # --- UI描画 (動画モード時のみリッチなUIを表示) ---
        display_frame = frame.copy()
        
        if is_video_mode:
            # 時間計算
            duration_sec = total_frames / fps
            current_sec = current_pos / fps
            
            # ステータス表示エリア
            h, w, _ = display_frame.shape
            cv2.rectangle(display_frame, (0, 0), (w, 100), (0, 0, 0), -1)
            
            status_text = "PLAYING" if is_playing else "PAUSED"
            time_text = f"{current_sec:.1f}s / {duration_sec:.1f}s"
            
            cv2.putText(display_frame, f"[{status_text}] Time: {time_text}", (20, 30), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            
            # キーガイド
            guide = "[Space]: Play/Pause  [S]: Save PNG  [Esc]: Quit"
            cv2.putText(display_frame, guide, (20, 70), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

        cv2.imshow(window_name, display_frame)

        # --- キー入力処理 ---
        wait_time = int(1000 / fps) if (is_video_mode and is_playing) else 30
        if not is_video_mode: wait_time = 1 # カメラモードは最速で

        key = cv2.waitKey(wait_time) & 0xFF

        if key == 27 or key == ord('q'): # Esc or q
            break
        
        elif is_video_mode and key == 32: # Space (再生/停止)
            is_playing = not is_playing
            
        elif key == ord('s'): # S (保存)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"qr_result_{timestamp}.png"
            cv2.imwrite(filename, frame) # 検出ボックスが描画された画像を保存
            print(f"Saved: {filename}")
            
            # 保存完了エフェクト（簡易）
            cv2.putText(display_frame, "SAVED!", (50, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 255), 3)
            cv2.imshow(window_name, display_frame)
            cv2.waitKey(200)

        # 一時停止中の処理 (動画モードのみ)
        if is_video_mode and not is_playing:
            # read()で1コマ進んでしまうのを戻して、静止画のように見せる
            cap.set(cv2.CAP_PROP_POS_FRAMES, current_pos)

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()