#!/usr/bin/env python3
"""実機なしで高レベル API スクリプトを試すための「偽 R1」。

R1 の sport / arm / voice サービスと同じ名前・API ID の RPC サーバーを開発 PC 上に立て、
受け取ったコマンドを表示する。物理シミュレーションは行わない（転倒・干渉・実際の動きは分からない）。
確認できるのは「呼び出しが届くか」「引数・順序・タイミングが意図どおりか」「エラー処理」まで。

  pixi run python scripts/mock_robot.py                 # ターミナル1（DDS ドメイン 1）
  pixi run python scripts/my_script.py --domain 1       # ターミナル2

実機は必ずドメイン 0。モックを 0 で動かすと実機のサービスと衝突するため、既定を 1 にしてある。
"""
import argparse
import json
import math
import threading
import time
from pathlib import Path

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.rpc.server import Server

FSM_NAMES = {0: "ZeroTorque", 1: "Damp", 4: "LockedStand", 701: "Lie2StandUp", 702: "StandUp2Lie", 811: "Run"}
ARM_ACTIONS = {
    99: ("release_arm", 0.0, False), 11: ("blow_kiss_with_both_hands", 3.0, False),
    12: ("blow_kiss_with_left_hand", 3.0, False), 13: ("blow_kiss_with_right_hand", 3.0, False),
    15: ("both_hands_up", 1.0, True), 17: ("clamp", 2.2, False), 18: ("high_five", 1.0, True),
    19: ("hug", 2.0, True), 22: ("refuse", 0.9, True), 23: ("right_hand_up", 1.0, True),
    24: ("ultraman_ray", 2.1, True), 25: ("wave_under_head", 2.6, False), 26: ("wave_above_head", 5.7, False),
    27: ("shake_hand", 1.0, True), 28: ("box_left_hand_win", 1.0, True), 29: ("box_right_hand_win", 1.0, True),
    30: ("box_both_hand_win", 2.5, True), 31: ("extend_right_arm_forward", 1.0, True),
    33: ("right_hand_on_heart", 1.6, True), 34: ("both_hands_up_deviate_right", 1.0, True),
    35: ("emphasize", 0.6, True), 36: ("forward_push", 1.5, True),
}


def log(tag: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {tag:<5} {msg}", flush=True)


class MockSport(Server):
    """速度指令を積分して、平面上の位置 (x, y, yaw) だけ追跡する。"""

    def __init__(self, fsm: int = 811):
        super().__init__("sport")
        self.fsm = fsm
        self.vel = (0.0, 0.0, 0.0)
        self.vel_until = 0.0
        self.pose = [0.0, 0.0, 0.0]
        self.lock = threading.Lock()

    def Init(self):
        self._SetApiVersion("1.0.0.0")
        self._RegistHandler(7001, self.get_fsm, 0)
        self._RegistHandler(7101, self.set_fsm, 0)
        self._RegistHandler(7105, self.set_velocity, 0)
        self._RegistHandler(7106, self.set_task, 0)
        threading.Thread(target=self.integrate, daemon=True).start()

    def get_fsm(self, _):
        return 0, json.dumps({"data": self.fsm})

    def set_fsm(self, p):
        fsm = json.loads(p)["data"]
        if fsm not in FSM_NAMES:
            log("sport", f"SetFsmId({fsm}) -> R1 の公開 ID ではない。拒否 (7302)")
            return 7302, ""
        note = "  ※実機では立位からだと転倒する" if fsm in (0, 1) and self.fsm in (4, 811) else ""
        self.fsm = fsm
        log("sport", f"SetFsmId({fsm}) = {FSM_NAMES[fsm]}{note}")
        return 0, ""

    def set_velocity(self, p):
        d = json.loads(p)
        vx, vy, om = d["velocity"]
        dur = d.get("duration", 1.0)
        with self.lock:
            self.vel, self.vel_until = (vx, vy, om), time.time() + dur
        note = "" if self.fsm == 811 else "  ※Run(811) 以外なので実機では動かない"
        log("sport", f"SetVelocity vx={vx:+.2f} vy={vy:+.2f} omega={om:+.2f} duration={dur:g}s{note}")
        return 0, ""

    def set_task(self, p):
        task = json.loads(p)["data"]
        names = {0: "WaveHand", 1: "WaveHand(turn)", 2: "ShakeHand 開始", 3: "ShakeHand 終了"}
        log("sport", f"SetTaskId({task}) = {names.get(task, '不明')}")
        return 0, ""

    def integrate(self, dt: float = 0.05):
        moving = False
        while True:
            time.sleep(dt)
            with self.lock:
                active = time.time() < self.vel_until and self.fsm == 811 and any(self.vel)
                vx, vy, om = self.vel
            if active:
                yaw = self.pose[2]
                self.pose[0] += (vx * math.cos(yaw) - vy * math.sin(yaw)) * dt
                self.pose[1] += (vx * math.sin(yaw) + vy * math.cos(yaw)) * dt
                self.pose[2] += om * dt
            elif moving:
                x, y, yaw = self.pose
                log("sport", f"停止。推定位置 x={x:+.2f}m y={y:+.2f}m yaw={math.degrees(yaw):+.0f}deg")
            moving = active


class MockArm(Server):
    def __init__(self):
        super().__init__("arm")
        self.holding = None

    def Init(self):
        self._SetApiVersion("1.0.0.0")
        self._RegistHandler(7106, self.execute, 0)
        self._RegistHandler(7107, self.get_list, 0)
        self._RegistHandler(7108, self.execute_custom, 0)
        self._RegistHandler(7113, self.stop_custom, 0)

    def execute(self, p):
        d = json.loads(p)
        if "action_id" not in d:
            log("arm", f"パラメータ不正 {d}（R1 は {{\"action_id\": N}}。G1ArmActionClient は使えない） -> 3204")
            return 3204, ""
        aid = d["action_id"]
        if aid not in ARM_ACTIONS:
            log("arm", f"ExecuteAction({aid}) -> 不明な ID (7402)")
            return 7402, ""
        name, duration, holds = ARM_ACTIONS[aid]
        if self.holding is not None and aid not in (99, self.holding):
            log("arm", f"ExecuteAction({aid}={name}) -> 保持中 (7401)。先に 99 で解除する")
            return 7401, ""
        if self.holding is not None:
            log("arm", f"ExecuteAction({aid}) -> 保持を解除")
            self.holding = None
            return 0, ""
        log("arm", f"ExecuteAction({aid}) = {name}（{duration}s 再生{'・姿勢保持' if holds else ''}）")
        time.sleep(duration)  # 実機と同じくブロッキング
        self.holding = aid if holds else None
        return 0, ""

    def get_list(self, _):
        presets = [{"id": i, "name": n} for i, (n, _d, _h) in ARM_ACTIONS.items()]
        return 0, json.dumps([presets, []])

    def execute_custom(self, p):
        log("arm", f"ExecuteCustomAction({json.loads(p).get('action_name')}) -> 未登録 (7403)")
        return 7403, ""

    def stop_custom(self, _):
        log("arm", "StopCustomAction")
        return 0, ""


class MockVoice(Server):
    def __init__(self):
        super().__init__("voice")
        self.volume = 70

    def Init(self):
        self._SetApiVersion("1.0.0.0")
        self._RegistHandler(1001, self.tts, 0)
        self._RegistHandler(1004, self.stop_play, 0)
        self._RegistHandler(1005, self.get_volume, 0)
        self._RegistHandler(1006, self.set_volume, 0)
        self._RegistHandler(1010, self.led, 0)

    def tts(self, p):
        d = json.loads(p)
        lang = {0: "中国語", 1: "英語"}.get(d.get("speaker_id"), "不明な speaker_id")
        note = "" if d.get("text", "").isascii() or d.get("speaker_id") == 0 else "  ※非 ASCII 文字。日本語は発話できない"
        log("voice", f"TtsMaker[{lang}] \"{d.get('text')}\"{note}")
        return 0, ""

    def stop_play(self, p):
        log("voice", f"PlayStop {p}")
        return 0, ""

    def get_volume(self, _):
        return 0, json.dumps({"volume": self.volume})

    def set_volume(self, p):
        self.volume = json.loads(p)["volume"]
        log("voice", f"SetVolume({self.volume})")
        return 0, ""

    def led(self, p):
        d = json.loads(p)
        log("voice", f"LedControl R={d['R']} G={d['G']} B={d['B']}")
        return 0, ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Unitree R1 high-level services (no physics)")
    parser.add_argument("-i", "--interface", default=None, help="Network interface (default: auto)")
    parser.add_argument("-d", "--domain", type=int, default=1, help="DDS domain id (real robot = 0)")
    parser.add_argument("--fsm", type=int, default=811, choices=sorted(FSM_NAMES),
                        help="起動時のモード。既定 811 = コントローラーで Running Mode にした後の状態")
    args = parser.parse_args()

    if args.domain == 0:
        print("警告: ドメイン 0 は実機と同じ。実機がつながっているとサービスが衝突する。")
    try:
        Path("/tmp").mkdir(exist_ok=True)
    except OSError:
        pass
    ChannelFactoryInitialize(args.domain, args.interface)

    servers = [MockSport(args.fsm), MockArm(), MockVoice()]
    for s in servers:
        s.Init()
        s.Start(False)
    log("mock", f"R1 モック起動 (domain={args.domain}, fsm={args.fsm} {FSM_NAMES[args.fsm]})。Ctrl+C で終了。")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
