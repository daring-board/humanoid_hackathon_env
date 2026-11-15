#!/usr/bin/env python3
"""認識と移動・把持の結合: 箱のQRが確定したら腕で把持し、その zone の区画マーカー(QR `ZONE:A`)を
カメラで追いながら近づき、十分大きく見えたら停止して腕を解放する。

既定は dry-run(ロボットに一切コマンドを送らず、送るはずだった Move 等を print するだけ)。
実機へ送るのは --execute を付けたときだけ。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arm_controller import ArmController  # noqa: E402
from camera_source import CameraError, add_source_args, source_from_args  # noqa: E402
from events import make_event, post_event  # noqa: E402
from recognition_link import add_recognizer_args, recognizer_from_args  # noqa: E402

VX_LIMIT = 0.3
VYAW_LIMIT = 0.5
PERIOD = 0.2
PRINT_INTERVAL = 1.0


class PickMoveError(RuntimeError):
    """実行できない状況(SDK 未導入・引数不正など)。日本語の原因を添える。"""


class ZoneApproach:
    """区画マーカーへ近づく判断(純粋なクラス。ロボットにもカメラにも触らない)。"""

    def __init__(self, vx_max=0.2, vyaw_max=0.3, turn_offset=0.15, arrive_size=0.25,
                 lost_timeout=3.0, move_timeout=30.0, search=False):
        self.vx_max = self._clamp("vx", vx_max, VX_LIMIT)
        self.vyaw_max = self._clamp("vyaw", vyaw_max, VYAW_LIMIT)
        self.turn_offset = max(float(turn_offset), 1e-6)
        self.arrive_size = float(arrive_size)
        self.lost_timeout = float(lost_timeout)
        self.move_timeout = float(move_timeout)
        self.search = bool(search)
        self._t0 = None
        self._last_seen = None

    @staticmethod
    def _clamp(name, value, limit):
        value = float(value)
        clamped = max(0.0, min(value, limit))
        if clamped != value:
            print(f"[警告] --{name} {value} は安全上の上限(0〜{limit})の範囲外のため "
                  f"{clamped} に丸めました。", file=sys.stderr)
        return clamped

    def fresh(self):
        return ZoneApproach(self.vx_max, self.vyaw_max, self.turn_offset, self.arrive_size,
                            self.lost_timeout, self.move_timeout, self.search)

    @staticmethod
    def _out(state, vx, vyaw, reason):
        return {"state": state, "vx": float(vx), "vy": 0.0, "vyaw": float(vyaw), "reason": reason}

    def step(self, target, now):
        if self._t0 is None:
            self._t0 = now
        if now - self._t0 > self.move_timeout:
            return self._out("failed", 0.0, 0.0, f"移動開始から {self.move_timeout:g} 秒を超えた")
        if target is None:
            ref = self._last_seen if self._last_seen is not None else self._t0
            lost = now - ref
            if lost < self.lost_timeout:
                return self._out("wait", 0.0, 0.0, f"マーカーが見えない({lost:.1f}秒)")
            if self.search:
                return self._out("search", 0.0, self.vyaw_max * 0.5, "マーカーを探して旋回")
            return self._out("failed", 0.0, 0.0,
                             f"区画マーカーを {self.lost_timeout:g} 秒以上見失った")
        self._last_seen = now
        offset, size = float(target["offset"]), float(target["size_ratio"])
        if size >= self.arrive_size:
            return self._out("arrived", 0.0, 0.0, f"size_ratio {size:.2f} >= {self.arrive_size:g}")
        if abs(offset) > self.turn_offset:
            sign = 1.0 if offset > 0 else -1.0
            return self._out("turn", 0.0, -sign * self.vyaw_max, f"offset {offset:+.2f} が大きい")
        vyaw = -offset / self.turn_offset * self.vyaw_max * 0.5
        return self._out("forward", self.vx_max, vyaw, "前進")


class FixedApproach:
    """カメラを使わず、開始位置から相対的な固定位置(x前方・y左方[m], 最後に yaw[rad] 旋回)へ
    時間ベース(デッドレコニング)で移動する。純粋なクラスで、ロボットにも触らない。
    実際の速度とのずれで位置誤差が出るため、現地で --approach-x 等を調整すること。"""

    def __init__(self, x=1.0, y=0.0, yaw=0.0, vx_max=0.2, vyaw_max=0.3, move_timeout=30.0):
        self.x, self.y, self.yaw = float(x), float(y), float(yaw)
        speed = ZoneApproach._clamp("vx", vx_max, VX_LIMIT)
        self.vyaw = ZoneApproach._clamp("vyaw", vyaw_max, VYAW_LIMIT)
        dist = (self.x ** 2 + self.y ** 2) ** 0.5
        if dist > 0 and speed <= 0:
            raise PickMoveError("--vx が 0 のため並進移動できません。")
        if self.yaw != 0 and self.vyaw <= 0:
            raise PickMoveError("--vyaw が 0 のため旋回できません。")
        self._vx = speed * self.x / dist if dist > 0 else 0.0
        self._vy = speed * self.y / dist if dist > 0 else 0.0
        self._t_move = dist / speed if dist > 0 else 0.0
        self._t_turn = abs(self.yaw) / self.vyaw if self.yaw != 0 else 0.0
        self.total = self._t_move + self._t_turn
        self.move_timeout = float(move_timeout)
        self._t0 = None

    def step(self, now):
        if self._t0 is None:
            self._t0 = now
        el = now - self._t0
        if self.total > self.move_timeout:
            return ZoneApproach._out("failed", 0.0, 0.0,
                                     f"必要時間 {self.total:.1f}秒 が --move-timeout {self.move_timeout:g}秒 を超える")
        if el < self._t_move:
            d = ZoneApproach._out("forward", self._vx, 0.0, f"並進 {el:.1f}/{self._t_move:.1f}秒")
            d["vy"] = self._vy
            return d
        if el < self.total:
            sign = 1.0 if self.yaw > 0 else -1.0
            return ZoneApproach._out("turn", 0.0, sign * self.vyaw,
                                     f"旋回 {el - self._t_move:.1f}/{self._t_turn:.1f}秒")
        return ZoneApproach._out("arrived", 0.0, 0.0, "固定位置に到達(時間ベース)")


class DryRunLoco:
    """ロボットへは何も送らず、送るはずだったコマンドを print するだけ。"""

    def __init__(self, interval=PRINT_INTERVAL, clock=time.monotonic):
        self._interval = interval
        self._clock = clock
        self._last = None
        self._last_t = -1e9

    def Move(self, vx, vy, vyaw):
        key = (round(vx, 3), round(vy, 3), round(vyaw, 3))
        now = self._clock()
        if key != self._last or now - self._last_t >= self._interval:
            print(f"[dry-run] Move(vx={vx:.3f}, vy={vy:.3f}, vyaw={vyaw:.3f})", flush=True)
            self._last, self._last_t = key, now
        return 0

    def StopMove(self):
        print("[dry-run] StopMove()", flush=True)
        return 0

    def WaveHand(self):
        print("[dry-run] WaveHand()", flush=True)
        return 0


def make_real_loco(domain, interface, init_dds):
    try:
        from r1_clients import R1LocoClient
        if init_dds:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    except ImportError as e:
        raise PickMoveError(
            f"--execute には unitree_sdk2py が必要ですが import できません({e})。"
            "pixi 環境(pixi run pick-move ...)で実行してください。") from e
    if init_dds:
        Path("/tmp").mkdir(exist_ok=True)
        ChannelFactoryInitialize(domain, interface)
    loco = R1LocoClient()
    loco.SetTimeout(10.0)
    loco.Init()
    time.sleep(1.0)
    return loco


def ensure_running_mode(loco, sleep=time.sleep, settle=2.0):
    """速度指令の前に FSM を確認し、Running Mode(811) でなければ切り替える。

    切り替えるのは Locked Stand(4) からだけ。脱力/ダンピング/仰向けなどからは
    安全のため自動で切り替えず PickMoveError にする(コントローラーで立たせてもらう)。
    """
    from r1_clients import FSM_LOCKED_STAND, FSM_RUN
    code, fsm = loco.GetFsmId()
    if code != 0:
        raise PickMoveError(f"現在のモードを取得できません(GetFsmId code={code})。接続を確認してください。")
    print(f"[mode] 現在の FSM ID: {fsm}", flush=True)
    if fsm == FSM_RUN:
        return fsm
    if fsm != FSM_LOCKED_STAND:
        raise PickMoveError(
            f"FSM ID {fsm} から自動で歩行モードにはできません。Locked Stand({FSM_LOCKED_STAND})に"
            f"立たせてから再実行してください(Running Mode は {FSM_RUN})。")
    print(f"[mode] Locked Stand → Running Mode({FSM_RUN}) に切り替えます", flush=True)
    code = loco.Start()
    if code != 0:
        raise PickMoveError(f"Running Mode への切り替えに失敗(SetFsmId code={code})")
    sleep(settle)
    code, fsm = loco.GetFsmId()
    if code != 0 or fsm != FSM_RUN:
        raise PickMoveError(f"切り替え後も Running Mode になっていません(code={code}, FSM ID={fsm})")
    print("[mode] Running Mode になりました", flush=True)
    return fsm


SAFETY_NOTICE = """\
================ 実機にコマンドを送ります(--execute) ================
 - 2名体制: 実行者とは別に、コントローラーを持った停止担当を置く
 - 周囲 2m 以上を空ける。初めて動かすときはロボットを吊った状態で試す
 - 緊急停止: 底面の Emergency Stop、または L2 + B を長押し
 - Ctrl+C でも StopMove と腕の安全解放が動きます
 3秒後に開始します。やめるなら今すぐ Ctrl+C。
====================================================================="""


def _check(label, code):
    if code not in (0, None):
        print(f"[警告] {label} がエラーコード {code} を返しました。", file=sys.stderr, flush=True)


class _StatusPrinter:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._key = None
        self._t = -1e9

    def show(self, zone, d, target):
        key = (d["state"], round(d["vx"], 3), round(d["vyaw"], 3))
        now = self._clock()
        if key == self._key and now - self._t < PRINT_INTERVAL:
            return
        self._key, self._t = key, now
        seen = (f"offset={target['offset']:+.2f} size_ratio={target['size_ratio']:.3f}"
                if target else "offset=- size_ratio=-")
        print(f"[区画 {zone}] {d['state']:<7} {seen} -> vx={d['vx']:.2f} vy={d['vy']:.2f} "
              f"vyaw={d['vyaw']:+.2f} ({d['reason']})", flush=True)


def _approach(rec, loco, zone, appr, deadline, clock, sleep):
    printer = _StatusPrinter(clock)
    next_t = clock()
    while True:
        now = clock()
        if deadline is not None and now >= deadline:
            return "timeout", "--max-seconds に達した"
        tgt = rec.get_target(zone)
        d = appr.step(tgt, now)
        printer.show(zone, d, tgt)
        if d["state"] in ("arrived", "failed"):
            return d["state"], d["reason"]
        _check("Move", loco.Move(d["vx"], d["vy"], d["vyaw"]))
        next_t += PERIOD
        sleep(max(0.0, next_t - clock()))


def _approach_fixed(loco, appr, deadline, clock, sleep):
    printer = _StatusPrinter(clock)
    next_t = clock()
    while True:
        now = clock()
        if deadline is not None and now >= deadline:
            return "timeout", "--max-seconds に達した"
        d = appr.step(now)
        printer.show("固定位置", d, None)
        if d["state"] in ("arrived", "failed"):
            return d["state"], d["reason"]
        _check("Move", loco.Move(d["vx"], d["vy"], d["vyaw"]))
        next_t += PERIOD
        sleep(max(0.0, next_t - clock()))


def run_approach(args, loco=None, arm=None, *, clock=time.monotonic, sleep=time.sleep):
    """approach モード: 認識を使わず、把持→固定位置へ移動→解放。"""
    appr = FixedApproach(args.approach_x, args.approach_y, args.approach_yaw,
                         args.vx, args.vyaw, args.move_timeout)
    if args.execute:
        print(SAFETY_NOTICE, flush=True)
    if loco is None:
        if args.execute:
            loco = make_real_loco(args.domain, args.interface, init_dds=args.source != "videohub")
        else:
            print("[dry-run] ロボットにはコマンドを送りません(実機へ送るには --execute)。", flush=True)
            loco = DryRunLoco()
    if arm is None:
        arm = ArmController(
            mode=getattr(args, "arm_mode", "preset"),
            execute=args.execute,
            custom_grab=getattr(args, "custom_grab", "grab_box"),
            custom_release=getattr(args, "custom_release", "release_arm"),
            custom_wait=getattr(args, "custom_wait", 3.0),
            clamp_roll=getattr(args, "clamp_roll", 0.15),
            open_roll=getattr(args, "open_roll", 0.45),
            shoulder_pitch=getattr(args, "shoulder_pitch", -0.35),
            elbow_angle=getattr(args, "elbow_angle", 0.50),
        )
    if args.execute:
        sleep(3.0)
    deadline = clock() + args.max_seconds if args.max_seconds else None
    events = []
    print(f"[approach] 固定位置へ移動: x={args.approach_x:+.2f}m y={args.approach_y:+.2f}m "
          f"yaw={args.approach_yaw:+.2f}rad(所要 {appr.total:.1f}秒)", flush=True)
    try:
        arm.grab()
        ev = make_event("moving", None, "fixed", detail="固定位置へ移動開始", source="control",
                        data={"from": "start", "to": "fixed", "arm_mode": arm.mode})
        post_event(args.server, ev)
        events.append(ev)

        t_move = clock()
        state, reason = _approach_fixed(loco, appr, deadline, clock, sleep)
        _check("StopMove", loco.StopMove())

        if state == "arrived":
            arm.release()
            if args.gesture == "wave":
                _check("WaveHand", loco.WaveHand())
            ev = make_event("arrived", None, "fixed", detail="固定位置に到着・配置完了", source="control",
                            data={"duration_ms": int((clock() - t_move) * 1000),
                                  "gesture": args.gesture, "arm_mode": arm.mode})
        elif state == "failed":
            arm.release()
            ev = make_event("error", None, "fixed", detail=f"移動失敗: {reason}", source="control",
                            error={"code": "MOVE_FAILED", "message": reason})
        else:
            print(f"[中断] {reason}", flush=True)
            return events
        post_event(args.server, ev)
        events.append(ev)
    except KeyboardInterrupt:
        print("中断しました(停止・解放コマンドを送ります)。", file=sys.stderr)
        raise
    finally:
        try:
            loco.StopMove()
        except Exception as e:
            print(f"[警告] 最終の StopMove に失敗: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
        arm.emergency_release()
    return events


def run(args, source=None, loco=None, arm=None, *, clock=time.monotonic, sleep=time.sleep):
    if getattr(args, "approach", False):
        return run_approach(args, loco, arm, clock=clock, sleep=sleep)
    base =ZoneApproach(args.vx, args.vyaw, args.turn_offset, args.arrive_size,
                        args.lost_timeout, args.move_timeout, args.search)
    zone_fixed = args.zone.strip().upper() if args.zone else None
    own_source = source is None
    if own_source:
        source = source_from_args(args)
    rec = None
    events = []
    try:
        rec = recognizer_from_args(args, source)
        if zone_fixed and zone_fixed not in rec.zones:
            raise PickMoveError(f"--zone {zone_fixed} が --zones {sorted(rec.zones)} に含まれていません。")
        if args.execute:
            print(SAFETY_NOTICE, flush=True)
        if loco is None:
            if args.execute:
                loco = make_real_loco(args.domain, args.interface, init_dds=args.source != "videohub")
            else:
                print("[dry-run] ロボットにはコマンドを送りません(実機へ送るには --execute)。", flush=True)
                loco = DryRunLoco()
        if arm is None:
            arm = ArmController(
                mode=getattr(args, "arm_mode", "preset"),
                execute=args.execute,
                custom_grab=getattr(args, "custom_grab", "grab_box"),
                custom_release=getattr(args, "custom_release", "release_arm"),
                custom_wait=getattr(args, "custom_wait", 3.0),
                clamp_roll=getattr(args, "clamp_roll", 0.15),
                open_roll=getattr(args, "open_roll", 0.45),
                shoulder_pitch=getattr(args, "shoulder_pitch", -0.35),
                elbow_angle=getattr(args, "elbow_angle", 0.50),
            )
        if args.execute:
            sleep(3.0)
        deadline = clock() + args.max_seconds if args.max_seconds else None
        rec.start()
        try:
            while deadline is None or clock() < deadline:
                if zone_fixed:
                    goal = {"box_id": None, "zone": zone_fixed}
                else:
                    goal = rec.pop_box(timeout=PERIOD)
                    if goal is None:
                        continue
                zone = goal["zone"]
                print(f"[箱] {goal['box_id']} を把持して区画 {zone} へ運ぶ" if goal["box_id"]
                      else f"[区画 {zone}] 箱を待たず直接向かう", flush=True)

                arm.grab()

                ev = make_event("moving", goal["box_id"], zone, detail=f"区画 {zone} へ移動開始",
                                run_id=rec.run_id, source="control",
                                data={"from": "start", "to": zone, "arm_mode": arm.mode})
                post_event(args.server, ev)
                events.append(ev)

                t_move = clock()
                state, reason = _approach(rec, loco, zone, base.fresh(), deadline, clock, sleep)
                _check("StopMove", loco.StopMove())

                if state == "arrived":
                    arm.release()
                    if args.gesture == "wave":
                        _check("WaveHand", loco.WaveHand())
                    ev = make_event("arrived", goal["box_id"], zone, detail=f"区画 {zone} に到着・配置完了",
                                    run_id=rec.run_id, source="control",
                                    data={"duration_ms": int((clock() - t_move) * 1000),
                                          "gesture": args.gesture, "arm_mode": arm.mode})
                elif state == "failed":
                    arm.release()
                    ev = make_event("error", goal["box_id"], zone, detail=f"移動失敗: {reason}",
                                    run_id=rec.run_id, source="control",
                                    error={"code": "MOVE_FAILED", "message": reason})
                else:
                    print(f"[中断] {reason}", flush=True)
                    break

                post_event(args.server, ev)
                events.append(ev)
                if args.once or zone_fixed:
                    break
        except KeyboardInterrupt:
            print("中断しました(停止・解放コマンドを送ります)。", file=sys.stderr)
            raise
        finally:
            try:
                loco.StopMove()
            except Exception as e:
                print(f"[警告] 最終の StopMove に失敗: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
            arm.emergency_release()
    finally:
        if rec is not None:
            rec.stop(close_source=own_source)
        elif own_source:
            source.close()
    return events


def build_parser():
    parser = argparse.ArgumentParser(
        description="箱QR確定→腕把持→区画マーカーへ接近→停止・解放。既定は dry-run(--execute で実機へ送信)")
    add_source_args(parser)
    add_recognizer_args(parser)
    parser.add_argument("--execute", action="store_true",
                        help="実機へコマンドを送る(付けなければ dry-run)")
    parser.add_argument("--vx", type=float, default=0.2, help=f"前進速度 m/s(上限 {VX_LIMIT} に丸める)")
    parser.add_argument("--vyaw", type=float, default=0.3, help=f"旋回速度 rad/s(上限 {VYAW_LIMIT} に丸める)")
    parser.add_argument("--turn-offset", type=float, default=0.15, help="旋回を始める |offset| の閾値")
    parser.add_argument("--arrive-size", type=float, default=0.25, help="到着とみなす size_ratio の閾値")
    parser.add_argument("--lost-timeout", type=float, default=3.0, help="マーカーを見失って待つ秒数")
    parser.add_argument("--move-timeout", type=float, default=30.0, help="1箱あたりの移動の制限秒数")
    parser.add_argument("--search", action="store_true", help="見失ったらゆっくり旋回して探す")
    parser.add_argument("--gesture", choices=("none", "wave"), default="none",
                        help="到着後の動作(wave=手を振る)")
    parser.add_argument("--once", action="store_true", help="1箱処理したら終了")
    parser.add_argument("--max-seconds", type=float, default=None, help="全体の制限秒数(テスト用)")
    parser.add_argument("--zone", type=str, default=None,
                        help="箱のQRを待たず、この zone へ直接向かう(移動だけの単体確認用)")

    parser.add_argument("--approach", action="store_true",
                        help="approachモード: 認識を使わず、把持→固定位置へ時間ベースで移動→解放")
    parser.add_argument("--approach-x", type=float, default=1.0,
                        help="approachモード: 前方への移動距離 m(負で後退)")
    parser.add_argument("--approach-y", type=float, default=0.0,
                        help="approachモード: 左方への移動距離 m(負で右)")
    parser.add_argument("--approach-yaw", type=float, default=0.0,
                        help="approachモード: 並進後の旋回角 rad(正で左回り)")

    parser.add_argument("--arm-mode", choices=("preset", "custom", "sdk", "none"), default="preset",
                        help="腕制御モード: preset(15->17->99), custom(ティーチング), sdk(rt/arm_sdk), none(腕なし)")
    parser.add_argument("--custom-grab", type=str, default="grab_box",
                        help="customモード時の把持アクション名")
    parser.add_argument("--custom-release", type=str, default="release_arm",
                        help="customモード時の解放アクション名(または release_arm)")
    parser.add_argument("--custom-wait", type=float, default=3.0,
                        help="customモード時のアクション完了待機秒数")
    parser.add_argument("--clamp-roll", type=float, default=0.15,
                        help="sdkモード時の把持肩ロール角(rad: 小さいほど狭く挟む)")
    parser.add_argument("--open-roll", type=float, default=0.45,
                        help="sdkモード時の開放肩ロール角(rad: 大きいほど広く開く)")
    parser.add_argument("--shoulder-pitch", type=float, default=-0.35,
                        help="sdkモード時の肩ピッチ角(rad: 負で腕を前に上げる)")
    parser.add_argument("--elbow-angle", type=float, default=0.50,
                        help="sdkモード時の肘角度(rad)")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    try:
        run(args)
    except (CameraError, PickMoveError) as e:
        print(f"[エラー] {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
