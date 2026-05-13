import cv2
import numpy as np

# -----------------------------------------------------------
# Init
# -----------------------------------------------------------
font = cv2.FONT_HERSHEY_SIMPLEX

# -----------------------------------------------------------
# 画像キャプチャ
# -----------------------------------------------------------
# VideoCaptureインスタンス生成
# 【修正点】Mac用に引数を変更 (Windows用のCAP_DSHOWを削除)
# 0は標準カメラ、1は外部カメラなどを指します
cap = cv2.VideoCapture(0)


# cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
# cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

# 設定が反映されたか確認（カメラによっては指定した数値とズレることがあります）
print("Width:", cap.get(cv2.CAP_PROP_FRAME_WIDTH))
print("Height:", cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

# QRCodeDetectorインスタンス生成
qrd = cv2.QRCodeDetector()

if not cap.isOpened():
    print("カメラを開けませんでした。接続や権限を確認してください。")
    exit()

while True:
    ret, frame = cap.read()

    if ret:
        # QRコードデコード
        # detectAndDecodeMultiは複数のQRコードを同時に検出可能です
        retval, decoded_info, points, straight_qrcode = qrd.detectAndDecodeMulti(frame)

        if retval:
            # 座標を整数型に変換
            points = points.astype(np.int32)

            # 検出された複数のQRコードをループ処理
            for dec_inf, point in zip(decoded_info, points):
                if dec_inf == "":
                    continue

                # デバッグ出力（ターミナルに内容を表示）
                print(f"Detected: {dec_inf}")

                # QRコード座標取得 (左上の点)
                x = point[0][0]
                y = point[0][1]

                # テキスト描画（QRコードの上に内容を表示）
                frame = cv2.putText(
                    frame, dec_inf, (x, y - 10), font, 0.5, (0, 0, 255), 2, cv2.LINE_AA
                )

                # バウンディングボックス描画（四角で囲む）
                # polylinesはリスト形式で座標を渡す必要があります
                frame = cv2.polylines(frame, [point], True, (0, 255, 0), 2, cv2.LINE_AA)

        # 画像表示
        cv2.imshow("QR Code Reader - Mac Debug", frame)

    # 'q'キーで終了
    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

# キャプチャリソースリリース
cap.release()
cv2.destroyAllWindows()  # ウィンドウを閉じる処理を追加
