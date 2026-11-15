# ロボット行動ログ データレイアウト

R1 の行動ログを JSON で保存・送信するための共通フォーマット。B/A が送信し、C の receiver が受けて保存し、ダッシュボードが読む。

## 保存形式
- **JSONL**（1行1イベント、UTF-8、追記のみ）。ファイルは `logs/events.jsonl`
- 1行は改行を含まない1つの JSON オブジェクト（`json.dumps(obj, ensure_ascii=False)`）
- 既存行は書き換えない。訂正は新しいイベントを追記する
- リセット時は `logs/events.<yyyymmdd-HHMMSS>.bak.jsonl` に退避してから空にする
- 送信は `POST http://<devPC>:8000/events`（本文は下記の1イベント）。受信側が `id` と `received_at` を付けて保存する

## イベントのフィールド

| フィールド | 型 | 必須 | 付与者 | 説明 |
|---|---|---|---|---|
| `v` | int | ○ | 送信側 | スキーマ版。現在 `1` |
| `id` | string | ○ | receiver | イベント一意ID（ULID または `uuid4`）。重複送信の排除に使う |
| `ts` | string | ○ | 送信側 | 発生時刻。ISO 8601・タイムゾーン付き（例 `2026-11-15T10:30:00.123+09:00`） |
| `received_at` | string | ○ | receiver | 受信時刻（同形式）。`ts` との差で遅延を見る |
| `robot` | string | ○ | 送信側 | ロボット識別子（例 `R1`） |
| `run_id` | string | ○ | 送信側 | 通し実行（デモ1回・リハ1回）の識別子（例 `20261115-1030`）。リハ成功率の集計に使う |
| `box_id` | string \| null | ○ | 送信側 | 箱ID。QR を読めていない時は `null` |
| `event` | string | ○ | 送信側 | イベント種別（下記） |
| `zone` | string \| null | ○ | 送信側 | 分類先 `"A"` / `"B"`。未確定・不明は `null` |
| `source` | string | ○ | 送信側 | 送信元モジュール：`recognition`（B）/ `control`（A）/ `dashboard`（ダミー送信） |
| `detail` | string | - | 送信側 | 人が読むメッセージ（タイムライン表示用） |
| `error` | object \| null | - | 送信側 | `event` が `error` の時のみ。`{ "code": "...", "message": "..." }` |
| `data` | object | - | 送信側 | イベント固有の付加情報（下記）。無ければ省略 |

必須フィールドの null は「値が無い」を意味し、`"?"` や空文字は使わない。

## event 種別と `data`

| event | 意味 | 主な送信元 | `data` の例 |
|---|---|---|---|
| `scanned` | QR 読取成功・分類判定済み | recognition | `{"qr_raw": "{\"box_id\":\"B001\",\"zone\":\"A\"}", "attempts": 1, "read_ms": 800}` |
| `moving` | 区画へ移動開始 | control | `{"from": "start", "to": "A"}` |
| `arrived` | 区画に到着（腕での指し示し完了含む） | control | `{"duration_ms": 12400, "gesture": "point"}` |
| `error` | 異常（読取失敗・想定外QR・移動失敗） | どちらも | `{"qr_raw": "..."}` など |
| `verified` | Gemini による見た目の照合が一致 | recognition | `{"observed": "...", "confidence": 0.9}` |
| `demo_start` / `demo_end` | デモ通し実行（`run_demo.py`）の開始・終了 | control | `{"phases": [...], "execute": false}` / `{"ok": true, "failed_phase": null}` |
| `phase_start` / `phase_end` | 1フェーズ（`scripts/demo/phaseN_*.py`）の開始・終了 | recognition / control | `{"phase": "grasp", "step": 2}` / `{"phase": "grasp", "status": "done", "duration_ms": 5200}` |
| `step` | フェーズ内の1ステップ（人が読む進捗） | recognition / control | `{"phase": "move"}` |

- `phase_start` の `detail` は `▶▶▶ Step N 開始: <ラベル> ― <処理概要>`、`phase_end` は `■■■ Step N ✅完了/❌失敗/⏭スキップ(秒): …`。`data` に `step` / `label`（start は `summary` = 処理概要）を持つ。ダッシュボードはこの2種を色付きで強調表示する
- `scripts/demo/` の各スクリプトは、上記すべてを `data.phase`（`approach` / `read_qr` / `grasp` / `move` / `release`）付きで `logs/events.jsonl` に直接追記する（receiver は通さない）。詳細は [demo_scripts.md](demo_scripts.md)

- ダッシュボードは上記4種のみ解釈する。未知の `event` はタイムラインにだけ表示し、状態は変えない
- 拡張したい時は新しい `event` 名を足し、この表に追記する

## error.code 一覧

| code | 意味 | 誤出荷アラート |
|---|---|---|
| `UNKNOWN_QR` | QR は読めたが `zone` が定義外・形式不正（想定外QR） | 表示する |
| `QR_READ_FAILED` | リトライ上限まで読めなかった | 表示しない（エラー表示のみ） |
| `MOVE_FAILED` | 移動中の異常・タイムアウト | 表示しない |
| `ROBOT_FAULT` | ロボット側の異常（接続断・API エラー） | 表示しない |
| `CONTENT_MISMATCH` | 箱の見た目が台帳と不一致（Gemini 照合） | 表示しない |
| `PREREQUISITE` | 前フェーズが未完了、または必要な情報（区画など）が無い | 表示しない |
| `PHASE_FAILED` | 通し実行がいずれかのフェーズの失敗で中断した（`demo_end`） | 表示しない |
| `INTERRUPTED` | Ctrl+C でフェーズが中断された | 表示しない |
| `UNEXPECTED` | 想定外の例外（フェーズのスクリプトのバグなど） | 表示しない |

## 例
実際のファイルは1行1イベントだが、ここでは読みやすいように整形して示す。

### 正常系（1箱ぶん：読取 → 移動 → 到着）

`scanned`（QR 読取成功）
```json
{
  "v": 1,
  "id": "01JD8R3K4ZP6Q",
  "ts": "2026-11-15T10:30:00.120+09:00",
  "received_at": "2026-11-15T10:30:00.150+09:00",
  "robot": "R1",
  "run_id": "20261115-1030",
  "box_id": "B001",
  "event": "scanned",
  "zone": "A",
  "source": "recognition",
  "detail": "QR読取成功 (0.8s)",
  "data": {
    "qr_raw": "{\"box_id\":\"B001\",\"zone\":\"A\"}",
    "attempts": 1,
    "read_ms": 800
  }
}
```

`moving`（区画Aへ移動開始）
```json
{
  "v": 1,
  "id": "01JD8R3K9XM2T",
  "ts": "2026-11-15T10:30:02.050+09:00",
  "received_at": "2026-11-15T10:30:02.071+09:00",
  "robot": "R1",
  "run_id": "20261115-1030",
  "box_id": "B001",
  "event": "moving",
  "zone": "A",
  "source": "control",
  "detail": "区画Aへ移動",
  "data": {
    "from": "start",
    "to": "A"
  }
}
```

`arrived`（到着・指し示し完了）
```json
{
  "v": 1,
  "id": "01JD8R4C7HE5W",
  "ts": "2026-11-15T10:30:14.480+09:00",
  "received_at": "2026-11-15T10:30:14.502+09:00",
  "robot": "R1",
  "run_id": "20261115-1030",
  "box_id": "B001",
  "event": "arrived",
  "zone": "A",
  "source": "control",
  "detail": "腕で指し示し完了",
  "data": {
    "duration_ms": 12400,
    "gesture": "point"
  }
}
```

### 想定外QR（誤出荷アラート対象）
```json
{
  "v": 1,
  "id": "01JD8R6A2NB8Y",
  "ts": "2026-11-15T10:31:20.300+09:00",
  "received_at": "2026-11-15T10:31:20.318+09:00",
  "robot": "R1",
  "run_id": "20261115-1030",
  "box_id": "X999",
  "event": "error",
  "zone": null,
  "source": "recognition",
  "detail": "想定外QR: 区画定義なし",
  "error": {
    "code": "UNKNOWN_QR",
    "message": "zone が定義外"
  },
  "data": {
    "qr_raw": "X999"
  }
}
```

### 送信側が POST する本文（`id` と `received_at` は付けない）
```json
{
  "v": 1,
  "ts": "2026-11-15T10:30:00.120+09:00",
  "robot": "R1",
  "run_id": "20261115-1030",
  "box_id": "B001",
  "event": "scanned",
  "zone": "A",
  "source": "recognition",
  "detail": "QR読取成功 (0.8s)"
}
```

## 集計の定義（ダッシュボード）
- 区画別処理数：`event == "arrived"` を `zone` ごとに数える（`run_id` で絞り込み可）
- 誤出荷検知数：`error.code == "UNKNOWN_QR"` の件数
- 1箱の処理時間：同一 `run_id`・`box_id` の最初の `scanned` から `arrived` までの `ts` 差
- 通しの成功率：`run_id` ごとに `arrived` まで到達した箱数 ÷ 最初の `scanned`/`error` が出た箱数

## 送信側の実装メモ
- 送信側は `id` と `received_at` を付けない（receiver が付与）。`v`・`ts`・`robot`・`run_id`・`box_id`・`event`・`zone`・`source` は必ず付ける
- receiver は必須フィールドが欠けていたら 422 を返し、保存しない
- 時刻は端末間のずれに注意。全員の PC を時刻同期するか、`received_at` を並び順の正とする
- `qr_raw` は QR の生文字列。個人情報は入れない

## 未確定
- `run_id` の採番ルール（デモ開始ボタンで発行するか、環境変数で渡すか）
- QR の中身の形式（`task_schedule.md` では `{"box_id":"B001","zone":"A"}`）と、B が `UNKNOWN_QR` を送る条件
