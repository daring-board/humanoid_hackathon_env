# デモスクリプト説明

デモ動作シナリオ（スプレッドシート「デモ動作シナリオ」）の Step 0〜4 を、**1フェーズ＝1スクリプト**で実装したもの。全部を順に呼ぶルートスクリプト `run_demo.py` がある。置き場所は `scripts/demo/`。

## シナリオとスクリプトの対応

| Step | フェーズ名 | スクリプト | 内容 | 状態 |
|---|---|---|---|---|
| 0 | `approach` | `phase0_approach.py` | Box に近づく（QR が読める位置まで） | **未実装のスタブ**。実行すると `skipped` になる（シナリオ上「無理なら 1 から開始」） |
| 1 | `read_qr` | `phase1_read_qr.py` | QR を読み、次のアクション（区画 A or B）を決める | 実装済み（既存の認識コードを利用） |
| 2 | `grasp` | `phase2_grasp.py` | ロボットアームで Box を把持 | 実装済み。`pick_move.py` と同じ `ArmController` で把持（既定 preset。custom は動作の事前登録が必要） |
| 3 | `move` | `phase3_move.py` | 状態JSONの箱の区画から移動先を特定し、**固定位置へ歩く**（時間ベース。`--track` で区画マーカー追跡） | 実装済み。固定位置は `scripts/demo/zone_positions.json`（**仮置き。現地で実測して書き換える**） |
| 4 | `release` | `phase4_release.py` | 目的位置で Box を解放・設置（`ArmController.release`） | 実装済み |
| — | — | `run_demo.py` | 上記を順に呼ぶルート | 実装済み |

ファイル構成:

```
scripts/demo/
  common.py            状態JSON・イベントログ・フェーズ実行枠・腕操作の共通部品
  phase0_approach.py … phase4_release.py
  run_demo.py          ルートスクリプト
```

既存の `scripts/`（`camera_source.py` / `qr_reader.py` / `recognition_link.py` / `pick_move.py` / `r1_clients.py` など）は、これらから import して使う部品・単体ツールとして残している。`recognize.py` / `pick_move.py` / `control_example.py` などは単体の確認用としてそのまま使える。

## 使い方

pixi 環境で実行する（以下、リポジトリのルートで）。**既定は dry-run**で、ロボットには何も送らず「送るはずの内容」を表示するだけ。実機へ送るのは `--execute` を付けたときだけ。

```bash
# 全フェーズを dry-run（PC内蔵カメラ）
pixi run demo --source webcam

# 実機で通し（Step 1 と 3 は R1 の頭部カメラ）
pixi run demo --source videohub -i eth0 --execute

# 途中から（状態JSONを引き継ぐ）／途中まで／特定フェーズを飛ばす
pixi run demo --from grasp --execute -i eth0
pixi run demo --to read_qr --source videohub -i eth0
pixi run demo --skip approach --source webcam

# 1フェーズだけ（pixi タスクは demo-approach / demo-read-qr / demo-grasp / demo-move / demo-release）
pixi run demo-read-qr --source file --path work/qr/B001_A.png --timeout 5
pixi run demo-grasp --force                  # 前フェーズが未完了でも dry-run で確認
pixi run demo-move --zone A --force
```

### ルート `run_demo.py` の引数

| 引数 | 意味 |
|---|---|
| `--from PHASE` / `--to PHASE` | 実行範囲。`--from` が先頭（`approach`）のときだけ新しい通し実行（新しい `run_id`）を始め、状態JSONを初期化する。途中からのときは状態JSONを引き継ぐ |
| `--skip PHASE` | そのフェーズを飛ばす（状態は `skipped`）。複数指定可 |
| `--state PATH` / `--log PATH` | 状態JSON・イベントログの場所（既定は下記） |
| その他の引数 | 各フェーズが受け付けるものだけを渡す。例: `--source` `--path` `-i` `-d` `--execute` `--zone` `--gemini` `--timeout` |

いずれかのフェーズが失敗（終了コード≠0）したら、そこで止まる。ルートの終了コードは、失敗したフェーズの終了コード（全部成功なら 0）。

### 各フェーズの主な引数

| フェーズ | 主な引数（`--help` で全部見られる） |
|---|---|
| 共通 | `--state` `--log` `--force`（前フェーズ未完了でも実行。単体確認用） |
| `read_qr` | カメラ: `--source {webcam,file,videohub,udp}` `--path` `-i` `-d` ／ 認識: `--zones` `--gemini` `--catalog` ／ `--timeout`（QR 待ちの秒数、既定 60）／ `--show` / `--no-show`（映像表示、既定は表示）／ 引き渡し: `--then "コマンド"` `--notify-url URL`（下の「分類動作への引き渡し」） |
| `grasp` / `release` | `--execute` `-i` `-d` ／ `--arm-mode {preset,custom,none}` `--custom-grab` `--custom-release` `--custom-wait` ／ `release` のみ `--gesture wave`。sdk モードはフェーズ間で保持できないため非対応 |
| `move` | `--zone`（状態JSONの区画を上書き）`--positions` `--approach-x/y/yaw`（固定位置の上書き）`--vx` `--vyaw` `--move-timeout` ／ `--track` とその用の `--source` `--path` など |

## 映像の表示（QRの読み取り確認）

`read_qr`（Step 1）は、既定でカメラ映像をウィンドウに表示する。取得元（`webcam` / `videohub` / `file`）に関係なく同じ表示になる。

- 読み取った箱の QR は**緑の枠＋読み取った文字**で重ねる（区画マーカーを映したときは青の枠）
- 左上に直近のイベント（`scanned: A001 A` など）、下にフェーズの状況（QR 待ちの残り秒数）を出す
- ウィンドウで **q** を押すとフェーズを中断する（`INTERRUPTED`）
- 画面の文字は OpenCV の都合で英数字のみ
- 表示しない（SSH 接続や GUI なしの環境）ときは `--no-show`。ルートから渡してもよい（`pixi run demo --no-show ...`）。GUI の無い OpenCV では自動で表示を止めて続行する

## 状態JSON（フェーズ間の受け渡し）

`logs/state.json`（環境変数 `DEMO_STATE` か `--state` で変更）。フェーズ同士は直接やり取りせず、この1ファイルだけで受け渡す。書き込みは一時ファイル経由で置き換えるので、読む側が途中の内容を見ることはない。

```json
{
  "v": 1,
  "run_id": "20261114-172944",
  "created_at": "...", "updated_at": "...",
  "current_phase": "release",
  "box": {"box_id": "B001", "zone": "A", "scanned_at": "...", "verified": null},
  "phases": {
    "approach": {"status": "skipped", "started_at": "...", "finished_at": "...", "message": "...", "result": null},
    "read_qr":  {"status": "done", "...": "...", "result": {"box_id": "B001", "zone": "A", "verified": null}},
    "grasp": {}, "move": {}, "release": {}
  }
}
```

| キー | 書く人 | 読む人 | 意味 |
|---|---|---|---|
| `run_id` | 通し実行の開始時 | 全員 | 通し実行の識別子。イベントログの `run_id` と同じ |
| `box` | `read_qr` | `grasp` `move` `release` | 読み取った箱。`zone` が目的の区画（A / B）。`verified` は Gemini の見た目照合（`true` 一致 / `false` 不一致 / `null` 未実施・結果なし） |
| `destination` | `move` | `release`、移動を実装するスクリプト | 移動先。`{"zone": "A", "decided_at": "...", "from": "box.zone"}`（`--zone` で上書きしたときは `from` が `--zone`） |
| `phases.<name>.status` | 各フェーズ | 各フェーズ・ルート | `pending` / `running` / `done` / `skipped` / `failed` |
| `phases.<name>.result` | 各フェーズ | — | 完了時の結果（動作名・所要時間など） |

前提のチェック: `grasp` は `read_qr`、`move` は `grasp`、`release` は `move` が `done`（または `skipped`）でないと `PREREQUISITE` エラーで止まる。フェーズを実行し直すと、そのフェーズ以降の状態は `pending` に戻る。新しい通し実行を始めたいときは `run_demo.py`（先頭から）を実行するか、`logs/state.json` を削除する。

## 分類動作への引き渡し

`read_qr` が読んだ結果（箱ID と区画）を、分類動作（掴む・運ぶ・置く）を実装するスクリプトへ渡す方法は3通りある。

```bash
# 1. 読めたら続けてコマンドを起動する（{box_id} {zone} {run_id} {verified} {state} を置き換える）
pixi run demo-read-qr --source videohub -i eth0 --then "python scripts/sort.py --zone {zone} --box {box_id}"

# 2. 状態JSONから取り出す（読むだけ。まだ読めていなければ終了コード 3）
pixi run demo-next-action                  # 1行の JSON
pixi run demo-next-action --field zone     # 値だけ（A / B）
pixi run demo-next-action --fresh --wait 30   # 起動後に読まれた次の箱を、最大30秒待つ

# 3. HTTP で通知する（{"cmd":"sort","box_id":...,"zone":...} を POST。失敗しても read_qr は成功）
pixi run demo-read-qr --source videohub -i eth0 --notify-url http://localhost:8002/command
```

- `--then` のコマンドは、状態JSONを保存した後に起動する。終了コードは、そのコマンドのものになる。QR が読めなかったときは起動しない。ルート（`pixi run demo --then "..."`）から渡してもよい。
- Python からは `scripts/demo/next_action.py` の `read_next_action()` / `wait_next_action()` を import して使える。
- テスト: `pixi run test-demo-handoff`。

## イベントログ（各ステップの記録）

`logs/events.jsonl` に1行1イベントで追記する。ダッシュボードが読むのと同じファイル・同じ形式（[log_schema.md](log_schema.md)）なので、実行するとダッシュボードのタイムラインに出る。

各フェーズは次を必ず記録する。

| event | タイミング |
|---|---|
| `demo_start` / `demo_end` | ルートの開始・終了（`demo_end` は失敗時 `error.code = PHASE_FAILED`） |
| `phase_start` / `phase_end` | 各フェーズの開始・終了。`phase_end` の `data` に `status`（done / skipped / failed）と `duration_ms` |
| `step` | フェーズ内の各ステップ（カメラを開く、QR 確定、腕動作の再生、到着 など） |
| `scanned` / `moving` / `arrived` | 既存のイベント。`read_qr` が `scanned`、`move` が `moving` / `arrived` を出す |
| `error` | 失敗時。`error.code`（`QR_READ_FAILED` `UNKNOWN_QR` `ROBOT_FAULT` `PREREQUISITE` `INTERRUPTED` `UNEXPECTED`） |
| `verified` / `CONTENT_MISMATCH` | `read_qr --gemini` のとき、見た目の照合結果 |

すべてのイベントの `data.phase` にフェーズ名が入る。`source` は `read_qr` が `recognition`、それ以外が `control`。`box_id` / `zone` は、`read_qr` で確定した後のイベントから入る。

確認の例:

```bash
pixi run python -c "import json;[print(e['ts'][11:19], e['event'], (e.get('data') or {}).get('phase'), e.get('detail')) for e in map(json.loads, open('logs/events.jsonl', encoding='utf-8')) if e['run_id']=='<run_id>']"
```

## 実機で動かす前に

- **走行モード**: `move` は、移動の前に FSM ID を確認する。走行モード（811）ならそのまま、Locked Stand（4）なら走行モードに切り替える。それ以外（ダンピングなど）では動かさずに失敗する。
- **移動のエラー**: `Move` がエラーコードを3回続けて返すと、止まって失敗にする（以前は時間が来ると「到着」になっていた）。11/15 の実機では `Move` が 127 を返し続けた。原因は未確定で、走行モードでないか、腕が姿勢を保持していて歩けない可能性がある。
- **DDS**: どのフェーズも、ロボットに送る前にそのプロセスで DDS を初期化する（`scripts/dds_util.py`）。

- **腕動作**: 既定の preset は、前の姿勢保持を解除（99）してから `hug`（19、ハグ）を送る。`--grab-preset` で別のプリセット（例 `forward_push`）に変えられる。解放は 99。事前登録は不要。腕の操作がエラーコードを返すと、フェーズは失敗になる（7401 は1回だけ解除してやり直す）。`--arm-mode custom` ならロボットのティーチング機能で `grasp_box` / `release_box` を保存しておく（`GetActionList()` で確認）。箱を掴めるかは未検証。
- **`ReleaseArm` が箱の保持にどう影響するかは実機で未確認**。
- **移動は時間ベースのデッドレコニング**: `zone_positions.json` の x/y/yaw は仮置き。実機で歩かせて調整する。Step 3 の間、腕は Step 2 で把持したまま保持する（途中で失敗しても腕は解放しない。Step 4 か手動で解放）。
- 安全: 2名体制（実行者＋コントローラーを持った停止担当）、周囲 2m 以上を空ける、緊急停止は L2+B 長押し。`--execute` を付けた腕動作（`grasp` / `release`）は、開始前に注意書きと数秒の待ちが入る。
- `phase0_approach` は未実装。ロボットを箱の前（QR が見える位置）に手で置いてから `--from read_qr`（または `--skip approach`）で始める。

## 動作確認の状況

PC 上で、`file` 取得元（`work/qr/` の合成画像）と dry-run により、次を確認した。実機・R1 のカメラ・実際の腕動作は**未確認**。

- 先頭から通しで実行 → 各フェーズが `done`（approach は `skipped`）、状態JSONとイベントログが期待どおりに残る
- `move` は `box.zone` を `destination` に写して完了する（`--zone B --force` で上書きも確認）
- 前フェーズ未完了で `grasp` を単体実行すると `PREREQUISITE` で止まり、`--force` で通る

画像の作り方: `pixi run make-qr --box-id B001 --zone A`。固定表の QR（`ITEM-001` など）は `scripts/qr_reader.py` の `FIXED_QR_TABLE`。

## スクリプトを足す・変えるとき

- 新しいフェーズ: `common.py` の `PHASES` / `PHASE_INFO` / `REQUIRES` に追加し、`phaseN_xxx.py` を既存のものに倣って作る（`build_parser()` と `body(ph)`、`main()` で `run_phase()` を呼ぶ）。ルートは `build_parser()` を見て引数を振り分けるので、ルートの変更は不要。
- ステップの記録は `ph.step("...", キー=値)`。`step` 以外のイベントは `ph.log("moving", ...)`。失敗は `raise PhaseError(code, メッセージ)`、意図して飛ばすなら `raise PhaseSkipped("理由")`。
- 状態JSONへ渡したい情報は `ph.state[...]` に入れておけば、フェーズ終了時に保存される。
