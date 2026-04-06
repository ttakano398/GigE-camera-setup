# 仕様書: `continue_calib-gige.py` の B 方針実装

## 1. この文書の目的

この文書は、今後自分で `continue_calib-gige.py` を実装するための設計メモです。

主目的は次の 2 点です。

- `continue_calib-gige-opencv.py` のアルゴリズム資産を活かしつつ、`cv2.VideoCapture` の reopen 前提をやめる
- `setup.sh` / `README.md` / `viewer.py` および `*-opencv*` 系ファイルの使える部分を流用しながら、B 方針向けに整理し直す

本実装では、露光・ゲイン・ホワイトバランス更新時に pipeline を止めず、GStreamer を常時動かしたまま live 更新することを最優先にする。

---

## 2. 実装対象と位置づけ

### 2.1 主対象

- 新規実装: `continue_calib-gige.py`

### 2.2 参照しつつ必要に応じて作り直す対象

- `viewer.py`
- `setup.sh`
- `README.md`

### 2.3 参考実装として残す対象

- `continue_calib-gige-opencv.py`
- `viewer-opencv.py`
- `setup-opencv.sh`
- `README-opencv.md`

補足:

- 現在の repo では `*-opencv*` 側が新しめの参照元になっている
- 旧 `README.md` / `setup.sh` / `viewer.py` の内容は、基本的に `README-opencv.md` / `setup-opencv.sh` / `viewer-opencv.py` と同系統
- ドキュメントやスクリプトを再実装するときは、旧名と `-opencv` 名の両方を見比べて、良い部分だけ拾う前提でよい

---

## 3. repo 内の参照元と流用方針

### 3.1 `continue_calib-gige-opencv.py`

このファイルは A 方針の主参照元であり、特に次はそのまま設計の土台にできる。

- `CameraMode`, `CameraState` の考え方
- ArUco / AprilTag の互換層
- `update_roi_geometry()`
- `adjust_param()`
- `auto_calibrate_sep()`
- UI 文言
- キー操作体系
- `max` / `fhd` のモード定義
- ID セット切替の考え方

一方で、次は B 方針ではそのまま使わない。

- `cv2.VideoCapture(..., cv2.CAP_GSTREAMER)` による取り込み
- `open_capture()`
- `reopen_capture()`
- `cap.release()` → pipeline 再生成 → reopen 前提の制御
- 設定反映後に数フレーム捨てて安定化する前提

要するに、アルゴリズムと UI は流用対象、キャプチャ制御は再実装対象である。

### 3.1.1 外部参照: `260316_continue_calib.py`

元になった実装として、以下も重要な参照元とする。

- `/Users/takanotaisei/Documents/NI/Sushi/260316/260316_continue_calib.py`

このファイルから主に流用するもの:

- SEP ベースの状態遷移
- ROI 幾何と ID 切替の考え方
- `c` / `r` / `d` / `i` / `q` の操作感
- `bgr_hist` による平滑化
- `SKIP_FRAMES_AFTER_CMD` を使った、設定変更直後の落ち着き待ちの考え方

このファイルから持ち込まないもの:

- `v4l2-ctl` ベースの制御
- `/dev/videoX` 前提
- 実験用 / 調整用に広く露出していた CLI 引数群

方針としては、実装の流れはこの元ファイルを参考にしつつ、I/O 層だけを GigE + GStreamer + live property 更新に差し替える。

### 3.2 `viewer.py` / `viewer-opencv.py`

この 2 つは最小プレビューの参照元として使える。

流用しやすい要素:

- `--serial` の CLI 形
- `DEFAULT_SERIAL`
- `WINDOW_NAME`
- `imshow` / `waitKey` の最小ループ
- `appsink sync=false drop=true max-buffers=1` という方針

注意点:

- 既存 `viewer.py` / `viewer-opencv.py` は `tcamsrc` + `cv2.VideoCapture` の最小確認用途
- B 方針本体は `tcamsrc` + `gi.repository.Gst` + `TcamPropertyProvider` を前提とする
- したがって viewer は「接続確認用の簡易ツール」と割り切ってよく、本体の live property 制御方式をそのまま viewer に持ち込む必要はない

### 3.3 `setup.sh` / `setup-opencv.sh`

この 2 つは環境構築の骨格として再利用できる。

流用しやすい要素:

- `.venv` を repo 直下に作る方針
- OpenCV を GStreamer + GTK 対応でビルドする方針
- `CMAKE_ARGS="-DWITH_GSTREAMER=ON -DWITH_GTK=ON"`
- ビルド後に `cv2.getBuildInformation()` で確認する流れ

B 方針向けに追加確認したい要素:

- `gi` が import できること
- `from gi.repository import Gst` が通ること
- `from gi.repository import Tcam` が通ること
- `Gst.init(None)` が通ること
- `gst-inspect-1.0 tcamsrc` が通ること

### 3.4 `README.md` / `README-opencv.md`

この 2 つは運用手順とトラブルシュートの参照元として使える。

流用しやすい要素:

- NIC への一時 IP 設定手順
- `tcam-gigetool list` による発見手順
- 固定 IP 化の流れ
- `ping` での疎通確認
- OpenCV / GStreamer / GTK の確認方法
- プレビューまでの最短手順

README 再実装時の改善点:

- 実ファイル名を現在のものに合わせる
- A 方針と B 方針の違いを明記する
- `viewer.py` は接続確認用、`continue_calib-gige.py` は実運用向けであることを分けて説明する

---

## 4. B 方針の要約

### 4.1 目的

The Imaging Source の GigE カメラに対して、配信を継続したまま露光・ゲイン・ホワイトバランスなどのパラメータを更新し、自動キャリブレーションを行う。

### 4.2 A 方針との違い

A 方針:

- OpenCV `VideoCapture` で GStreamer pipeline を開く
- 設定変更のたびに `release()` / reopen

B 方針:

- Python から `gi.repository.Gst` を直接使う
- pipeline は常時起動
- `tcamsrc` の `TcamPropertyProvider` へ live 更新
- フレーム取得は `appsink`
- 画像処理と表示は OpenCV を継続利用してよい

### 4.3 非採用

- 設定変更ごとの pipeline 再生成
- `v4l2-ctl` ベースの制御
- `/dev/videoX` 前提の UVC 実装
- カメラ制御を OpenCV `VideoCapture` に持たせる設計

---

## 5. 想定環境

- Ubuntu 22.04
- Python 3.10 系
- TIS `tiscamera` 導入済み
- `tcamsrc` が使える
- GStreamer Python binding (`gi`, `Gst`) が使える
- OpenCV が GStreamer + GUI backend 付きで使える

実装前確認コマンドの例:

```bash
gst-inspect-1.0 tcamsrc
tcam-gigetool list
python - <<'PY'
import cv2
print(cv2.__version__)
for line in cv2.getBuildInformation().splitlines():
    if "GStreamer" in line or "GUI" in line or "GTK" in line:
        print(line)
PY
python - <<'PY'
import gi
gi.require_version("Gst", "1.0")
gi.require_version("Tcam", "1.0")
from gi.repository import Gst
Gst.init(None)
print("Gst init ok")
PY
```

---

## 6. 実装対象ファイルごとの方針

### 6.1 `continue_calib-gige.py`

本命の実装対象。

責務:

- カメラ接続
- appsink からのフレーム取得
- live property 更新
- marker 検出
- ROI 平均 BGR 抽出
- SEP ベース自動キャリブレーション
- 描画とキー入力

### 6.2 `viewer.py`

接続確認用の簡易ツールとして再実装してよい。

最低限:

- `--serial`
- カメラを開いて `imshow`
- `q` で終了

方針:

- まずは既存 `viewer.py` / `viewer-opencv.py` を土台にしてよい
- 目的は「環境が通っているかの確認」であり、自動キャリブレーションや live property 更新は不要

### 6.3 `setup.sh`

B 方針でも再利用価値が高い。

最低限:

- system 依存導入
- `.venv` 再作成
- OpenCV の GStreamer / GTK 対応確認
- `gi` / `Gst` 動作確認

必要なら:

- OpenCV ビルド手順は旧版と同様に維持
- 追加で PyGObject 関連チェックを入れる

### 6.4 `README.md`

README は B 方針用に書き直してよい。

含めるべき内容:

- NIC / IP / `tcam-gigetool` の準備
- `setup.sh` の役割
- `viewer.py` の使い方
- `continue_calib-gige.py` の使い方
- A 方針と B 方針の違い
- よくある詰まりどころ

---

## 7. `continue_calib-gige.py` の必須要件

### 7.1 コマンドライン引数

最小構成:

- `--serial`
- `--mode` (`max` / `fhd`)
- `--marker` (`aruco` / `apriltag`)

必要なら既存と同じデフォルトシリアルを持ってよいが、必ず上書き可能にする。

補足:

- 元ファイル `260316_continue_calib.py` にある `--device`, `--target-brightness`, `--threshold`, `--gain`, `--min-exposure`, `--max-exposure`, `--skip-frames`, `--smoothing`, `--h-ref`, `--dev` は、初版では CLI から外してよい
- これらは意味のある定数としてコード内へ寄せ、必要になった段階で再度引数化する

### 7.2 カメラモード

標準モード:

- `max`: `2592x1944 @ 22fps`
- `fhd`: `1920x1080 @ 30fps`

初期値は `fhd` を推奨する。

理由:

- live 調整中の応答性
- CPU / メモリ負荷
- 表示の扱いやすさ

### 7.3 フレーム取得

前提:

- `tcamsrc` を使う
- raw Bayer (`grbg`) を受ける
- `bayer2rgb` または同等処理を通す
- `videoconvert` を経て BGR にする
- `appsink` から Python 側へフレームを渡す

目標:

- Python 側では `numpy.ndarray (H, W, 3)` の `uint8` BGR として扱える

想定 pipeline イメージ:

```text
tcamsrc serial="<SERIAL>" type=aravis ! \
video/x-bayer,format=grbg,width=<W>,height=<H>,framerate=<FPS>/1 ! \
bayer2rgb ! videoconvert ! video/x-raw,format=BGR ! \
appsink sync=false drop=true max-buffers=1
```

### 7.4 live property 更新

更新対象:

- `ExposureAuto`
- `ExposureTime`
- `GainAuto`
- `Gain`
- `BalanceWhiteAuto`
- `BalanceWhiteRed`
- `BalanceWhiteGreen`
- `BalanceWhiteBlue`

要件:

- 露光 / ゲイン / WB 更新時に pipeline を止めない
- 更新失敗時はログを出し、直前値を維持する
- mode 切替時のみ pipeline 再構築を許容してよい
- frame ごとに無制限に書き込まず、設定変更後は短いクールダウンを入れる

補足:

- property 更新 API は `TcamPropertyProvider` の `set_tcam_enumeration()` / `set_tcam_float()` を優先して使う
- `tcam-properties` / `tcam-properties-json` は一括設定用の補助手段として扱う
- 自動キャリブレーションの第一段階は `ExposureTime` と `BalanceWhite{Red,Blue}` 中心でよい
- `Gain` は live 更新可能な設計にしておくが、初版で自動制御に必須ではない
- 初期化時および `r` リセット時の手動 WB 初期値は `BalanceWhiteRed=1.55`, `BalanceWhiteBlue=1.55` を採用する
- `BalanceWhiteGreen` は固定 `1.0` 運用でもよいが、カメラ都合で明示設定が必要なら state に持つ
- 更新頻度は元ファイルの `SKIP_FRAMES_AFTER_CMD=3` の意図を引き継ぎ、live 更新でも 1 回変更したら少なくとも数フレームは追加更新を抑制する
- 初版は「変更後 3 フレーム程度のクールダウン」または「約 100ms 程度の最小更新間隔」のどちらかを controller 側で持てばよい

### 7.5 検出対象

- デフォルトは ArUco `DICT_4X4_50`
- `--marker apriltag` 指定時は `DICT_APRILTAG_36h11`

OpenCV API 差分対応:

1. `cv2.aruco.ArucoDetector`
2. `cv2.aruco.detectMarkers`

現在の参照実装では 4.13 系を想定し、`ArucoDetector` 優先でよい。

### 7.6 自動キャリブレーション

既存 SEP ロジックを踏襲する。

基本方針:

- ROI 平均 BGR を使う
- G 成分を基準に露光を調整する
- 露光が収束したら `R/G`, `B/G` の差で WB を詰める

phase:

- `SEP_EXP_FINE`
- `SEP_WB_RED`
- `SEP_WB_BLUE`
- `SEP_DONE`

追加要件:

- `DONE` 後に閾値を外れたら該当 phase に戻る
- marker ロスト時は global brightness ベースの recovery を行う

### 7.7 UI / 操作

最低限:

- `c`: 自動キャリブレーション ON / OFF
- `r`: キャリブレーション状態と手動設定を初期値へ戻す
- `d`: ROI 方向切替
- `i`: 対象 ID セット切替
- `q`: 終了

追加で入れてよいもの:

- `p`: 現在設定出力
- `o`: ウィンドウを原寸へ戻す
- `1`: `max` mode
- `2`: `fhd` mode

### 7.8 表示

表示は OpenCV `imshow` でよい。

ただし:

- 表示のために pipeline を止めない

最低限のオーバーレイ:

- AUTO 状態
- marker 種別
- 対象 ID
- ROI 方向
- 露光値
- ROI BGR または目標 BGR
- 現在 step 数

---

## 8. 推奨内部構造

### 8.1 `GigECameraController`

責務:

- GStreamer 初期化
- pipeline 構築
- source / appsink 参照保持
- 最新フレームの取得
- live property 更新
- mode 切替
- camera state 保持

想定メソッド例:

- `start()`
- `stop()`
- `read_frame()`
- `set_exposure_us()`
- `set_gain_db()`
- `set_white_balance()`
- `apply_manual_state()`
- `switch_mode()`

### 8.2 `CalibrationEngine`

責務:

- marker 検出
- ROI 幾何更新
- ROI 平均 BGR 抽出
- SEP 制御
- recovery 制御

### 8.3 UI / main loop

責務:

- キー入力
- 描画
- controller と engine の接続
- ログ出力

---

## 9. データ構造

### 9.1 `CameraMode`

- `width`
- `height`
- `fps`

### 9.2 `CameraState`

- `serial`
- `mode`
- `exposure_auto`
- `gain_auto`
- `wb_auto`
- `exposure_us`
- `gain_db`
- `wb_red`
- `wb_green`
- `wb_blue`

初期手動値の推奨:

- `wb_red = 1.55`
- `wb_green = 1.0`
- `wb_blue = 1.55`

### 9.3 `CalibrationState`

- `calib_active`
- `sep_phase`
- `controller_gain`
- `prev_sign`
- `opt_steps`
- `bgr_hist`
- `current_direction_idx`
- `id_mode_idx`

---

## 10. ログ要件

起動時:

- Python executable
- `cv2.__version__`
- `cv2.aruco` 関連 attrs
- 使用シリアル
- 選択 mode
- marker 種別
- pipeline 概要
- 初期 camera state

更新時:

- exposure 変更
- gain 変更
- WB 変更
- phase 遷移
- marker loss recovery
- 例外詳細

---

## 11. 実装前に確認しておくべき点

- `ExposureAuto`, `GainAuto`, `BalanceWhiteAuto` の enum 値文字列が実機で想定通りか
- `ExposureTime`, `Gain`, `BalanceWhiteRed/Green/Blue` の型と範囲が実機で想定通りか
- appsink からのフレーム取り出しを pull 方式にするか callback 方式にするか

この章は未確定事項の洗い出しであり、実装中に確認できたら README かコードコメントへ反映する。

---

## 12. 実装順の推奨

1. `setup.sh` を整え、OpenCV / GStreamer / `gi` の確認が通る状態を作る
2. `viewer.py` を簡易プレビュー用として再実装し、接続確認を通す
3. `continue_calib-gige.py` で `GigECameraController` だけ先に作る
4. appsink から BGR `numpy.ndarray` を取れるところまで確認する
5. live property 更新の最小実験を行う
6. ArUco / AprilTag 検出を移植する
7. ROI / SEP ロジックを移植する
8. UI / キー入力 / オーバーレイを整える
9. `README.md` を新しい運用手順に合わせて更新する

---

## 13. 成功条件

以下を満たしたら実装完了とする。

- `continue_calib-gige.py --serial <SERIAL> --mode fhd --marker aruco` で起動できる
- 映像が継続表示される
- `c` で自動キャリブレーションを開始できる
- 露光 / WB 更新時に映像が止まらない
- marker を見せると ROI BGR に基づいて調整が進む
- `q` で終了できる
- 例外時にログから原因が追える

---

## 14. 補足メモ

- A 方針は「まず動くもの」として残す価値がある
- B 方針は「実運用向け」
- 表示系が OpenCV でも問題ない
- 問題の本質は描画ではなく、設定変更のたびに再接続しないこと
- `setup.sh` / `README.md` / `viewer.py` も、旧版を丸ごと踏襲するのではなく、B 方針に合わせて役割を整理して作り直す
