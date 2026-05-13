import cv2


def check_camera_connection():
    print("カメラIDを探索中...\n")

    # 0番から9番までチェック
    for index in range(10):
        cap = cv2.VideoCapture(index)

        if cap.isOpened():
            # カメラが開けた場合、解像度を取得してみる
            width = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
            height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)

            print(f"✅ ID {index}: 接続成功 (解像度: {int(width)}x{int(height)})")

            # 念のため1フレーム読んでみる
            ret, frame = cap.read()
            if ret:
                print(f"   -> 映像取得OK")
            else:
                print(f"   -> 接続はできたが映像が取れません")

            cap.release()
        else:
            print(f"❌ ID {index}: 接続なし")

    print("\n探索終了")


if __name__ == "__main__":
    check_camera_connection()
