#!/usr/bin/env python3
"""Step 3: 目的位置への移動。

Step 1 が状態JSONに書いた箱の区画(box.zone。--zone で上書き可)から移動先を特定し、
state["destination"] に記録したうえで、その区画の固定位置へ歩く。
既定は固定位置移動(カメラ不使用・時間ベース)。区画マーカー追跡は --track のときだけ。
固定位置は scripts/demo/zone_positions.json(--positions で変更)。--approach-x/y/yaw で一時的に上書きできる。
腕は Step 2 で把持したまま保持する(このフェーズでは腕を触らない)。既定は dry-run、実機へ送るのは --execute のときだけ。

例:
  python scripts/demo/phase3_move.py --force                         # dry-run(固定位置)
  python scripts/demo/phase3_move.py --zone B --force --approach-x 0.5
  python scripts/demo/phase3_move.py -i eth0 --execute               # 実機(固定位置)
  python scripts/demo/phase3_move.py --track --source videohub -i eth0 --execute   # マーカー追跡
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from common import (DEMO_DIR, PhaseError, add_common_args, add_robot_args, now_iso, run_phase,
                    setup_console)

VALID_ZONES = ("A", "B")
DEFAULT_POSITIONS = DEMO_DIR / "zone_positions.json"


def build_parser():
    parser = argparse.ArgumentParser(description="Step 3: 箱の区画(状態JSON)へ移動する。既定は固定位置・dry-run")
    add_common_args(parser)
    add_robot_args(parser)
    parser.add_argument("--zone", type=str, default=None, help="移動先の区画。省略時は状態JSONの箱の区画")
    parser.add_argument("--positions", type=str, default=str(DEFAULT_POSITIONS),
                        help="区画ごとの固定位置JSON(x前方m / y左方m / yaw rad)")
    parser.add_argument("--approach-x", type=float, default=None, help="固定位置の x を上書き(m)")
    parser.add_argument("--approach-y", type=float, default=None, help="固定位置の y を上書き(m)")
    parser.add_argument("--approach-yaw", type=float, default=None, help="固定位置の yaw を上書き(rad)")
    parser.add_argument("--vx", type=float, default=0.2, help="並進速度 m/s(上限 0.3 に丸める)")
    parser.add_argument("--vyaw", type=float, default=0.3, help="旋回速度 rad/s(上限 0.5 に丸める)")
    parser.add_argument("--move-timeout", type=float, default=30.0, help="移動の制限秒数")
    parser.add_argument("--max-seconds", type=float, default=None, help="全体の制限秒数(テスト用)")
    parser.add_argument("--track", action="store_true",
                        help="固定位置ではなく、区画マーカー(QR ZONE:A)をカメラで追って近づく")
    # --track 用(pick_move.py と同じ)
    parser.add_argument("--source", choices=("webcam", "file", "videohub", "udp"), default="webcam",
                        help="--track のカメラ取得元")
    parser.add_argument("--index", type=int, default=0, help="webcam のカメラ番号")
    parser.add_argument("--path", type=str, default=None, help="file 取得元の画像/動画パス")
    parser.add_argument("--port", type=int, default=5001, help="udp のポート")
    parser.add_argument("--zones", type=str, default="A,B", help="想定する zone(カンマ区切り)")
    parser.add_argument("--turn-offset", type=float, default=0.15, help="--track: 旋回を始める |offset|")
    parser.add_argument("--arrive-size", type=float, default=0.25, help="--track: 到着とみなす size_ratio")
    parser.add_argument("--lost-timeout", type=float, default=3.0, help="--track: 見失って待つ秒数")
    parser.add_argument("--search", action="store_true", help="--track: 見失ったら旋回して探す")
    return parser


def resolve_zone(ph) -> tuple[str, str]:
    """状態JSONの box.zone(または --zone)から移動先の区画を決める。"""
    zone = (ph.args.zone or (ph.box or {}).get("zone") or "").strip().upper()
    origin = "--zone" if ph.args.zone else "box.zone"
    if not zone:
        raise PhaseError("PREREQUISITE", "移動先の区画が不明です(Step 1 が未実行か、--zone を指定してください)")
    if zone not in VALID_ZONES:
        raise PhaseError("UNKNOWN_QR", f"区画 {zone!r} は想定外です(候補: {', '.join(VALID_ZONES)})")
    return zone, origin


def resolve_position(args, zone: str) -> dict:
    try:
        table = json.loads(Path(args.positions).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PhaseError("UNEXPECTED", f"固定位置ファイル {args.positions} を読めません({e})") from e
    pos = table.get(zone)
    if not isinstance(pos, dict):
        raise PhaseError("UNEXPECTED", f"{args.positions} に区画 {zone} の固定位置がありません")
    out = {k: float(pos.get(k, 0.0)) for k in ("x", "y", "yaw")}
    for k, v in (("x", args.approach_x), ("y", args.approach_y), ("yaw", args.approach_yaw)):
        if v is not None:
            out[k] = float(v)
    return out


def make_loco(args):
    import pick_move
    if not args.execute:
        print("[dry-run] ロボットにはコマンドを送りません(実機へ送るには --execute)。", flush=True)
        return pick_move.DryRunLoco()
    try:
        # フェーズは別プロセスで動くので、このプロセスで必ず DDS を初期化する
        # (ChannelFactory は初期化済みなら何もしないため、videohub 側との二重初期化は無害)
        return pick_move.make_real_loco(args.domain, args.interface, init_dds=True)
    except pick_move.PickMoveError as e:
        raise PhaseError("ROBOT_FAULT", str(e)) from e


def body(ph):
    import pick_move
    args = ph.args
    zone, origin = resolve_zone(ph)
    ph.state["destination"] = {"zone": zone, "decided_at": now_iso(), "from": origin}
    mode = "track" if args.track else "fixed"
    ph.step(f"移動先を区画 {zone} に決定({origin} → state の destination)", zone=zone, origin=origin, mode=mode)

    clock, sleep = time.monotonic, time.sleep
    deadline = clock() + args.max_seconds if args.max_seconds else None
    try:
        if args.track:
            appr = pick_move.ZoneApproach(args.vx, args.vyaw, args.turn_offset, args.arrive_size,
                                          args.lost_timeout, args.move_timeout, args.search)
            pos = None
        else:
            pos = resolve_position(args, zone)
            appr = pick_move.FixedApproach(pos["x"], pos["y"], pos["yaw"], args.vx, args.vyaw, args.move_timeout)
            ph.step(f"固定位置へ移動: x={pos['x']:+.2f}m y={pos['y']:+.2f}m yaw={pos['yaw']:+.2f}rad "
                    f"(所要 {appr.total:.1f}秒)", **pos)
    except pick_move.PickMoveError as e:
        raise PhaseError("UNEXPECTED", str(e)) from e

    loco = make_loco(args)
    if args.execute:
        print(pick_move.SAFETY_NOTICE, flush=True)
        sleep(3.0)
        try:
            pick_move.ensure_running_mode(loco, sleep)
        except pick_move.PickMoveError as e:
            raise PhaseError("ROBOT_FAULT", str(e)) from e
    ph.log("moving", detail=f"区画 {zone} へ移動開始({mode})", zone=zone,
           data={"from": "start", "to": zone, "mode": mode, "position": pos})
    t_move = clock()
    try:
        if args.track:
            state, reason = _track(args, loco, zone, appr, deadline, clock, sleep)
        else:
            state, reason = pick_move._approach_fixed(loco, appr, deadline, clock, sleep)
    finally:
        try:
            loco.StopMove()  # 腕は保持したまま。ここでは解放しない
        except Exception as e:
            print(f"[警告] StopMove に失敗: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    if state != "arrived":
        code = "MOVE_FAILED" if state == "failed" else "INTERRUPTED"
        raise PhaseError(code, f"区画 {zone} への移動に失敗: {reason}")
    duration_ms = int((clock() - t_move) * 1000)
    ph.log("arrived", detail=f"区画 {zone} に到着", zone=zone, data={"duration_ms": duration_ms, "mode": mode})
    ph.step(f"区画 {zone} に到着({mode}, {duration_ms / 1000:.1f}秒)")
    return {"zone": zone, "mode": mode, "position": pos, "duration_ms": duration_ms, "dry_run": ph.dry_run}


def _track(args, loco, zone, appr, deadline, clock, sleep):
    import pick_move
    from camera_source import CameraError, source_from_args
    from recognition_link import recognizer_from_args
    rec = None
    try:
        source = source_from_args(args)
        rec = recognizer_from_args(args, source)
        rec.start()
        return pick_move._approach(rec, loco, zone, appr, deadline, clock, sleep)
    except CameraError as e:
        raise PhaseError("ROBOT_FAULT", f"カメラを開けません: {e}") from e
    finally:
        if rec is not None:
            rec.stop(close_source=True)


def main(argv=None) -> int:
    setup_console()
    return run_phase("move", build_parser().parse_args(argv), body)


if __name__ == "__main__":
    sys.exit(main())
