import cv2
from qreader import QReader

# インスタンス生成 (モデルサイズは 'n', 's', 'm', 'l' から選べます。nが最速)
qreader = QReader(model_size="n")

cap = cv2.VideoCapture(0)

# cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
# cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

# 設定が反映されたか確認（カメラによっては指定した数値とズレることがあります）
print("Width:", cap.get(cv2.CAP_PROP_FRAME_WIDTH))
print("Height:", cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

while True:
    ret, frame = cap.read()
    if not ret:
        break

    # 画像をRGBに変換（QReaderはRGBを期待するため）
    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    # 検出とデコード (戻り値はテキストのタプル)
    decoded_text = qreader.detect_and_decode(image=rgb_frame)

    for text in decoded_text:
        if text:
            print(f"Detected: {text}")
            # 注: QReaderの標準メソッドだけでは座標取得が少し面倒ですが、
            # 単純な読み取りならこれが最強です。
            cv2.putText(
                frame,
                f"QR: {text}",
                (50, 50),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2,
            )

    cv2.imshow("QReader", frame)
    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

cap.release()
cv2.destroyAllWindows()
