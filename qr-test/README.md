# GigE QR Viewer

`qr-test` は、ルートの `viewer.py` と同じ GigE カメラ入力に QR 認識を重ねるための追加モジュールです。

## ファイル構成

- `viewer_qr.py`: GigE QR 認識ビューワ
- `setup_qr.sh`: ルート `setup.sh` で作成済みの `.venv` に QR 依存だけを追加するスクリプト
- `opencv-obs/`: OpenCV 標準カメラや動画ファイルで試していた元の QR 実験スクリプト
- `setup.sh`: 旧 QR 実験用の standalone セットアップ。GigE の `viewer_qr.py` 用には通常使いません

## 想定環境

`viewer_qr.py` は GigE カメラを GStreamer 経由で開くため、先にリポジトリルートの `setup.sh` で `.venv` を作成してください。

```bash
cd /path/to/GigE-setup
./setup.sh --with-opencv-build
```

そのあと QR 用パッケージを既存 `.venv` に追加します。

```bash
./qr-test/setup_qr.sh
source .venv/bin/activate
```

`setup_qr.sh` は `opencv-python` / `opencv-contrib-python` をインストールしません。ルート環境の GStreamer 有効 OpenCV を上書きしないためです。

## 起動

```bash
python qr-test/viewer_qr.py --serial 08520932 --mode fhd
```

`--serial` を省略すると、起動時に camera serial と QR アルゴリズムの選択ウィンドウが表示されます。
serial は `tcam-gigetool list --format s` で見つかったものを優先し、見つからない場合は既知の serial 候補を表示します。

選択を固定したい場合:

```bash
python qr-test/viewer_qr.py --algorithm opencv --serial 08520932 --mode fhd
```

起動画面のキー操作:

- serial: `1` から `8`
- algorithm: `A` OpenCV, `S` WeChat, `D` pyzbar, `F` QReader, `G` all
- `Enter`: 開始
- `q` / `Esc`: キャンセル

補正付きで使う場合:

```bash
python qr-test/viewer_qr.py --algorithm opencv --rectify --serial 08520932 --mode fhd
python qr-test/viewer_qr.py --algorithm opencv --fisheye --serial 08520932 --mode fhd
```

## アルゴリズム

- `opencv`: OpenCV 標準 `QRCodeDetector`
- `wechat`: OpenCV WeChat QRCode。別途 `qr-test/opencv_3rdparty` にモデルファイルが必要
- `pyzbar`: `pyzbar` + OS 側の `zbar/libzbar`
- `qreader`: QReader/YOLO 系。`setup_qr.sh` の標準設定で追加されます
- `all`: 利用可能なアルゴリズムをまとめて実行

QReader/YOLO 系が不要な場合は軽量セットアップにできます。

```bash
./qr-test/setup_qr.sh --no-qreader
```

## キー操作

- `c`: 検出結果付き画像を `qr-test/capture` に保存
- `r`: 検出結果付き動画の録画開始
- `e`: 録画停止
- `q` / `Esc`: 終了

## WeChat QRCode モデル

`wechat` を使う場合は、以下のファイルを `qr-test/opencv_3rdparty/` に配置してください。

- `detect.prototxt`
- `detect.caffemodel`
- `sr.prototxt`
- `sr.caffemodel`

ファイルがない場合、起動時の選択画面では WeChat QRCode が無効になります。

## トラブルシュート

### `failed to open camera via GStreamer/tcamsrc`

ルート環境の OpenCV が GStreamer 有効版か確認してください。

```bash
source .venv/bin/activate
python - <<'PY'
import cv2
for line in cv2.getBuildInformation().splitlines():
    if "GStreamer" in line:
        print(line)
PY
```

`GStreamer: YES` でない場合は、ルートで `./setup.sh --with-opencv-build` を再実行してください。

### `pyzbar` が import できない

Python パッケージに加えて OS 側の zbar が必要です。Ubuntu では以下を実行します。

```bash
sudo apt install -y libzbar0
```

### QR セットアップ後に `cv2` が変になった

`opencv-python` / `opencv-contrib-python` を直接 pip install すると、GigE 用 OpenCV が上書きされることがあります。その場合は以下の順で作り直してください。

```bash
./setup.sh --with-opencv-build
./qr-test/setup_qr.sh
```
