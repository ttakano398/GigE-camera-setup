# GigE Camera Setup and Calibration

このディレクトリには、The Imaging Source の GigE カメラを Ubuntu 上で扱うための最小セットを置いています。

- `setup.sh`: `.venv` を再作成し、本命 `continue_calib-gige.py` 向け依存を入れるセットアップスクリプト（`--with-opencv-build` 指定時のみ OpenCV を GStreamer + GTK 付きでビルド）
- `viewer.py`: 接続確認用の簡易ビューワ
- `qr-test/viewer_qr.py`: GigE 映像に QR 認識結果を重ねて表示するビューワ
- `qr-test/setup_qr.sh`: 既存 `.venv` に QR 用 Python パッケージだけを追加するスクリプト
- `continue_calib-gige.py`: `tcamsrc` + `TcamPropertyProvider` で live 更新しながら自動キャリブレーションする本命ツール
- `*-opencv*`: A 方針の参考実装

## パイプライン概要

このREADMEでは、本命の `continue_calib-gige.py` で OpenCV `VideoCapture` を使わず、Python から GStreamer を直接組んで `tcamsrc` の出力を `appsink` で受け取ります。  
パイプラインは `tcamsrc(type=aravis) -> video/x-bayer -> bayer2rgb -> videoconvert -> video/x-raw(BGR) -> appsink` の順で、露光/ゲイン/WB は `TcamPropertyProvider` で配信中に更新します。

## 1. 方針の違い

### A 方針

- OpenCV `VideoCapture` で GStreamer pipeline を開く
- 設定変更時に pipeline を作り直す

### B 方針

- `tcamsrc` を GStreamer から直接使う
- `appsink` でフレームを受ける
- `TcamPropertyProvider` の `set_tcam_*()` で live 更新する
- 露光 / ゲイン / WB 更新時に配信を止めない

`continue_calib-gige.py` は B 方針です。

## 2. 想定環境

- Ubuntu 22.04
- TIS `tiscamera` 導入済み
- `tcamsrc` が使用可能
- Python 3.10 系
- OpenCV が GUI backend 付き（`continue_calib-gige.py` 用）
- OpenCV の GStreamer backend は `viewer.py` を使う場合のみ必要
- `python3-gi` / `python3-gst-1.0` が利用可能

今回のカメラ前提:

- model: `DFM47GR0521-ML`
- Bayer: `grbg`
- `BalanceWhiteGreen`: writable

## 3. 事前確認

```bash
tcam-ctrl --version
tcam-ctrl --packages
gst-inspect-1.0 tcamsrc
tcam-gigetool list
tcam-ctrl -p 08520932
```

## 4. セットアップ

まず GigE カメラ用の基本環境を作ります。`viewer.py` / `viewer_qr.py` は OpenCV の GStreamer backend が必要なので、通常は OpenCV ビルド込みでセットアップしてください。

```bash
chmod +x setup.sh
./setup.sh
source .venv/bin/activate
```

Ansible で `gige_setup.sh` + `setup.sh` 相当をまとめて実行する場合:

```bash
cd /home/ai-sushi-module/camera-test/tcamctrl/GigE-camera-setup
ANSIBLE_LOCAL_TEMP=/tmp/.ansible-local ANSIBLE_REMOTE_TEMP=/tmp/.ansible-remote \
ansible-playbook ansible-gige-setup.yml -K
```

`ansible-gige-setup.yml` は既定で OpenCV ソースビルド（GStreamer + GTK）を有効化しています。

`setup.sh` は `.venv` を `--system-site-packages` 付きで作るため、`python3-gi` / `python3-gst-1.0` を venv からも参照できます。

`viewer.py` / `qr-test/viewer_qr.py` を使う場合は OpenCV をビルドしてください:

```bash
./setup.sh --with-opencv-build
```

## 5. 接続確認

```bash
source .venv/bin/activate
python viewer.py --serial 08520932 --mode fhd
```

- `q` または `Esc` で終了

## 6. QR ビューワ

QR 認識も使う場合は、ルートの `setup.sh` で作った既存 `.venv` に追加パッケージを入れます。`qr-test/setup_qr.sh` は OpenCV を入れ直さないため、GStreamer 有効版の `cv2` を維持します。

```bash
./qr-test/setup_qr.sh
./qr-test/install_opencv_3rdparty.sh  # WeChat QRCode を使う場合のみ
source .venv/bin/activate
python qr-test/viewer_qr.py --serial 08520932 --mode fhd
```

起動時に camera serial と QR アルゴリズムの選択ウィンドウが出ます。選択を固定したい場合:

```bash
python qr-test/viewer_qr.py --algorithm opencv --serial 08520932 --mode fhd
```

詳細は `qr-test/README.md` を参照してください。

## 7. 自動キャリブレーション

```bash
source .venv/bin/activate
python continue_calib-gige.py --serial 08520932 --mode fhd --marker aruco
```

### キー操作

- `c`: 自動キャリブレーション ON / OFF
- `r`: 手動設定とキャリブレーション状態を初期化
- `d`: ROI 方向切替
- `i`: 対象 ID セット切替
- `1`: `max` mode
- `2`: `fhd` mode
- `p`: 現在設定をログ出力
- `o`: ウィンドウを原寸へ戻す
- `q`: 終了

### 初期手動値

- `ExposureTime = 15000 us`
- `Gain = 0.0 dB`
- `BalanceWhiteRed = 1.55`
- `BalanceWhiteGreen = 1.0`
- `BalanceWhiteBlue = 1.55`

## 8. 典型的な確認フロー

1. `tcam-gigetool list` でカメラが見えることを確認する
2. `viewer.py` で映像が出ることを確認する
3. QR が必要な場合は `qr-test/setup_qr.sh` を実行し、`qr-test/viewer_qr.py` を起動する
4. `continue_calib-gige.py` を起動する
5. `c` で自動キャリブレーションを開始する

## 9. トラブルシュート

### `failed to open camera via GStreamer/tcamsrc`

- `tcam-gigetool list` でカメラが見えているか確認
- `gst-inspect-1.0 tcamsrc` が通るか確認
- NIC とカメラの IP セグメントが一致しているか確認

### `gi` / `Gst` / `Tcam` の import が失敗する

- `python3-gi`, `python3-gst-1.0` が入っているか確認
- `.venv` を作り直して `source .venv/bin/activate` し直す
- `python - <<'PY'` で `gi.require_version("Gst", "1.0")`, `gi.require_version("Tcam", "1.0")` を確認する

### `imshow` が使えない

```bash
python - <<'PY'
import cv2
for line in cv2.getBuildInformation().splitlines():
    if "GStreamer" in line or "GUI" in line or "GTK" in line:
        print(line)
PY
```

`continue_calib-gige.py` では GUI backend が必要です。  
`viewer.py` では `GStreamer: YES` も必要です（必要なら `./setup.sh --with-opencv-build` を実行）。

### QR セットアップ後に GigE viewer が開けない

- `qr-test/setup_qr.sh` を使い、`pip install opencv-python` / `pip install opencv-contrib-python` を直接実行しない
- `python -c "import cv2; print(cv2.getBuildInformation())"` で `GStreamer: YES` が残っているか確認
- OpenCV が上書きされた場合は、ルートで `./setup.sh --with-opencv-build` を再実行してから `./qr-test/setup_qr.sh` を実行する
