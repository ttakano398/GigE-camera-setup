# GigE Camera Setup and Viewer

このディレクトリには、The Imaging Source の GigE カメラを Ubuntu 上で扱うための最小セットを置いています。

* `setup.sh`: OpenCV を GStreamer + GTK 対応でビルドし、`.venv` にインストールするセットアップスクリプト
* `viewer.py`: GStreamer (`tcamsrc`) 経由で GigE カメラを OpenCV から開いて `imshow` するビューワ

## パイプライン概要

このREADMEでは、`tcamsrc` を含む GStreamer パイプラインを OpenCV の `cv2.VideoCapture(..., cv2.CAP_GSTREAMER)` に渡して映像を取得します。  
パイプラインは `tcamsrc -> video/x-bayer -> bayer2rgb -> videoconvert -> video/x-raw(BGR) -> appsink(OpenCV)` の順です。

対象環境の想定:

* Ubuntu 22.04
* The Imaging Source GigE カメラ
* TIS `tiscamera` / `tcam-gigetool` 導入済み、または導入可能

---

## 1. カメラ接続と認識までの流れ

### 1.1 PoE ボード / 電源供給ボードを接続

対象カメラは LAN ケーブルだけでは起動しない構成があるため、まず電源供給ボードを接続します。

今回の流れでは、これによりカメラが物理リンク可能な状態になりました。

---

### 1.2 カメラ専用 NIC に一時 IP を割り当てる

複数の NIC をカメラ専用として運用する想定です。

例:

* `enp134s0 -> 192.168.14.1/24`
* `enp135s0 -> 192.168.15.1/24`
* `enp136s0 -> 192.168.16.1/24`
* `enp137s0 -> 192.168.17.1/24`

一時的に IP を割り当てる例:

```bash
sudo ip addr add 192.168.17.1/24 dev enp134s0
ip addr show dev enp134s0
```

不要になったら消せます。

```bash
sudo ip addr flush dev enp134s0
```

---

### 1.3 TIS の `tiscamera` をインストール

TIS の配布ページから `tiscamera` を取得します。

配布ページ:

```text
https://dl-gui.theimagingsource.com/en_US/10a51957-9b67-5dc2-a139-b456dc946869/
```

インストール例:

```bash
sudo apt install ./tiscamera_1.1.1.4142_amd64_ubuntu_1804.deb
```

導入確認:

```bash
which tcam-gigetool
tcam-gigetool list
```

---

### 1.4 カメラ探索のために link-local 帯へ一時設定

カメラの現在 IP が不明な場合、まず NIC を link-local 帯にして探索します。

```bash
sudo ip addr flush dev enp134s0
sudo ip addr add 169.254.100.1/16 dev enp134s0
ip addr show dev enp134s0
```

`rp_filter` を無効化します。

```bash
sudo sysctl -w net.ipv4.conf.enp134s0.rp_filter=0
sudo sysctl -w net.ipv4.conf.all.rp_filter=0
sudo sysctl -w net.ipv4.conf.default.rp_filter=0
```

その後、カメラを列挙します。

```bash
tcam-gigetool list
```

今回の例では、ここでカメラの現在 IP が `192.168.30.59` と分かりました。

---

### 1.5 一時的に PC 側 NIC をカメラと同一セグメントへ合わせる

カメラが `192.168.30.59` にいることが分かったら、PC 側も同じ帯に一時的に合わせます。

```bash
sudo ip addr flush dev enp134s0
sudo ip addr add 192.168.30.1/24 dev enp134s0
ping -I 192.168.30.1 -c 4 192.168.30.59
```

これで ping が通れば、少なくとも以下が確認できます。

* 物理接続 OK
* 電源供給 OK
* カメラ認識 OK
* IP 通信 OK

---

## 2. Python / OpenCV 側セットアップ

このディレクトリには `setup.sh` があり、以下をまとめて実行します。

* system 依存パッケージ導入
* `.venv` 再作成
* `opencv-python` の clone
* `WITH_GSTREAMER=ON -DWITH_GTK=ON` で wheel ビルド
* `.venv` へのインストール
* ソースツリー退避
* GStreamer / GTK 対応確認

### 実行

```bash
chmod +x setup.sh
./setup.sh
```

どのディレクトリから実行しても、`setup.sh` 自身が置かれたディレクトリを基準に動きます。

つまり、このディレクトリに `.venv` が作られます。

---

## 3. `viewer.py` の使い方

`viewer.py` は、TIS GigE カメラを GStreamer (`tcamsrc`) 経由で OpenCV から開き、`imshow` します。

### 実行

```bash
source .venv/bin/activate
python viewer.py
```

または、どこからでも絶対パスで呼び出せます。

```bash
source /home/ai-sushi-module/camera-test/.venv/bin/activate
python /home/ai-sushi-module/camera-test/viewer.py
```

### serial 指定

シリアル番号を明示する場合:

```bash
python viewer.py --serial 08520932
```

デフォルトでは `08520932` を使うようになっています。

---

## 4. トラブルシュート

### 4.1 `tcam-gigetool list` で出ない

* 電源供給ボードの接続を再確認
* NIC の物理リンクを確認
* `169.254.100.1/16` の一時設定を再確認
* `rp_filter=0` を再確認

### 4.2 `viewer.py` で `failed to open camera`

* `tcam-gigetool list` でカメラが見えているか確認
* `tcamsrc` が使えるか確認

```bash
gst-inspect-1.0 tcamsrc
```

* カメラの IP と NIC 側の一時 IP を再確認

### 4.3 `imshow` が使えない

`setup.sh` により GTK 対応付きで OpenCV をビルドする前提です。

ビルド確認:

```bash
python - <<'PY'
import cv2
for line in cv2.getBuildInformation().splitlines():
    if 'GStreamer' in line or 'GUI' in line or 'GTK' in line:
        print(line)
PY
```

期待するのは少なくとも:

* `GStreamer: YES`
* `GUI: ...`
* `GTK+: YES` または相当の GUI backend

---

## 5. 典型的な運用の流れ

1. 電源供給ボードを接続してカメラを起動する
2. `tcam-gigetool list` で現在 IP を見つける
3. NIC 側を一時的に同じセグメントへ合わせる
4. `ping` で疎通確認する
5. `setup.sh` で Python / OpenCV 環境を作る
6. `viewer.py` でプレビューする

---

## 6. カメラ側 IP を固定運用にする手順

一時運用では、毎回以下のように

* `tcam-gigetool list` で現在 IP を調べる
* PC 側 NIC をそのセグメントへ一時的に合わせる

という流れになります。

```bash
sudo ip addr flush dev enp134s0
sudo ip addr add 192.168.30.1/24 dev enp134s0
ping -I 192.168.30.1 -c 4 192.168.30.59
```

これを繰り返したくない場合は、**カメラ側 IP を固定化**します。

### 6.1 固定運用の考え方

例として `enp134s0` をカメラ専用 NIC とし、次のように固定します。

* PC 側: `192.168.14.1/24`
* カメラ側: `192.168.14.2/24`
* gateway: `0.0.0.0`

このようにすると、以後は毎回 `192.168.14.x` 帯で接続できます。

---

### 6.2 事前準備

まず、PC 側 NIC を一時的にカメラの現在セグメントへ合わせ、`tcam-gigetool list` で認識できる状態にしておきます。

今回の例では、カメラの現在 IP は `192.168.30.59` でした。

```bash
sudo ip addr flush dev enp134s0
sudo ip addr add 192.168.30.1/24 dev enp134s0
ping -I 192.168.30.1 -c 4 192.168.30.59
```

さらに、カメラのシリアル番号を確認します。

```bash
tcam-gigetool list
```

例:

* Serial Number: `08520932`

---

### 6.3 カメラ側 IP を固定化

以下のように、`tcam-gigetool set` でカメラ側を static 設定にします。

```bash
tcam-gigetool set --ip 192.168.14.2 --netmask 255.255.255.0 --gateway 0.0.0.0 --mode static 08520932
```

ここでの意味は以下です。

* `--ip 192.168.14.2`: カメラの固定 IP
* `--netmask 255.255.255.0`: /24
* `--gateway 0.0.0.0`: 直結前提なので gateway は不要
* `--mode static`: 一時ではなく固定設定
* `08520932`: カメラのシリアル番号

---

### 6.4 PC 側 NIC を固定帯へ戻して確認

カメラ設定後、PC 側も固定運用帯へ戻します。

```bash
sudo ip addr flush dev enp134s0
sudo ip addr add 192.168.14.1/24 dev enp134s0
ping -I 192.168.14.1 -c 4 192.168.14.2
```

これで疎通すれば、固定運用へ移行できています。

必要なら再度:

```bash
tcam-gigetool list
```

を実行して、カメラの Current IP が `192.168.14.2` になっていることを確認します。

---

### 6.5 どの工程をどう変えるか

一時運用では README 前半の以下の工程を毎回行っていました。

1. link-local 帯 (`169.254.100.1/16`) へ一時設定
2. `tcam-gigetool list` で現在 IP を調べる
3. その都度、PC 側 NIC をカメラの現在セグメントへ合わせる

固定運用に切り替えた後は、**毎回の 1〜3 を省略**できます。

つまり、今後は以下で十分です。

```bash
sudo ip addr flush dev enp134s0
sudo ip addr add 192.168.14.1/24 dev enp134s0
ping -I 192.168.14.1 -c 4 192.168.14.2
```

その後すぐに `viewer.py` を実行できます。

```bash
source .venv/bin/activate
python viewer.py --serial 08520932
```

---

### 6.6 複数 NIC での固定運用例

カメラ専用 NIC 群を次のように割り当てると管理しやすいです。

* `enp134s0 -> 192.168.14.1/24`
* `enp135s0 -> 192.168.15.1/24`
* `enp136s0 -> 192.168.16.1/24`
* `enp137s0 -> 192.168.17.1/24`

各カメラ側も、それぞれ対応する `.2` などへ固定化すると分かりやすいです。

例:

* `enp134s0` に接続するカメラ -> `192.168.14.2`
* `enp135s0` に接続するカメラ -> `192.168.15.2`

---

## 7. 補足

* NIC の一時 IP 設定は再起動で消えることがあります
* カメラ専用 NIC 群を用意しておくと運用しやすいです
* 将来的にはカメラ側 IP を固定運用にする方が管理しやすいです
