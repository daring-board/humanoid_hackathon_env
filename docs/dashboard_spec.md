# ダッシュボード仕様（C 担当）

`docs/task_schedule.md` の C 担当タスクの仕様。イベント JSON のフォーマットは [log_schema.md](log_schema.md) を正とする。

## 方針
主役は「ロボット状態＋作業タイムライン」。集計・マップは補助。

## 構成（2プロセス・ローカル完結）
```
B/A ──POST /events──▶ receiver (FastAPI, :8000) ──append──▶ events.jsonl（正）
                                                             ▲ read
                                            dashboard (Streamlit, :8501)
```
- Streamlit は HTTP 受信口を持たないため、受信専用の `dashboard/receiver.py`（FastAPI、`POST /events` と `GET /health` のみ）を分離する
- 永続化は JSONL（1行1イベント、追記のみ）。Streamlit は `st.fragment(run_every=1)` で末尾を読み込み再描画
- 集計は読み込み時に pandas で都度計算（DB不要）。SQLite は使わない（JSONL で十分・工数削減）
- 起動：`pixi run dashboard`（receiver と streamlit を同時起動するタスクを `pixi.toml` に登録）
- 依存追加：`fastapi`, `uvicorn`, `streamlit`, `pandas`

### カメラ映像（最新フレーム方式）
```
認識スレッド(recognition_link) ──live_frame.publish──▶ logs/latest.jpg ──read──▶ dashboard
```
- `scripts/live_frame.py` が認識ループ内から呼ばれ、最新フレームに検出枠（箱=緑、区画マーカー=オレンジ）を重ねて `logs/latest.jpg` に保存する
- 0.5 秒に1回へ間引き、幅 640px・JPEG 品質 70 に縮小。一時ファイルへ書いて `os.replace` で差し替える（読み手に壊れた画像を見せない）。差し替えに失敗（Windows で読み取り中など）した回は次回へ持ち越す
- 失敗しても例外は投げず、認識は止めない
- 出力先は環境変数 `LIVE_FRAME` で変更、`LIVE_FRAME=off` で無効化（`dashboard/common.py` の `FRAME_PATH` も同じ変数を読む）
- 実効 1〜2 fps。滑らかな映像が必要になったら receiver に MJPEG エンドポイントを足す（別方式）

## 画面（1ページ・横長でプロジェクタ投影を想定）
1. ヘッダー：ロボット状態バッジ（待機 / 読取中 / 移動中 / 完了 / エラー）と現在の箱ID・分類先を大きく表示
2. 左：作業タイムライン（新しい順。時刻・箱ID・分類先・イベント種別・detail）
3. 右上：R1 カメラ映像（`logs/latest.jpg`。ファイルが無ければ案内表示、5秒以上更新が無ければ警告）
4. 右中：簡易マップ（スタート・区画A/B の固定配置。`moving`/`arrived` に応じてロボットアイコンを移動。位置情報は取らない）
5. 右下：集計（区画ごとの処理数、誤出荷検知数、直近の処理時間）
6. 誤出荷アラート：想定外QR（`error` かつ定義外 zone）で赤いバナーを表示

## イベント→状態の対応
| event | ロボット状態 | マップ |
|---|---|---|
| `scanned` | 読取中→（判定後）移動準備 | スタート地点 |
| `moving` | 移動中 | スタート→区画 zone へ |
| `arrived` | 完了 | 区画 zone |
| `error` | エラー（誤出荷アラート） | 変更なし |

無イベントが一定時間続いたら「待機」に戻す（例：15秒。仮値）。

## 運用機能（デモ・開発用、サイドバー）
- ダミーイベント送信ボタン（scanned→moving→arrived／誤出荷）— 実機なしの開発と保険デモ用
- ログのリセット（`events.jsonl` をバックアップ退避してから空に）
- receiver 疎通表示（最終受信時刻）

## スコープ
- Must：状態バッジ、タイムライン、receiver、ダミー送信
- Should：区画別集計、誤出荷アラート、マップ移動
- Could：処理時間KPI、ログCSVダウンロード

## 未確定
- 誤出荷判定の条件（B が想定外 QR を `error` で送る取り決め）→ D1 15:00 のイベント仕様確定で合意
- 待機に戻るまでの秒数

## モックアップ
[dashboard_mockup.html](dashboard_mockup.html)（ブラウザで開くと動作確認できる。表示はすべてサンプル）

## 実装
- 場所: `dashboard/`（`receiver.py` 受信 / `app.py` 画面 / `common.py` ログ読み書き・状態導出・映像パス / `send_dummy.py` ダミー送信 / `run.py` 同時起動）
- 使い方は [dashboard_manual.md](dashboard_manual.md) を参照
- 状態の「待機に戻す」は完了・エラーのみ対象（読取中・移動中は長引くため戻さない）
