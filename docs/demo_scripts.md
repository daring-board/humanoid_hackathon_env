# デモスクリプト説明

デモ動作シナリオ（スプレッドシート「デモ動作シナリオ」）の Step 0〜4 を、**1フェーズ＝1スクリプト**で実装したもの。全部を順に呼ぶルートスクリプト `run_demo.py` がある。置き場所は `scripts/demo/`。

## シナリオとスクリプトの対応

| Step | フェーズ名 | スクリプト | 内容 | 状態 |
|---|---|---|---|---|
| 0 | `approach` | `phase0_approach.py` | Box に近づく（QR が読める位置まで） | **未実装のスタブ**。実行すると `skipped` になる（シナリオ上「無理なら 1 から開始」） |
| 1 | `read_qr` | `phase1_read_qr.py` | QR を読み、次のアクション（区画 A or B）を決める | 実装済み（既存の認識コードを利用） |
| 2 | `grasp` | `phase2_grasp.py` | ロボットアームで Box を把持 | 実装済み。**腕動作の事前登録が必要**（下記） |
| 3 | `move` | `phase3_move.py` | 移動先（区画 A or B）を決めて状態JSONに記録する | 実装済み。**ロボットは動かさず、カメラも使わない**（移動先は固定の場所なので、区画マーカーの確認はしない） |
| 4 | `release` | `phase4_release.py` | 目的位置で Box を解放・設置 | 実装済み。**腕動作の事前登録が必要**（下記） |
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
| `read_qr` | カメラ: `--source {webcam,file,videohub,udp}` `--path` `-i` `-d` ／ 認識: `--zones` `--gemini` `--catalog` ／ `--timeout`（QR 待ちの秒数、既定 60）／ `--show` / `--no-show`（映像表示、既定は表示） |
| `grasp` / `release` | `--execute` `-i` `-d` ／ `--custom-action NAME`（既定 `grasp_box` / `release_box`）`--preset NAME`（プリセットで代用。例 `hug`）`--action-wait 秒` ／ `release` のみ `--keep-arm` |
| `move` | `--zone`（状態JSONの区画を上書き。A / B） |

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

## イベントログ（各ステップの記録）

`logs/events.jsonl` に1行1イベントで追記する。ダッシュボードが読むのと同じファイル・同じ形式（[log_schema.md](log_schema.md)）なので、実行するとダッシュボードのタイムラインに出る。

各フェーズは次を必ず記録する。

| event | タイミング |
|---|---|
| `demo_start` / `demo_end` | ルートの開始・終了（`demo_end` は失敗時 `error.code = PHASE_FAILED`） |
| `phase_start` / `phase_end` | 各フェーズの開始・終了。`phase_end` の `data` に `status`（done / skipped / failed）と `duration_ms` |
| `step` | フェーズ内の各ステップ（カメラを開く、QR 確定、腕動作の再生、到着 など） |
| `scanned` | 既存のイベント。`read_qr` が出す（`move` は `moving` / `arrived` を**出さない**。ロボットが動かないため） |
| `error` | 失敗時。`error.code`（`QR_READ_FAILED` `UNKNOWN_QR` `ROBOT_FAULT` `PREREQUISITE` `INTERRUPTED` `UNEXPECTED`） |
| `verified` / `CONTENT_MISMATCH` | `read_qr --gemini` のとき、見た目の照合結果 |

すべてのイベントの `data.phase` にフェーズ名が入る。`source` は `read_qr` が `recognition`、それ以外が `control`。`box_id` / `zone` は、`read_qr` で確定した後のイベントから入る。

確認の例:

```bash
pixi run python -c "import json;[print(e['ts'][11:19], e['event'], (e.get('data') or {}).get('phase'), e.get('detail')) for e in map(json.loads, open('logs/events.jsonl', encoding='utf-8')) if e['run_id']=='<run_id>']"
```

## 実機で動かす前に

- **腕動作の事前登録**: `grasp` / `release` は、ロボットのティーチング機能で保存したカスタム動作 `grasp_box` / `release_box` を再生する。**この名前の動作を作って保存するところは、まだ誰もやっていない**（`GetActionList()` で一覧を確認できる）。保存できないうちは `--preset`（例 `--preset hug`）で代用できるが、箱を掴めるかは未検証。
- **`ReleaseArm` の意味は未確認**: `release` は動作の後に `ReleaseArm()`（プリセット ID 99）を送って腕の制御を通常状態に戻す。この呼び出しが箱の保持にどう影響するかは実機で未確認。不安なら `--keep-arm`。
- **移動は未実装**: `move` は移動先を状態JSONに記録するだけで、ロボットを動かさない。固定の場所へ実際に歩かせる処理は、`destination.zone` を読んで別に実装する（旧版の区画マーカー接近は `scripts/pick_move.py` に残っている）。
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
