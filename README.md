# GigE Camera Setup and Calibration

このディレクトリには、The Imaging Source の GigE カメラを Ubuntu 上で扱うための最小セットを置いています。

- `setup.sh`: OpenCV を GStreamer + GTK 対応でビルドし、`.venv` を再作成するセットアップスクリプト
- `viewer.py`: 接続確認用の簡易ビューワ
- `continue_calib-gige.py`: `tcamsrc` + `TcamPropertyProvider` で live 更新しながら自動キャリブレーションする本命ツール
- `*-opencv*`: A 方針の参考実装

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
- OpenCV が GStreamer + GUI backend 付き
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

```bash
chmod +x setup.sh
./setup.sh
source .venv/bin/activate
```

`setup.sh` は `.venv` を `--system-site-packages` 付きで作るため、`python3-gi` / `python3-gst-1.0` を venv からも参照できます。

## 5. 接続確認

```bash
source .venv/bin/activate
python viewer.py --serial 08520932 --mode fhd
```

- `q` または `Esc` で終了

## 6. 自動キャリブレーション

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

## 7. 典型的な確認フロー

1. `tcam-gigetool list` でカメラが見えることを確認する
2. `viewer.py` で映像が出ることを確認する
3. `continue_calib-gige.py` を起動する
4. `c` で自動キャリブレーションを開始する

## 8. トラブルシュート

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

少なくとも `GStreamer: YES` と GUI backend が必要です。
