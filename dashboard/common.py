"""ダッシュボード共通処理（ログの読み書き・状態の導出）。

データレイアウトは docs/log_schema.md、画面仕様は docs/dashboard_spec.md を参照。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = Path(os.environ.get("EVENTS_LOG", ROOT / "logs" / "events.jsonl"))
_frame_env = os.environ.get("LIVE_FRAME", "")  # scripts/live_frame.py と同じ出力先
FRAME_PATH = Path(_frame_env) if _frame_env and _frame_env.lower() != "off" else ROOT / "logs" / "latest.jpg"
RECEIVER_URL =os.environ.get("RECEIVER_URL", "http://127.0.0.1:8000")

ZONES = ("A", "B")
IDLE_SEC = 15  # 完了/エラーからこの秒数イベントが無ければ「待機」に戻す（仮値）

_lock = threading.Lock()


def append_event(event: dict, path: Path = LOG_PATH) -> None:
    """1イベントを JSONL に追記する（既存行は書き換えない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False)
    with _lock, path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_events(path: Path = LOG_PATH) -> list[dict]:
    """全イベントを古い順に返す。壊れた行は読み飛ばす。"""
    if not path.exists():
        return []
    events = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def reset_log(path: Path = LOG_PATH) -> Path | None:
    """ログをバックアップへ退避して空にする。退避先を返す（元ファイルが無ければ None）。"""
    with _lock:
        if not path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")
        path.rename(backup)
        return backup


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def derive_state(events: list[dict], now: datetime | None = None) -> dict:
    """最新イベントからロボット状態を導出する。

    state: idle / scan / move / done / err
    """
    idle = {"state": "idle", "label": "待機", "box_id": None, "zone": None, "event": None}
    table = {
        "scanned": ("scan", "読取中"),
        "moving": ("move", "移動中"),
        "arrived": ("done", "完了"),
        "error": ("err", "エラー"),
    }
    # 未知のイベントは状態を変えない（直近の既知イベントを使う）
    known = [e for e in events if e.get("event") in table]
    if not known:
        return idle
    last = known[-1]
    state, label = table[last["event"]]
    # 移動中・読取中は長引くことがあるため、待機に戻すのは完了/エラーのみ
    if state in ("done", "err"):
        now = now or datetime.now().astimezone()
        try:
            age = now - parse_ts(last.get("received_at") or last["ts"])
        except (KeyError, ValueError):
            age = timedelta(0)
        if age > timedelta(seconds=IDLE_SEC):
            return idle
    return {
        "state": state,
        "label": label,
        "box_id": last.get("box_id"),
        "zone": last.get("zone"),
        "event": last["event"],
    }


def summarize(events: list[dict]) -> dict:
    """区画別処理数と誤出荷検知数を数える。"""
    zones = {z: 0 for z in ZONES}
    unknown_qr = 0
    for e in events:
        if e.get("event") == "arrived" and e.get("zone") in zones:
            zones[e["zone"]] += 1
        if e.get("event") == "error" and (e.get("error") or {}).get("code") == "UNKNOWN_QR":
            unknown_qr += 1
    return {"zones": zones, "unknown_qr": unknown_qr}
