#!/usr/bin/env python3
"""「次に何をすべきか」(QRを読んだ結果)を状態JSONから取り出す道具。フェーズではない。

ロボットにもカメラにも触らず、状態JSONを書き換えない。分類動作(掴む・運ぶ・置く)を実装する人が、
自分のスクリプトやシェルから phase1_read_qr の結果を受け取るために使う。

コマンドとして:
  python scripts/demo/next_action.py                    # read_qr が done なら1行のJSON(終了コード0)
  python scripts/demo/next_action.py --field zone       # 値だけ。シェルから zone=$(pixi run demo-next-action --field zone)
  python scripts/demo/next_action.py --wait 30          # read_qr が done になるまで最大30秒待つ(0.2秒ごとに読み直す)
  python scripts/demo/next_action.py --fresh --wait 60  # このコマンドを起動した後に読まれた「次の箱」だけを待つ
  python scripts/demo/next_action.py --newer-than 2026-11-15T10:00:00+09:00

標準出力(JSON):
  {"run_id":..,"box_id":..,"zone":..,"verified":..,"scanned_at":..,"destination":{..}|null,
   "phase_status":{"approach":..,"read_qr":"done","grasp":"pending",..}}
終了コード: 0=取り出せた / 3=まだ無い(read_qr が done でない・状態JSONや箱が無い・--wait の時間切れ・
            --fresh/--newer-than より古い。理由は標準エラーに日本語で、標準出力は空) / 1=状態JSONが壊れている

Python から使う(分類動作のスクリプトの例):
    import sys
    sys.path.insert(0, "scripts/demo")          # リポジトリ直下から実行する場合。絶対パスでもよい
    from next_action import read_next_action, wait_next_action

    act = read_next_action()                    # done でなければ None
    act = wait_next_action(timeout=60)          # done になるまで待つ(時間切れは None)
    if act:
        print(act["box_id"], act["zone"], act["verified"])   # 例: B001 A True

    # 次の箱を待つ(古い箱を拾わない)
    from datetime import datetime
    started = datetime.now().astimezone()
    act = wait_next_action(timeout=120, newer_than=started)

別プロセスとして呼ぶ(シェルを使わない例):
    import json, subprocess, sys
    r = subprocess.run([sys.executable, "scripts/demo/next_action.py", "--wait", "30"],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode == 0:
        act = json.loads(r.stdout)
    elif r.returncode == 3:
        print("まだ箱が読まれていない:", r.stderr.strip())
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from common import PHASES, STATE_PATH, load_state, setup_console

POLL_SEC = 0.2
FIELDS = ("box_id", "zone", "verified", "run_id", "scanned_at")


def _to_dt(v) -> datetime | None:
    """ISO 文字列または datetime を、タイムゾーン付き datetime にする(タイムゾーン無しは現地時刻とみなす)。"""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.astimezone()
    return None


def _why_not(state_path, newer_than=None) -> tuple[dict | None, str]:
    """(結果, 取り出せない理由)。取り出せたら (結果, "")。壊れた状態JSONは RuntimeError。"""
    path = Path(state_path) if state_path is not None else STATE_PATH
    state = load_state(path)
    if state is None:
        return None, f"状態JSON {path} がありません(まだ phase1_read_qr を実行していません)"
    status = (state.get("phases") or {}).get("read_qr", {}).get("status")
    if status != "done":
        return None, f"read_qr がまだ完了していません(状態: {status})"
    box = state.get("box")
    if not box:
        return None, "状態JSONに箱の情報(box)がありません"
    if newer_than is not None:
        scanned = _to_dt(box.get("scanned_at"))
        if scanned is None or scanned <= newer_than:
            return None, f"箱 {box.get('box_id')} は {newer_than.isoformat(timespec='seconds')} より前に読まれたものです"
    return {
        "run_id": state.get("run_id"), "box_id": box.get("box_id"), "zone": box.get("zone"),
        "verified": box.get("verified"), "scanned_at": box.get("scanned_at"),
        "destination": state.get("destination"),
        "phase_status": {p: (state.get("phases") or {}).get(p, {}).get("status") for p in PHASES},
    }, ""


def read_next_action(state_path=None, newer_than=None) -> dict | None:
    """read_qr が done なら結果の辞書、そうでなければ None。状態JSONが壊れていれば RuntimeError。"""
    return _why_not(state_path, _to_dt(newer_than))[0]


def wait_next_action(state_path=None, timeout: float = 60.0, newer_than=None) -> dict | None:
    """read_qr が done になる(newer_than 指定時はそれより後に読まれる)まで待つ。時間切れは None。"""
    nt = _to_dt(newer_than)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        res = _why_not(state_path, nt)[0]
        if res is not None or time.monotonic() >= deadline:
            return res
        time.sleep(min(POLL_SEC, max(0.0, deadline - time.monotonic())))


def build_parser():
    p = argparse.ArgumentParser(description="QRを読んだ結果(次のアクション)を状態JSONから取り出す(読むだけ)")
    p.add_argument("--state", type=str, default=str(STATE_PATH), help="状態JSONのパス")
    p.add_argument("--field", choices=FIELDS, default=None, help="JSONではなく、その項目の値だけを1行で出す")
    p.add_argument("--wait", type=float, default=None, metavar="秒", help="read_qr が done になるまで待つ最大秒数")
    p.add_argument("--newer-than", type=str, default=None, metavar="ISO時刻",
                   help="この時刻より後に scanned_at が更新された箱だけを認める")
    p.add_argument("--fresh", action="store_true", help="このコマンドを起動した時刻より後に読まれた箱だけを認める")
    return p


def _fmt(v) -> str:
    return "null" if v is None else ("true" if v is True else "false" if v is False else str(v))


def main(argv=None) -> int:
    setup_console()
    a = build_parser().parse_args(argv)
    started = datetime.now().astimezone()
    nt = _to_dt(a.newer_than)
    if a.newer_than and nt is None:
        print(f"[エラー] --newer-than の時刻を解釈できません: {a.newer_than}", file=sys.stderr)
        return 2
    if a.fresh and (nt is None or started > nt):
        nt = started
    deadline = time.monotonic() + max(0.0, a.wait or 0.0)
    try:
        while True:
            res, why = _why_not(a.state, nt)
            if res is not None or time.monotonic() >= deadline:
                break
            time.sleep(min(POLL_SEC, max(0.0, deadline - time.monotonic())))
    except RuntimeError as e:
        print(f"[エラー] {e}", file=sys.stderr)
        return 1
    if res is None:
        print(f"[待機中] {why}" + (f"(--wait {a.wait:g} 秒で時間切れ)" if a.wait else ""), file=sys.stderr)
        return 3
    print(_fmt(res[a.field]) if a.field else json.dumps(res, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
