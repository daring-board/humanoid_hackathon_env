# 認識（担当B）引き継ぎメモ

最終更新: 2026-11-15（Day2 朝）

QR を読んで区画を決め、分類動作へ渡すところまでは、PC 上（合成画像・dry-run）で通っています。デモは `scripts/demo/` のフェーズ別スクリプトで動かします（[demo_scripts.md](demo_scripts.md)）。残りは、大きくした QR での実機の読取確認と、分類動作（掴む・運ぶ・置く）との結合です。次に実機を使える人は「次にやること」の1から進めてください。

## いま動くもの・未確認のもの

| 項目 | 状態 | 確認方法・補足 |
|---|---|---|
| QRの読取と分類先の判定 | 動作確認済み（PC） | 合成画像と PC 内蔵カメラ。現場の QR（`ITEM-001` など）は固定表で区画に変換する |
| R1 頭部カメラからの取得（`videohub`） | 実機で確認済み | 1280x720 で取得できた |
| R1 のカメラでの QR 読取 | **未確認** | 初回（11/14 15:34）は 1.0m で1件も読めなかった。QR を段ボールの側面いっぱいに大きくしたので、測り直す |
| デモの通し（`pixi run demo`） | dry-run で動作確認済み | `read_qr` → `grasp` → `move` → `release`。実機の腕動作は未確認 |
| 分類動作への引き渡し | 動作確認済み（PC） | `tests/test_demo_handoff.py` が 7/7 成功。下の「分類動作への引き渡し」 |
| Gemini による見た目の照合（`--gemini`） | 実際の API で確認済み | 1回 5〜6 秒。台帳（`scripts/catalog.json`）は見本のまま |
| ダッシュボードへのログ | 動作確認済み（PC） | 下の「ダッシュボードへ送るログ」 |
| 箱の近くまでの移動（`approach`） | 未実装（優先度低） | 手で位置を合わせてから `read_qr` を始める |
| 区画マーカーへの移動（`pick_move.py`） | dry-run のみ | デモの通しでは使っていない |

main には、いまのところ通らないテストが2つあります。`scripts/test_recognition.py` の `test_pick_move_dry_run_and_arrived` と、`tests/test_pick_move.py`（import エラー）です。どちらも `pick_move.py` に腕の制御を統合した変更の後から起きており、認識と引き渡しの動作には影響しません。

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
- API キーは、リポジトリ直下の `.env` に貼ります。`.env.example` を `.env` という名前でコピーし、`GEMINI_API_KEY=` の右に貼ってください。`.env` は git に入りません。環境変数 `GEMINI_API_KEY` が設定されていれば、そちらが優先されます。キーはコードにもログにも出しません。
- モデルは `gemini-3.5-flash-lite` が既定で、`--gemini-model` で変えられます。SDK は使わず、標準ライブラリで Interactions API（`/v1beta/interactions`）を呼びます。

**実際の API で動作を確認済みです（2026-11-14）。** `work/camera_check.jpg`（QR 紙を持つ人物）で、応答の JSON を判定まで読み取れました（不一致・確信度1.00、見た目の説明も妥当）。所要時間は 5〜6 秒で、`gemini-3.5-flash-lite` でも1回ごとに数秒かかるため、タイムアウトの既定を 8秒→20秒にしました。照合は別スレッドなので、移動は待ちません。

```bash
pixi run gemini-verify --image work/camera_check.jpg --box-id B001 --dry-run
pixi run gemini-verify --image work/camera_check.jpg --box-id B001
```

1行目は送る本文を表示するだけで、API は呼びません。

## 分類動作への引き渡し

`read_qr`（`pixi run demo-read-qr`）が QR を読んで、箱ID と区画を `logs/state.json` に書きます。分類動作（掴む・運ぶ・置く）を実装するスクリプトへは、次の3通りで渡せます。

| 渡し方 | 使い方 | 向いている場面 |
|---|---|---|
| 続けて起動 | `demo-read-qr --then "python scripts/sort.py --zone {zone} --box {box_id}"` | 分類側が1本のスクリプト |
| 取り出し | `demo-next-action`（JSON）、`demo-next-action --field zone`（値だけ） | 分類側が好きなタイミングで読む |
| 通知（HTTP） | `demo-read-qr --notify-url http://<PC>:8002/command` | 分類側が別の PC や常駐プロセス |

- `--then` は、QR が確定して状態を保存した後にコマンドを起動します。`{box_id}` `{zone}` `{run_id}` `{verified}` `{state}` を置き換え、そのコマンドの終了コードをそのまま返します。QR が読めなかったときは起動しません。
- `demo-next-action` は状態を読むだけです。まだ読めていなければ終了コード 3 を返します。`--wait 30` で読めるまで待ち、`--fresh` を付けると起動後に読まれた箱だけを返します（次の箱を待つ用途）。Python からは `next_action.read_next_action()` で同じ内容を取れます。
- `--notify-url` は次の JSON を POST します。送信に失敗しても `read_qr` は失敗にしません。

```json
{"v": 1, "ts": "2026-11-15T09:40:00+09:00", "cmd": "sort", "box_id": "B001", "zone": "A", "verified": null, "scanned_at": "2026-11-15T09:40:00.123+09:00", "run_id": "20261115-0940", "source": "recognition"}
```

## 挟む動作ができない場合の代わり

箱を腕で挟む動作が間に合わない場合に、QR を読んだ後の見せ方として使える案です。上ほど確実です。

1. **押して仕分ける**。箱を台か床に置き、腕のプリセット `forward_push`（前に押す）で区画の側へ押し出す。
2. **指し示して人に渡す**。`extend_right_arm_forward`（右腕を前に伸ばす）で区画を指し、音声で区画を伝える。
3. **載せて運ぶ**。腕に掛けるかごやトレーに人が箱を載せ、ロボットは区画まで歩く。
4. **判定役に徹する**。読取位置に立ったまま箱を順に見せ、区画を答える。想定外の QR や見た目の不一致を検知する場面を主役にする。

1 と 2 はプリセット動作だけで済み、関節の直接制御が要りません。ただし腕のプリセットは歩行中に実行できず、姿勢の保持は最長20秒で解除されます。`demo-grasp --preset forward_push` のように、いまのフェーズにそのまま指定できます。

## ダッシュボードへ送るログ

`logs/events.jsonl` に1行1イベントで追記され、ダッシュボードが1秒ごとに読んで表示します。形式は [log_schema.md](log_schema.md) です。

| イベント | 出すタイミング | 出す側 |
|---|---|---|
| `scanned` | 箱の QR が確定 | 認識 |
| `error`（`UNKNOWN_QR`） | 想定外の QR。誤出荷アラートの対象 | 認識 |
| `verified` / `error`（`CONTENT_MISMATCH`） | Gemini の照合結果（`--gemini` のとき） | 認識 |
| `demo_start` / `demo_end`、`phase_start` / `phase_end`、`step` | デモの各フェーズの進行 | デモスクリプト |
| `moving` / `arrived` | `pick_move.py` で移動したとき | 制御 |

- デモスクリプトは receiver を通さず、ファイルに直接書きます。ダッシュボードと同じ PC で動かしてください。別の PC から送る場合は `recognize --server <URL>` を使います。
- デモの `move` フェーズはロボットを動かさないので、`moving` / `arrived` を出しません。ダッシュボードの「移動中 → 到着」は、通しのデモでは変わりません。
- `CONTENT_MISMATCH` は、ダッシュボードの誤出荷アラートの対象になっていません（対象は `UNKNOWN_QR` だけ）。

## ほかの担当と決めること## 実機での最初の計測と、その対策（11/14 15:34）

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

優先度の高い順です。

1. **大きくした QR で、実機の読取を測り直す**。読める距離と、実機側の PC での1フレームの読取時間を押さえます。読めなかったときの画像が `work/qr_survey_<日時>_last.jpg` に残ります。

   ```bash
   pixi run qr-survey --source videohub -i <iface> --seconds 10 --save --label "0.5m 正面 大QR"
   ```

2. **分類動作と実際につなぐ**。分類動作の担当と、上の3通りのどれで受け取るかを決めます。迷ったら `--then` が最も単純です。
3. **腕動作の名前を合わせる**。`grasp` / `release` は、ティーチングで保存した `grasp_box` / `release_box` という名前の動作を再生します。挟む動作の担当と、この名前で保存するか、`--custom-action` で別の名前を渡すかを決めます。
4. **挟む動作ができない場合の代わりを1つ決めておく**（上の「挟む動作ができない場合の代わり」）。
5. **Gemini の台帳を実際の箱に合わせる**。`scripts/catalog.json` の `expected` を、実際の箱の見た目に書き換えます。
6. （低）**箱の近くまで移動する**。`approach` フェーズは未実装です。
7. （低）**ダッシュボード**。`CONTENT_MISMATCH` をアラートの対象にするか、`move` で `moving` / `arrived` を出すかを決めます。

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

## 作業の記録（2026-11-15）

- 分類動作への引き渡しを追加した（`demo-read-qr` の `--then` / `--notify-url`、`demo-next-action`）。
- Gemini の API キーを、リポジトリ直下の `.env` から読めるようにした。
- 現場の QR を、段ボールの側面いっぱいの大きさに作り直した（実機での読取は未確認）。
- 箱を挟む動作は別の担当が実装中。箱の近くまでの移動は優先度を下げた。

## 参照

- Unitree SDK2 Python の映像サンプル: <https://github.com/unitreerobotics/unitree_sdk2_python/tree/master/example/go2/front_camera>
- R1 開発者マニュアル（映像）: <https://support.unitree.com/home/en/R1_developer/video_service>
- ダッシュボードの使い方: [dashboard_manual.md](dashboard_manual.md)
- イベントの形式: [log_schema.md](log_schema.md)
