# ChArUco Calibration Pipeline

`viewer.py` で保存したキャプチャ画像から ChArUco ボードを検出し、`viewer.py --rectify` がそのまま読める `camera_calib.yaml` を作る手順です。

## 1. ボード設定

まず [calib_charuco.py](calib_charuco.py) の先頭にある `User-configurable board parameters` を実ボードに合わせて編集します。

- `SQUARES_X`, `SQUARES_Y`: チェス盤のマス数。内角点数ではありません。
- `SQUARE_LENGTH`: 1 マスの実寸。
- `MARKER_LENGTH`: マス内 ArUco マーカーの実寸。`SQUARE_LENGTH` より小さくします。
- `ARUCO_DICT_NAME`: 印刷したボードの辞書。例: `DICT_4X4_50`。

単位は `SQUARE_LENGTH` と `MARKER_LENGTH` で揃っていればよいですが、メートル推奨です。

## 2. キャプチャ

キャリブレーション用画像は未補正の RAW 表示から保存します。`--rectify` や `--fisheye` は付けずに起動してください。

```bash
cd /Users/takanotaisei/Documents/NI/GigE-setup
source .venv/bin/activate
python viewer.py --serial 08520932 --mode max
```

`c` キーで `camera_rectify/capture/` に PNG が保存されます。20-40 枚程度を目安に、画面の中央・四隅・斜め・距離違いを混ぜます。

## 3. キャリブレーション

```bash
python camera_rectify/calib_charuco.py --save-vis
```

出力:

- `camera_rectify/camera_calib.yaml`: `viewer.py --rectify` 用
- `camera_rectify/camera_calib.npz`: NumPy 用
- `camera_rectify/charuco_detected/`: 検出確認画像

ボード画像を現在の設定で書き出したい場合:

```bash
python camera_rectify/calib_charuco.py \
  --export-board-png camera_rectify/charuco_board.png \
  --only-export-board
```

## 4. Rectify 表示

```bash
python viewer.py \
  --serial 08520932 \
  --mode max \
  --rectify \
  --rectify-calib camera_rectify/camera_calib.yaml
```

`viewer.py` 側は YAML 内の `image_width` / `image_height` と現在の表示解像度が違う場合、内部パラメータ `K` を解像度比でスケールします。精度確認は、できるだけキャリブレーション時と同じ `--mode` で行ってください。
