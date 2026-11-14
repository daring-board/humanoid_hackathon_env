# 認識（担当B）引き継ぎメモ

最終更新: 2026-11-14（Day1 午後）

箱のQRの読取、区画マーカーの認識、ダッシュボードへの送信、制御（担当A）への指示の受け渡しまで、PC内蔵カメラと合成画像で動いています。R1 の頭部カメラからの取得（`videohub`）は、実機で画像を保存できるところまで確認できました。残りは、実機のカメラでの読取成功率の計測と、移動を伴う通し確認です。次に実機を使える人は「次にやること」の1から進めてください。

## いま動くもの・未確認のもの

| 項目 | 状態 | 確認方法 |
|---|---|---|
| QRの読取と分類先の判定 | 動作確認済み | `scripts/test_recognition.py` が 29/29 成功 |
| receiver への送信（`scanned` / `error`） | 動作確認済み | ローカルの receiver が 201 を返し、`logs/events.jsonl` に保存された |
| 区画マーカー（`ZONE:A`）の認識と位置の算出 | 動作確認済み | 合成画像で左右のずれと大きさが期待どおり |
| 制御（担当A）への指示（`go_to` / `target`） | 動作確認済み（受け側は見本） | `control_example.py --listen` が受信した |
| 担当A の実コードとの結合 | **未着手** | A のスクリプトが main にまだ無い |
| PC内蔵カメラからの取得（`webcam`） | 動作確認済み | 640x480、約15fps |
| R1 頭部カメラからの取得（`videohub`） | 実機で確認済み | `camera-check --source videohub` で画像を保存できた（解像度と fps の記録は未取得） |
| Gemini による見た目の照合（`--gemini`） | 偽サーバーで動作確認済み、**実際の API は未確認** | 下の「Gemini による見た目の照合」 |
| 認識と移動の結合（`pick_move.py`） | dry-run で動作確認済み、**実機は未確認** | 下の「次にやること」2 |
| R1 の UDP 配信の受信（`udp`） | 未確認（予備） | `videohub` で取れたので当面は不要 |
| pixi 環境での実行 | **未確認** | 確認はシステムの Python 3.12 / OpenCV 5.0 で行った |

## 構成

```
R1 頭部カメラ ─(有線LAN・DDS)─▶ camera_source.py ─▶ qr_reader.py ─▶ events.py ─POST /events─▶ receiver (:8000)
                                  取得元を切替        読取と確定       log_schema.md の形式
                                                          │
                                                          └─▶ recognition_link.py ─(import または POST /command)─▶ 制御（担当A）
```

| ファイル | 役割 |
|---|---|
| `scripts/camera_source.py` | カメラの取得元。`webcam` / `file` / `videohub` / `udp` を `--source` で切り替える |
| `scripts/qr_reader.py` | QRの読取。同じ内容が3フレーム続いたら確定し、5秒間は同じ内容を再送しない |
| `scripts/events.py` | イベントの生成と POST。形式は [log_schema.md](log_schema.md) |
| `scripts/recognition_link.py` | `Recognizer` クラス。認識をバックグラウンドで回し、箱の確定と区画マーカーの位置を A に渡す |
| `scripts/commands.py` | A への指示（`go_to` / `target`）の生成と POST |
| `scripts/control_example.py` | A 向けの見本。ロボットは動かさず、判断結果を表示するだけ |
| `scripts/recognize.py` | `Recognizer` を使う CLI |
| `scripts/qr_survey.py` | 読取成功率の計測。結果を `work/qr_survey.csv` に追記する |
| `scripts/pick_move.py` | 認識と移動の結合。箱の QR が確定したら区画マーカーへ向かう。既定はロボットを動かさない |
| `scripts/gemini_verify.py` | Gemini による見た目の照合。単体でも試せる |
| `scripts/catalog.json` | 箱ごとの「期待する見た目」の台帳（見本） |
| `scripts/r1_clients.py` | R1 用の `R1LocoClient`（スキル同梱のものをコピー） |
| `scripts/camera_check.py` | 取得元の診断。解像度・fps・成功率を表示し、1枚を `work/camera_check.jpg` に保存する |
| `scripts/make_qr.py` | テスト用QRの生成（`work/qr/` に出力） |
| `scripts/test_recognition.py` | 実機なしの自己テスト |

`pixi.toml` の `[tasks]` に `recognize` / `camera-check` / `make-qr` / `test-recognition` / `control-example` / `qr-survey` / `pick-move` / `gemini-verify` を追加しました。依存パッケージは変えていません。

## R1 のカメラ映像の取り方

接続は README「3. ロボットとの接続方法」のとおりです。PC の有線LANに `192.168.123.164` を手動で設定し、R1（`192.168.123.161`）と LAN ケーブルで直接つなぎます。R1 内蔵 PC にはログインしません。

取得には、Unitree 公式 SDK のサンプルと同じ方法を使います。`unitree_sdk2_python` の `example/go2/front_camera/camera_opencv.py` にある、`VideoClient` の `GetImageSample()` で JPEG を1枚ずつ受け取る方法です。SDK に R1 専用のカメラサンプルは無く、あるのは Go2 / B2 用だけなので、R1 で同じサービス（`videohub`）が応答するかは実機で確かめる必要があります。`scripts/camera_source.py` の `videohub` がこの方法の実装です。

```python
from unitree_sdk2py.go2.video.video_client import VideoClient

client = VideoClient()
client.SetTimeout(3.0)
client.Init()
code, data = client.GetImageSample()          # code == 0 なら成功
image = cv2.imdecode(np.frombuffer(bytes(data), np.uint8), cv2.IMREAD_COLOR)
```

`videohub` と `udp` を指定すると、起動時に PC の IP と R1 への ping を確かめ、README の前提と違えば警告を出します。

## 使い方

実機なしで一通り試す場合は、ダッシュボードを起動してから別のターミナルで実行します。

```bash
pixi run dashboard
pixi run make-qr --box-id B001 B002 --zone A B
pixi run recognize --source webcam --show --server http://localhost:8000/events
```

実機につないだら、先に診断で映像が来るかを確かめます。`<iface>` は `192.168.123.164` を設定した有線LANアダプタの名前です（Windows は `Get-NetAdapter` の Name 列）。

```bash
pixi run camera-check --source videohub -i <iface>
pixi run recognize --source videohub -i <iface> --show --server http://localhost:8000/events
```

QRの中身は `{"box_id":"B001","zone":"A"}` の JSON か、`B001:A` の簡易形式のどちらでも読めます。`--zones`（既定 `A,B`）に無い分類先と、解釈できない内容は、`error`（`UNKNOWN_QR`）として送ります。ダッシュボードの誤出荷アラートはこのイベントで出ます。

## 分類する場所の認識と、制御（担当A）への指示

区画A/B には、中身が `ZONE:A` / `ZONE:B` の QR を貼ります（`pixi run make-qr --zone-marker A B` で `work/qr/ZONE_A.png` などを生成）。認識側は、このマーカーが画面のどこに、どの大きさで見えているかを毎フレーム計算します。

| 値 | 意味 |
|---|---|
| `offset` | 画面中心からの左右のずれ。-1.0（左端）〜 +1.0（右端） |
| `bearing` | `left` / `center` / `right`。`offset` の絶対値が 0.15 以下なら `center` |
| `size_ratio` | マーカーの辺の長さ ÷ 画像の幅。近づくほど大きくなる |
| `distance_m` | 距離の推定値。`--marker-size-cm` と `--hfov-deg` を両方指定したときだけ出る。カメラの画角は未計測なので目安 |

A への渡し方は2通りあります。R1 に有線でつなぐ PC は1台なので、**同じプロセスに組み込む方式を基本**にしています。

同じプロセスに組み込む場合は、A のスクリプトから `Recognizer` を使います。DDS の初期化（`ChannelFactoryInitialize`）は A の側で1回だけ行い、`Recognizer` は初期化しません。

```python
from camera_source import open_source
from recognition_link import Recognizer

rec = Recognizer(open_source("videohub"), zones=("A", "B"),
                 server="http://localhost:8000/events")
rec.start()
box = rec.pop_box(timeout=0.2)        # 箱のQRが確定したら {"box_id": "B001", "zone": "A"}
target = rec.get_target(box["zone"])  # 区画マーカーが見えていれば offset / bearing / size_ratio
# target が None なら探索、bearing が left / right なら旋回、size_ratio が閾値以上なら到着
rec.stop()
```

別のプロセスで受ける場合は、`recognize` に `--command-url` を付けます。箱が確定すると `go_to` を1回、その後は区画マーカーの位置を `target` として毎秒2回まで POST します。見失って1秒たつと `visible: false` を1回送ります。

```json
{"v": 1, "ts": "2026-11-14T14:58:48+09:00", "cmd": "go_to", "box_id": "B001", "zone": "A", "run_id": "20261114-1458"}
{"v": 1, "ts": "2026-11-14T14:58:48+09:00", "cmd": "target", "zone": "A", "visible": true, "offset": 0.64, "bearing": "right", "size_ratio": 0.104, "distance_m": 0.72, "run_id": "20261114-1458"}
```

動きを確かめるには、ターミナルを2つ使います。

```bash
pixi run control-example --listen 8001
pixi run recognize --source webcam --show --command-url http://localhost:8001/command
```

`scripts/control_example.py` の `decide()` が、観測から「探索・左へ旋回・右へ旋回・前進・到着」を決める見本です。`loco.Move()` と `StopMove()` を書く場所をコメントで示してあります。

## Gemini による見た目の照合

分類先の判定は QR だけで行います。Gemini は追加の確認で、箱の QR が確定した瞬間の画像から「台帳に書いた見た目と合っているか」を判定します。`recognize` や `pick-move` に `--gemini` を付けたときだけ動きます。

- 送るのは、QR の周辺を切り出して長辺 768px 以下に縮めた画像1枚です。周りの人が写り込む範囲を減らすためです。
- 台帳は `scripts/catalog.json` です。箱ID ごとに `expected`（期待する見た目）を書きます。いま入っているのは見本なので、実際の箱の特徴に書き換えてください。
- 合えば `verified` イベント、合わなければ `error`（`CONTENT_MISMATCH`）を receiver へ送ります。
- 照合は別スレッドで行います。タイムアウト（既定20秒）、API の失敗、キー未設定、台帳に無い箱のいずれでも、QR の読取と移動は止まりません。
- API キーは環境変数 `GEMINI_API_KEY` からだけ読みます。コードにもログにも出しません。リポジトリには絶対に入れないでください。
- モデルは `gemini-3.5-flash-lite` が既定で、`--gemini-model` で変えられます。SDK は使わず、標準ライブラリで Interactions API（`/v1beta/interactions`）を呼びます。

**実際の API で動作を確認済みです（2026-11-14）。** `work/camera_check.jpg`（QR 紙を持つ人物）で、応答の JSON を判定まで読み取れました（不一致・確信度1.00、見た目の説明も妥当）。所要時間は 5〜6 秒で、`gemini-3.5-flash-lite` でも1回ごとに数秒かかるため、タイムアウトの既定を 8秒→20秒にしました。照合は別スレッドなので、移動は待ちません。`.env` の `GEMINI_API_KEY` を使う場合は、実行前に環境変数へ読み込んでください（スクリプトは `.env` を直接読みません）。

```bash
pixi run gemini-verify --image work/camera_check.jpg --box-id B001 --dry-run
pixi run gemini-verify --image work/camera_check.jpg --box-id B001
```

1行目は送る本文を表示するだけで、API は呼びません。

## ほかの担当と決めること

| 相手 | 決めること |
|---|---|
| A（制御） | 同じプロセスに組み込むか、HTTP で受けるか。到着の判定（`size_ratio` の閾値）をどちらが持つか。`moving` / `arrived` イベントは A が送る |
| C（ダッシュボード） | `CONTENT_MISMATCH` を誤出荷アラートの対象に加えるか（いまは `UNKNOWN_QR` だけ）。`verified` はタイムラインにだけ出る。receiver を動かす PC の IP。`pixi.toml` の `[tasks]` を双方が編集しているので、main に入れる順番 |
| D（PM・デモ） | 台帳（`scripts/catalog.json`）に書く、実際の箱の見た目。印刷済みの箱の QR の中身が `{"box_id":"B001","zone":"A"}` か `B001:A` になっているか。区画マーカー（`ZONE:A` / `ZONE:B`）の印刷と、貼る高さ・大きさ。実機の時間割（カメラの確認に15分ほど） |
| スタッフ | `videohub` が応答しない場合の、R1 のアプリでのサービス状態の確認 |

## 実機での最初の計測と、その対策（11/14 15:34）

R1 の頭部カメラ（1280x720）で、1.0m 正面の QR を10秒計測した結果は次のとおりでした。

| 項目 | 値 |
|---|---|
| 総フレーム数 | 6（0.5 fps） |
| 読取にかかった時間 | 平均 2000 ms、最大 5811 ms |
| 読めたフレーム | 0 |

原因は2つありました。

- **読取処理が遅かった**。読めなかったフレームで、検出を最大6回（うち4回は 2560x1440 に拡大した画像）やり直す作りになっていました。fps が低かったのはカメラではなく、この処理のせいです。
- **QR が小さく写っていた可能性が高い**。手元の実験では、1280x720 の画像で QR の見かけの幅が 60px 以下だと読めず、80px でぎりぎり、100px 以上で安定します。1.0m 離れると、10cm 角の QR はカメラの画角が90度でも約 64px にしかなりません。ただし、このときの画像は保存されておらず、QR が画面に入っていなかった可能性も残っています。

対策として、読取を作り直しました（`qr_reader.py` の `QRScanner`）。

- 毎フレームは速い1回だけ読み、重いやり直しは1フレームに1種類ずつ順番に回す。
- 一度読めた QR は、その周辺だけを拡大して追う。
- 確定の条件を「3フレーム連続」から「2秒以内に3回」に変えた。fps が低くても確定できる。
- `make-qr` の箱の QR は、短い形式（`B001:A`）を既定にした。マス目が粗くなり、同じ印刷サイズでも読みやすい。
- `qr-survey --save` は、読めなくても最後のフレームを `work/qr_survey_<日時>_last.jpg` に保存する。取得と読取の時間も分けて表示する。

開発用 PC（OpenCV 5.0）での1フレームの時間は、QR なしで平均 118 ms・最大 541 ms、追跡中で平均 120〜170 ms でした。作り直す前（400〜1000 ms）よりは速くなりましたが、目標にしていた「最悪 150 ms 以下」には届いていません。実機側の PC（OpenCV 4.8 系）での値は未計測です。

## 次にやること

1. **実機のカメラで読取成功率を測り直す**。最新のコードを取り込み、まず 30cm まで近づけて読めることを確かめてから、距離を伸ばします。`--save` を付けて、読めなかったときの画像を残してください。`--label` に条件を書くと、`work/qr_survey.csv` に1行ずつ残ります。読める最大距離と、そのときの `size_ratio`、1フレームの読取時間を押さえるのが目的です。

   ```bash
   pixi run qr-survey --source videohub -i <iface> --seconds 10 --save --label "0.3m 正面"
   ```

2. **移動を伴う通し確認をする**。必ず次の順で進めます。
   1. dry-run で、表示される速度の向きが正しいかを見る（マーカーを左に置くと `vyaw` が正、右で負、正面で前進）。
      ```bash
      pixi run pick-move --source videohub -i <iface> --zone A
      ```
   2. R1 を**スタンドに吊った状態**で `--execute` を付ける。停止担当がコントローラーを持つ2名体制で行う。
      ```bash
      pixi run pick-move --source videohub -i <iface> --zone A --execute
      ```
   3. 接地して、周囲を2m以上空けて同じコマンドを実行する。問題なければ `--zone` を外し、箱の QR から通す。
3. **到着の閾値を実測で決める**。既定の `--arrive-size 0.25` は仮の値です。1 の計測で、止まりたい距離での `size_ratio` を調べて置き換えます。QR の白い余白は大きさに含まれないので、見た目より少し小さく出ます。
4. **制御（担当A）と役割を合わせる**。`pick_move.py` は A の `go_to(zone)` に当たる部分を先に形にしたものです。A のスクリプトができたら、どちらを本番にするかを決めます。腕で指し示す動作は入っていません（到着後の `--gesture wave` のみ）。
5. **Gemini の照合を実際の箱で確かめる**。API は通っています。台帳（`scripts/catalog.json`）を実際の箱の見た目に書き換え、箱を写した画像で一致・不一致が妥当に出るかと、会場の回線での所要時間を見ます。

## 既知の注意点

- 読める QR の大きさの目安は、1280px 幅の画像で見かけの幅 100px 以上です。遠くから読ませたい場合は、QR を大きく印刷するか、読取位置を近づけます。
- 同じ QR を映し続けると、5秒ごとに `scanned` を再送します。1箱1回にしたい場合は、`qr_reader.py` の `StableDecoder` を「一度見えなくなるまで再送しない」動きに変えてください。
- `run_id` は起動時刻（`YYYYMMDD-HHMM`）を自動で付けます。リハーサルごとに `recognize` を起動し直すと、通し実行ごとに分かれます。
- receiver への送信に失敗しても認識は止まらず、警告を1行出します。422 が返る場合は、警告に出る本文で不足している項目が分かります。
- `pick_move.py` は `--execute` を付けない限りロボットに何も送りません。送るのは `Move` / `StopMove` と、`--gesture wave` のときの `WaveHand` だけです。速度は前進 0.3 m/s、旋回 0.5 rad/s を上限にコード側で丸めます（既定は 0.2 と 0.3）。マーカーを3秒見失うと止まって終了し、どの終わり方でも最後に `StopMove` を送ります。
- `pick_move.py` の `--execute` は SDK の無い環境で作ったため、実機でもモックでも未実行です。先に `-d 1` でモック（`mock_robot.py`）に向けて確かめられます。
- `videohub` は1枚ごとに要求と応答を往復するので、fps は高くありません。QR の読取には足りる見込みですが、実測はまだです。
- OpenCV 4.8 系（pixi 環境）では未実行です。版による API の違いはコード側で吸収していますが、最初に `pixi run test-recognition` を通してください。

## 作業の記録（2026-11-14）

- `docs/task_schedule.md` の担当B のタスクと、配布資料（Google Drive）を確認した。配布資料にカメラの取得方法は載っていなかった。
- Unitree 公式の R1 開発者マニュアル（Video streaming service interface）と、`unitree_sdk2_python` の映像サンプルを確認した。
- 取得元を差し替えられる構成で、取得・読取・送信のスクリプトを作成した。
- `docs/log_schema.md` と `dashboard/receiver.py` に合わせて、イベントに `v` / `run_id` / `source` を付け、`ts` をタイムゾーン付きにした。`task_schedule.md` の「イベント JSON」の6項目だけでは、receiver が 422 を返す。
- 合成した QR 画像で、正常系と想定外 QR の両方が receiver に保存されることを確認した。
- 別の PC で `camera-check --source videohub` を実行し、R1 の頭部カメラの画像を保存できた。
- 読取成功率の計測（`qr_survey.py`）と、認識と移動の結合（`pick_move.py`）を追加した。
- Gemini による見た目の照合（`--gemini`）を追加した。Gemini API は Interactions API が標準になっていたので、そちらで実装した。偽のサーバーで確認したあと、実際の API でも動作を確認した（応答は5通りのうち既存の抽出処理で読めた）。
- 区画マーカーの認識と、制御（担当A）への指示の受け渡し（`Recognizer` と `--command-url`）を追加した。合成画像で、箱の確定から `go_to`、`target` の受信までを確認した。

## 参照

- Unitree SDK2 Python の映像サンプル: <https://github.com/unitreerobotics/unitree_sdk2_python/tree/master/example/go2/front_camera>
- R1 開発者マニュアル（映像）: <https://support.unitree.com/home/en/R1_developer/video_service>
- ダッシュボードの使い方: [dashboard_manual.md](dashboard_manual.md)
- イベントの形式: [log_schema.md](log_schema.md)
